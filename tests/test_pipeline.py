"""Failure gates and full EPUB generation through a local OpenAI-compatible stub."""
import base64
import contextlib
import io
import json
import os
import shutil
import sys
import threading
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree as ET

from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import translate_epub as T
from optimize_epub import optimize_epub
from review_epub import review_epub, accept_feedback, validate_feedback
from test_epubcheck import make_epub


CHAPTER = '''<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">
<head><title>Chapter</title></head><body><h1>Chapter</h1>
<nav epub:type="toc"><ol><li><a href="#p">Section</a></li></ol></nav>
<p id="p">Compute <code id="code-anchor">run(x)</code> for
<math xmlns="http://www.w3.org/1998/Math/MathML" id="math-anchor"><mi>x</mi></math>
and <img src="formula.gif" alt="x_i"/>. Visit https://example.test/path.</p>
<pre><code>msg = """first

last"""</code></pre>
<figure><svg xmlns="http://www.w3.org/2000/svg" width="100" height="30" viewBox="0 0 100 30">
<defs><linearGradient id="gradient"><stop offset="0" stop-color="black"/></linearGradient></defs>
<rect width="100" height="30" fill="url(#gradient)"/></svg><figcaption>A simple gradient illustration.</figcaption></figure>
<table id="table"><caption>Parameter values.</caption><tr><th>Parameter</th><th>Value</th></tr><tr><td>Formula</td><td><img src="formula.gif" alt="x_i"/></td></tr></table>
</body></html>'''


def make_rich_epub(path, *, epub2=False):
    make_epub(path)
    with zipfile.ZipFile(path) as z:
        files = {n: z.read(n) for n in z.namelist()}
    files['OEBPS/chapter.xhtml'] = CHAPTER.encode('utf-8')
    opf = files['OEBPS/content.opf'].decode('utf-8')
    opf = opf.replace('id="chapter" href="chapter.xhtml" media-type="application/xhtml+xml"',
                      'id="chapter" href="chapter.xhtml" media-type="application/xhtml+xml" properties="mathml svg"')
    opf = opf.replace('</manifest>', '<item id="formula" href="formula.gif" media-type="image/gif"/></manifest>')
    if epub2:
        opf = opf.replace('version="3.0"', 'version="2.0"')
        opf = opf.replace('<meta property="dcterms:modified">2026-01-01T00:00:00Z</meta>', '')
        opf = opf.replace(' properties="mathml svg"', '').replace(' properties="nav"', '')
        opf = opf.replace('<spine>', '<spine toc="ncx">')
        opf = opf.replace('</manifest>', '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/></manifest>')
        files['OEBPS/chapter.xhtml'] = b'<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.1//EN" "http://www.w3.org/TR/xhtml11/DTD/xhtml11.dtd"><html xmlns="http://www.w3.org/1999/xhtml"><head><title>Chapter</title></head><body><h1>Chapter</h1><p>Read&nbsp;this chapter.</p><pre><code>print(1)</code></pre></body></html>'
        files['OEBPS/nav.xhtml'] = b'<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Contents</title></head><body><p>Contents</p><p><a href="chapter.xhtml">Chapter</a></p></body></html>'
        files['OEBPS/toc.ncx'] = b'<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1"><head><meta name="dtb:uid" content="urn:uuid:12345678-1234-1234-1234-123456789abc"/></head><docTitle><text>Sample</text></docTitle><navMap><navPoint id="chapter" playOrder="1"><navLabel><text>Chapter</text></navLabel><content src="chapter.xhtml"/></navPoint></navMap></ncx>'
    files['OEBPS/content.opf'] = opf.encode('utf-8')
    files['OEBPS/formula.gif'] = base64.b64decode('R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7')
    with zipfile.ZipFile(path, 'w') as z:
        for name, data in files.items():
            z.writestr(name, data, compress_type=zipfile.ZIP_STORED if name == 'mimetype' else zipfile.ZIP_DEFLATED)


