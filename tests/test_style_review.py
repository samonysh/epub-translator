"""Content preservation, reusable sampling, and evidence-bound visual feedback."""
import json
import os
import shutil
import struct
import sys
import unittest
import zipfile
from pathlib import Path

from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import translate_epub as T
from optimize_epub import optimize_epub
from review_epub import sample_locations, validate_feedback, review_epub


class ReadingLayoutTests(unittest.TestCase):
    def test_layout_is_idempotent_and_preserves_content(self):
        source = ('<body><p id="p">Source <code>f(x)</code></p><p class="translated-zh">译文</p>'
                  '<div class="ProgramCode"><pre><code>  first()\n\n    next()</code></pre></div>'
                  '<table id="table"><tr><td>Data</td></tr></table>'
                  '<p>Inline <math><mi>x</mi></math>.</p>'
                  '<div class="Equation"><math display="block"><mi>y</mi></math></div></body>')
        soup = BeautifulSoup(source, "html.parser")
        before = soup.get_text()
        T.normalise_reading_layout(soup)
        once = str(soup)
        T.normalise_reading_layout(soup)
        self.assertEqual(str(soup), once)
        self.assertEqual(soup.get_text(), before)
        self.assertEqual(soup.pre.get_text(), '  first()\n\n    next()')
        self.assertEqual(len(soup.select('.reading-code-block')), 1)
        self.assertEqual(len(soup.select('.reading-equation-scroll')), 1)
        self.assertEqual(soup.select_one('.reading-source')['id'], 'p')
        self.assertEqual(soup.select_one('.reading-table-wrap table')['id'], 'table')
        self.assertIsNone(soup.select_one('p > div'))

    def test_tex_in_a_figure_description_is_not_a_formula(self):
        soup = BeautifulSoup('<figure><span class="math-inline"><img class="formula-img" '
                             'src="book_Fig3.png" alt="A diagram explaining the transform \\sum{x} through several processing stages."/></span></figure>'
                             '<p><img src="book_TeX_IEq4.png" alt="x_i"/></p>', 'html.parser')
        T.normalise_images_for_reading(soup, set(), deduplicate=False)
        self.assertIn('epub-image', soup.figure.img['class'])
        self.assertNotIn('formula-img', soup.figure.img['class'])
        self.assertIsNone(soup.figure.select_one('.math-inline'))
        self.assertIn('formula-img', soup.p.img['class'])

    def test_restyle_never_drops_repeated_resources(self):
        soup = BeautifulSoup('<figure><img src="x.png"/></figure><figure><img src="x.png"/></figure>', 'html.parser')
        T.normalise_images_for_reading(soup, set(), deduplicate=False)
        self.assertEqual(len(soup.find_all('img')), 2)

    def test_inline_publisher_code_span_is_not_a_block_panel(self):
        soup = BeautifulSoup('<p>Call <span class="code">run()</span> now.</p>', 'html.parser')
        T.normalise_reading_layout(soup)
        self.assertFalse(soup.select('.reading-code-block'))


class VisualFeedbackTests(unittest.TestCase):
    def test_feedback_requires_exact_artifact_and_every_screenshot(self):
        report = {'artifact_sha256': 'abc', 'screenshots': [{'screenshot': 'wide.png'}, {'screenshot': 'narrow.png'}],
                  'render_errors': [], 'issue_count': 0}
        feedback = {'artifact_sha256': 'abc', 'status': 'reviewed', 'summary': 'Inspected both widths.',
                    'reviewed_screenshots': ['wide.png', 'narrow.png'], 'findings': []}
        validate_feedback(report, feedback)
        for key, value in [('artifact_sha256', 'stale'), ('status', 'pending'), ('reviewed_screenshots', ['wide.png'])]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate_feedback(report, dict(feedback, **{key: value}))
        with self.assertRaises(ValueError):
            validate_feedback(dict(report, issue_count=1), feedback)