class ProtectionTests(unittest.TestCase):
    def test_math_survives_in_its_translated_position_without_duplicate_ids(self):
        soup = T.parse_document('<p>For <math xmlns="http://www.w3.org/1998/Math/MathML" id="m"><mi>x</mi></math>'
                                ' call <code id="c">run(x)</code>.</p>')
        source = soup.p
        self.assertIn('⟦EPUB_MATH_0⟧', T.node_translation_text(source))
        T.insert_translation_node(soup, source, '调用 <code data-epub-inline-code="0">run(x)</code>，参数为 ⟦EPUB_MATH_0⟧。')
        translated = source.find_next_sibling('p')
        self.assertEqual(translated.math.mi.get_text(), 'x')
        self.assertEqual(translated.get_text(), '调用 run(x)，参数为 x。')
        self.assertEqual(len(soup.find_all(id='m')), 1)
        self.assertEqual(len(soup.find_all(id='c')), 1)

    def test_embedded_toc_does_not_swallow_chapter_body_or_plain_caption(self):
        soup = T.parse_document(CHAPTER)
        units, overrides = T.collect_units_in_html(soup, include_toc_links=True)
        self.assertIn(soup.find(id='p'), units)
        self.assertIn(soup.figcaption, units)
        self.assertIn(soup.nav.a, units)
        self.assertNotIn(soup.nav.li, units)
        self.assertIn(soup.caption, units)

    def test_legacy_entities_validate_locally_but_unknown_entities_fail(self):
        root = T.parse_xml_document('<p>Read&nbsp;this &amp; that</p>')
        self.assertEqual(root.text, 'Read\u00a0this & that')
        with self.assertRaises(ET.ParseError):
            T.parse_xml_document('<p>&invented_entity;</p>')

    def test_code_multiline_string_keeps_blank_line(self):
        soup = T.parse_document('<pre><code>msg = """first\n\nlast"""</code></pre>')
        before = soup.pre.get_text()
        T.normalise_code_blocks(soup)
        self.assertEqual(soup.pre.get_text(), before)

    def test_svg_and_math_attribute_case_survives(self):
        soup = T.parse_document(CHAPTER)
        self.assertIn('viewBox', soup.svg.attrs)
        self.assertIsNotNone(soup.find('linearGradient'))
        self.assertIn('viewBox=', str(soup))

    def test_epub2_generated_nodes_have_no_epub3_aria_attributes(self):
        soup = T.parse_document('<html><head/><body><h1>Chapter</h1><p>Identifier</p></body></html>')
        T.insert_translation_node(soup, soup.h1, '章节')
        T.insert_translation_node(soup, soup.p, 'Identifier')
        self.assertIn('aria-hidden', str(soup))
        self.assertNotIn('aria-hidden', T.serialize_document(soup, '2.0'))

    def test_placeholder_loss_and_altered_code_are_rejected_after_retry(self):
        source = 'Read https://example.test and <code data-epub-inline-code="0">run()</code> with ⟦EPUB_MATH_0⟧.'
        with patch.object(T, 'translate_items_json', return_value={'00000000': '读一下。'}):
            self.assertEqual(T.run_batch([source])[source], '')
        self.assertFalse(T.valid_translation(source, source.replace('run()', 'delete_all()')))
        self.assertFalse(T.valid_translation(source, source.replace('https://example.test', '')))

    def test_partial_response_results_accumulate_across_retries(self):
        responses = [json.dumps([{'id':'a','zh':'甲'}]), json.dumps([{'id':'b','zh':'乙'}])]
        with patch.object(T, 'call_llm_once', side_effect=responses), patch.object(T.time, 'sleep'):
            result = T.translate_items_json([{'id':'a','text':'A'}, {'id':'b','text':'B'}], max_retry=2)
        self.assertEqual(result, {'a':'甲', 'b':'乙'})


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.work = ROOT / '.test-scratch' / self._testMethodName
        self.work.mkdir(parents=True, exist_ok=True)
        self.source = self.work / 'input.epub'
        make_rich_epub(self.source)
        self.old_cache, self.old_path, self.old_config, self.old_client = T.CACHE, T.CACHE_PATH, T.CONFIG, T.CLIENT
        self.calls = []
        calls = self.calls

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                calls.append(body)
                items = json.loads(body['messages'][-1]['content'])
                result = []
                for item in items:
                    tokens = [m.group(0) for m in T.INLINE_CODE_MARKER_RE.finditer(item['text'])]
                    tokens += T.RESERVED_TOKEN_RE.findall(item['text'])
                    zh = '示例书' if item['text'] == 'Sample' else '示例译文 ' + ' '.join(tokens)
                    result.append({'id':item['id'], 'zh':zh.strip()})
                response = json.dumps({'id':'fixture', 'object':'chat.completion', 'created':1, 'model':'fixture',
                    'choices':[{'index':0, 'message':{'role':'assistant','content':json.dumps(result,ensure_ascii=False)},
                                'finish_reason':'stop'}], 'usage':{'prompt_tokens':1,'completion_tokens':1,'total_tokens':2}}).encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(response)))
                self.end_headers()
                self.wfile.write(response)

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f'http://127.0.0.1:{self.server.server_port}/v1'
        self.config = self.work / 'fixture-config.json'
        self.config.write_text(json.dumps({'api_key':'fixture-key', 'base_url':self.base_url, 'model_name':'fixture'}), encoding='utf-8')

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        if T.CLIENT is not None and T.CLIENT is not self.old_client:
            T.CLIENT.close()
        T.CACHE, T.CACHE_PATH, T.CONFIG, T.CLIENT = self.old_cache, self.old_path, self.old_config, self.old_client
        shutil.rmtree(self.work)

    def run_main(self, *extra):
        args = ['translate_epub.py', str(self.source), str(self.work/'output'/'placeholder.epub'),
                '--config', str(self.config), '--work-dir', str(self.work/'translation'), '--concurrency','2', '--batch-tokens','600', *extra]
        env = {'API_KEY':'fixture-key', 'BASE_URL':self.base_url, 'MODEL_NAME':'fixture', 'REASONING_PROVIDER':'generic'}
        captured = io.StringIO()
        with patch.object(sys, 'argv', args), patch.dict(os.environ, env), contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
            result = T.main()
        self.last_log = captured.getvalue()
        return result

    @unittest.skipUnless(shutil.which('java'), 'Java required for real EPUBCheck')
    def test_real_http_translation_package_check_and_cache_resume(self):
        self.assertEqual(self.run_main(), 0, self.last_log)
        self.assertGreater(len(self.calls), 0)
        output = self.work/'output'/'示例书.epub'
        with zipfile.ZipFile(output) as z:
            soup = T.parse_document(z.read('OEBPS/chapter.xhtml').decode('utf-8'))
            translated = soup.find(id='p').find_next_sibling('p')
            self.assertIsNotNone(translated.math)
            self.assertIsNotNone(translated.find('img', class_='formula-img'))
            self.assertIsNotNone(soup.select_one('figcaption .translated-zh'))
            self.assertEqual(len(soup.figure.find_all('figcaption')), 1)
            self.assertIsNotNone(soup.select_one('table.table-zh img.formula-img'))
            self.assertIsNotNone(soup.select_one('table#table caption .translated-zh'))
            self.assertEqual(len(soup.find_all(id='math-anchor')), 1)
            self.assertEqual(len(soup.find_all(id='code-anchor')), 1)
            self.assertIn('\n\n', soup.pre.get_text())
            self.assertIn('viewBox', soup.svg.attrs)
            self.assertEqual(z.infolist()[0].filename, 'mimetype')
        report = json.loads(output.with_suffix('.epubcheck.json').read_text(encoding='utf-8'))
        self.assertEqual(report['checker']['nError'] + report['checker']['nFatal'], 0)
        calls = len(self.calls)
        self.assertEqual(self.run_main('--resume'), 0)
        self.assertEqual(len(self.calls), calls)

    @unittest.skipUnless(shutil.which('java'), 'Java required for real EPUBCheck')
    def test_epub2_translation_and_ncx_pass_real_checker(self):
        make_rich_epub(self.source, epub2=True)
        self.assertEqual(self.run_main(), 0, self.last_log)
        output = self.work/'output'/'示例书.epub'
        with zipfile.ZipFile(output) as z:
            root = ET.fromstring(z.read('OEBPS/toc.ncx'))
            label = root.find('.//{*}navLabel/{*}text').text
            self.assertIn('示例译文', label)
            self.assertNotIn('aria-hidden', z.read('OEBPS/chapter.xhtml').decode('utf-8'))

    def test_checker_failure_does_not_replace_existing_output(self):
        output = self.work/'output'/'示例书.epub'
        output.parent.mkdir()
        output.write_bytes(b'previous validated artifact')
        def reject(candidate):
            report = candidate.with_suffix('.epubcheck.json')
            report.write_text('{}', encoding='utf-8')
            return False, report, 0, 1, 0
        with patch.object(T, 'validate_epub', side_effect=reject):
            self.assertEqual(self.run_main(), 1)
        self.assertEqual(output.read_bytes(), b'previous validated artifact')
        self.assertTrue(output.with_suffix('.failed.epub').is_file())

    @unittest.skipUnless(os.environ.get('EPUB_REVIEW_BROWSER'), 'Real Chromium required')
    def test_translation_style_review_reaches_pending_agent_review(self):
        review_dir = self.work/'review'
        self.assertEqual(self.run_main('--style-review', '--review-dir', str(review_dir),
            '--browser-executable', os.environ['EPUB_REVIEW_BROWSER']), 0, self.last_log)
        report = json.loads((review_dir/'report.json').read_text(encoding='utf-8'))
        self.assertEqual(report['status'], 'awaiting_agent_review')
        self.assertFalse(report['render_errors'])
        self.assertTrue(all(row.get('sha256') for row in report['screenshots']))

    def test_scan_and_write_failures_do_not_report_success(self):
        with patch.object(T, 'scan_html_file', side_effect=ValueError('fixture scan failure')):
            self.assertEqual(self.run_main(), 1)
        self.assertFalse(list((self.work/'output').glob('*.epub')))
        with patch.object(T, 'writeback_html_file', side_effect=OSError('fixture write failure')):
            self.assertEqual(self.run_main(), 1)
        self.assertFalse(list((self.work/'output').glob('*.epub')))

    def test_missing_translation_blocks_packaging_and_bad_cache_is_retried(self):
        cache = self.work/'translation'/'cache.json'
        cache.parent.mkdir(parents=True)
        cache.write_text(json.dumps({'Sample':''}), encoding='utf-8')
        with patch.object(T, 'run_batch', return_value={}):
            self.assertEqual(self.run_main('--resume'), 1)
        self.assertFalse(list((self.work/'output').glob('*.epub')))
        self.assertNotIn('Sample', json.loads(cache.read_text(encoding='utf-8')))

    def test_unrelated_work_directory_and_input_containment_are_rejected(self):
        unrelated = self.work/'unrelated'
        unrelated.mkdir()
        kept = unrelated/'keep.txt'
        kept.write_text('preserve me', encoding='utf-8')
        with self.assertRaises(ValueError):
            T.prepare_work_directory(unrelated, self.source, self.work/'out.epub')
        self.assertEqual(kept.read_text(encoding='utf-8'), 'preserve me')
        with self.assertRaises(ValueError):
            T.prepare_work_directory(self.work, self.source, self.work/'out.epub')

    def test_pack_failure_preserves_previous_output(self):
        source_dir = self.work/'extract'
        T.unpack_epub(self.source, source_dir)
        output = self.work/'out.epub'
        output.write_bytes(b'previous artifact')
        with patch.object(T, '_pack_epub_contents', side_effect=OSError('disk full')), self.assertRaises(OSError):
            T.pack_epub(source_dir, output)
        self.assertEqual(output.read_bytes(), b'previous artifact')

    def test_zip_traversal_is_rejected_before_extracting_any_entries(self):
        bad = self.work/'bad.epub'
        with zipfile.ZipFile(bad, 'w') as z:
            z.writestr('mimetype', 'application/epub+zip')
            z.writestr('safe.txt', 'safe')
            z.writestr('../escaped.txt', 'escaped')
        target = self.work/'extract'
        with self.assertRaises(ValueError):
            T.unpack_epub(bad, target)
        self.assertFalse(list(target.iterdir()))
        self.assertFalse((self.work/'escaped.txt').exists())

    def test_restyle_roundtrip_keeps_svg_math_and_code(self):
        output = self.work/'styled.epub'
        result = optimize_epub(self.source, output, check=False)
        self.assertTrue(result['content_preserved'])
        with zipfile.ZipFile(output) as z:
            soup = T.parse_document(z.read('OEBPS/chapter.xhtml').decode('utf-8'))
            self.assertIn('viewBox', soup.svg.attrs)
            self.assertIsNotNone(soup.find('linearGradient'))
            self.assertIn('\n\n', soup.pre.get_text())

    def test_css_injection_preserves_explicit_xml_namespaces(self):
        root = self.work/'extract'
        root.mkdir()
        chapter = root/'chapter.htm'
        chapter.write_text('<x:html xmlns:x="http://www.w3.org/1999/xhtml"><x:head><x:title>Chapter</x:title></x:head><x:body/></x:html>', encoding='utf-8')
        opf = root/'content.opf'
        opf.write_text('<o:package xmlns:o="http://www.idpf.org/2007/opf"><o:manifest><o:item id="translated-css" href="existing.css"/></o:manifest></o:package>', encoding='utf-8')
        T.inject_css(root)
        self.assertIsNotNone(ET.parse(chapter).find('.//{http://www.w3.org/1999/xhtml}link'))
        manifest = ET.parse(opf).find('.//{http://www.idpf.org/2007/opf}manifest')
        self.assertEqual(len(manifest.findall('{http://www.idpf.org/2007/opf}item')), 2)
        self.assertEqual(len({item.get('id') for item in manifest}), 2)

    @unittest.skipUnless(os.environ.get('EPUB_REVIEW_BROWSER'), 'Real Chromium required')
    def test_render_failure_is_recorded_and_raises_instead_of_success(self):
        plan = self.work/'plan.json'
        plan.write_text(json.dumps({'samples':[{'category':'invalid','file':'OEBPS/chapter.xhtml','selector':'[','index':0}]}), encoding='utf-8')
        output = self.work/'review'
        with self.assertRaises(RuntimeError):
            review_epub(self.source, output, plan=plan, widths=(420,), browser_executable=os.environ['EPUB_REVIEW_BROWSER'])
        report = json.loads((output/'report.json').read_text(encoding='utf-8'))
        self.assertEqual(report['status'], 'render_failed')
        self.assertTrue(report['render_errors'])

    @unittest.skipUnless(os.environ.get('EPUB_REVIEW_BROWSER'), 'Real Chromium required')
    def test_render_feedback_rejects_tampered_png_and_artifact(self):
        output = self.work/'review'
        report = review_epub(self.source, output, spread=1, widths=(420,), scan_all=True,
                             browser_executable=os.environ['EPUB_REVIEW_BROWSER'])
        feedback = {'artifact_sha256':report['artifact_sha256'],'status':'reviewed',
                    'summary':'Synthetic fixture acknowledgement for state-machine testing only.',
                    'reviewed_screenshots':[s['screenshot'] for s in report['screenshots']],
                    'findings':[{'status':'accepted','screenshot':report['screenshots'][0]['screenshot'],
                                 'reason':'Synthetic fixture diagnostics.', 'issue_ids':[i['id'] for i in report['issues']]}] if report['issues'] else []}
        path = output/'feedback.json'
        path.write_text(json.dumps(feedback), encoding='utf-8')
        accept_feedback(self.source, output, path)
        png = output/report['screenshots'][0]['screenshot']
        original = png.read_bytes()
        png.write_bytes(b'tampered')
        with self.assertRaises(ValueError):
            accept_feedback(self.source, output, path)
        png.write_bytes(original)
        self.source.write_bytes(self.source.read_bytes() + b'changed')
        with self.assertRaises(ValueError):
            accept_feedback(self.source, output, path)


class FeedbackCoverageTests(unittest.TestCase):
    def test_one_disposition_cannot_hide_other_layout_issues(self):
        report = {'artifact_sha256':'x', 'screenshots':[{'screenshot':'p.png'}], 'render_errors':[],
                  'issue_count':2, 'issues':[{'id':'issue-0001'}, {'id':'issue-0002'}]}
        feedback = {'artifact_sha256':'x','status':'reviewed','summary':'Fixture', 'reviewed_screenshots':['p.png'],
                    'findings':[{'screenshot':'p.png','status':'accepted','reason':'Reviewed','issue_ids':['issue-0001']}]}
        with self.assertRaises(ValueError):
            validate_feedback(report, feedback)
        feedback['findings'][0]['issue_ids'].append('issue-0002')
        validate_feedback(report, feedback)

    def test_issue_cannot_be_accepted_using_another_chapters_screenshot(self):
        report = {'artifact_sha256':'x', 'screenshots':[{'screenshot':'p.png', 'file':'a.xhtml', 'width':420}],
                  'render_errors':[], 'issue_count':1,
                  'issues':[{'id':'issue-0001', 'file':'b.xhtml', 'width':420}]}
        feedback = {'artifact_sha256':'x','status':'reviewed','summary':'Fixture', 'reviewed_screenshots':['p.png'],
                    'findings':[{'screenshot':'p.png','status':'accepted','reason':'Reviewed','issue_ids':['issue-0001']}]}
        with self.assertRaises(ValueError):
            validate_feedback(report, feedback)


if __name__ == '__main__':
    unittest.main()