class OptimizeArtifactTests(unittest.TestCase):
    def setUp(self):
        self.work = ROOT / '.test-scratch' / self._testMethodName
        self.work.mkdir(parents=True, exist_ok=True)
        self.source = self.work / 'input.epub'
        chapter = ('<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Sample</title>'
                   '<link rel="stylesheet" href="publisher.css"/></head><body><h1>Chapter</h1>'
                   '<p class="Para">A paragraph to compare.</p><p class="translated-zh">对应译文。</p>'
                   '<div class="ProgramCode"><pre><code>  x = 1\n\n    run(x)</code></pre></div>'
                   '<figure><img class="epub-inline-image" src="fig.png" alt="An example diagram of the entire processing system."/></figure>'
                   '<figure><img src="small.png" alt="Small diagram of a system"/></figure>'
                   '<figure><img src="wide.png" alt="Wide diagram of a system"/></figure>'
                   '<div class="Biography"><div class="BiographyFigure"><figure class="Figure"><div class="MediaObject">'
                   '<img src="portrait.png" alt="Portrait"/></div></figure></div></div>'
                   '<table><tr><td>One</td><td>Two</td></tr></table>'
                   '<div class="Equation"><math xmlns="http://www.w3.org/1998/Math/MathML" display="block"><mi>x</mi></math></div>'
                   '</body></html>')
        # Valid 800x400 PNG, compressed scanlines using only the standard library.
        import zlib
        def chunk(kind, data):
            return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))
        def png(w, h):
            return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', w, h, 8, 2, 0, 0, 0)) +
                    chunk(b'IDAT', zlib.compress((b'\0' + b'\x75\x98\xb2' * w) * h)) + chunk(b'IEND', b''))
        with zipfile.ZipFile(self.source, 'w') as z:
            z.writestr('mimetype', 'application/epub+zip')
            z.writestr('OEBPS/chapter.xhtml', chapter)
            z.writestr('OEBPS/fig.png', png(800, 400))
            z.writestr('OEBPS/small.png', png(180, 120))
            z.writestr('OEBPS/wide.png', png(1600, 160))
            z.writestr('OEBPS/portrait.png', png(200, 250))
            z.writestr('OEBPS/publisher.css', '.ProgramCode pre {padding:40px} .Figure img {width:10px} body {min-width:0}'
                       '.BiographyFigure {float:right;width:160px}')
            z.writestr('OEBPS/content.opf', '<package><manifest><item id="c" href="chapter.xhtml"/></manifest><spine><itemref idref="c"/></spine></package>')

    def tearDown(self):
        shutil.rmtree(self.work)

    def test_restyle_preserves_resources_and_packages_mimetype(self):
        output = self.work / 'out.epub'
        result = optimize_epub(self.source, output, check=False)
        self.assertTrue(result['content_preserved'])
        self.assertEqual(result['epubcheck'], 'not_run')
        with zipfile.ZipFile(self.source) as old, zipfile.ZipFile(output) as new:
            self.assertEqual(new.infolist()[0].filename, 'mimetype')
            self.assertEqual(new.infolist()[0].compress_type, zipfile.ZIP_STORED)
            self.assertEqual(old.read('OEBPS/fig.png'), new.read('OEBPS/fig.png'))
            self.assertEqual(old.read('OEBPS/publisher.css'), new.read('OEBPS/publisher.css'))
            soup = BeautifulSoup(new.read('OEBPS/chapter.xhtml'), 'html.parser')
            self.assertEqual(soup.pre.get_text(), '  x = 1\n\n    run(x)')
            self.assertIn('epub-image', soup.img['class'])
            self.assertEqual(len(soup.select('.reading-table-wrap')), 1)
        # Applying the same layout again does not create nested wrappers.
        optimize_epub(output, output, check=False)
        with zipfile.ZipFile(output) as z:
            soup = BeautifulSoup(z.read('OEBPS/chapter.xhtml'), 'html.parser')
            self.assertEqual(len(soup.select('.reading-table-wrap')), 1)

    @unittest.skipUnless(os.environ.get('EPUB_REVIEW_BROWSER'), 'Set EPUB_REVIEW_BROWSER for real Chromium verification')
    def test_browser_rendering_and_sampling(self):
        output = self.work / 'out.epub'
        optimize_epub(self.source, output, check=False)
        report = review_epub(output, self.work / 'review', browser_executable=os.environ['EPUB_REVIEW_BROWSER'], spread=1, scan_all=True)
        self.assertFalse(report['render_errors'])
        self.assertEqual(report['issue_count'], 0)
        self.assertEqual(report['status'], 'awaiting_agent_review')
        self.assertTrue((self.work / 'review' / 'gallery.html').exists())
        body = next(s for s in report['screenshots'] if s['category'] == 'body-pair' and s['width'] == 420)
        self.assertFalse(body['metrics']['pageOverflow'])
        self.assertGreater(body['metrics']['images'][0]['width'], 200)
        self.assertGreater(body['metrics']['images'][1]['width'], 180)
        self.assertGreater(body['metrics']['images'][2]['height'], 72)
        self.assertTrue(any(s['category'] == 'image-wide-scroll-right' and s['width'] == 420 for s in report['screenshots']))
        self.assertEqual(body['metrics']['images'][3]['width'], 160)
        wide = next(s for s in report['screenshots'] if s['category'] == 'body-pair' and s['width'] == 1050)
        self.assertEqual(wide['metrics']['images'][3]['width'], 160)
        code = next(s for s in report['screenshots'] if s['category'] == 'code')
        self.assertEqual(code['metrics']['code'][0]['whiteSpace'], 'pre')


if __name__ == '__main__':
    unittest.main()
