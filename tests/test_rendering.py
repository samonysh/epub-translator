"""代码排版与样式注入的端到端回归检查。"""
import importlib.util
import shutil
import unittest
from pathlib import Path

from bs4 import BeautifulSoup


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("translate_epub", ROOT / "scripts" / "translate_epub.py")
translator = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(translator)


class CodeRenderingTests(unittest.TestCase):
    def test_code_lines_are_contiguous_and_indentation_survives(self):
        soup = BeautifulSoup(
            '<div class="ProgramCode">\n<div class="LineGroup">\n'
            '<div class="FixedLine">  first()</div>\n'
            '<div class="FixedLine">  second()</div>\n'
            '</div>\n</div><pre><code>if ready:\n\n    run()\n\n    done()</code></pre>',
            "html.parser",
        )
        translator.normalise_code_blocks(soup)
        self.assertEqual(len(soup.select(".LineGroup > .FixedLine")), 2)
        self.assertEqual(soup.select_one(".LineGroup").get_text("|"), "  first()|  second()")
        self.assertEqual(soup.pre.code.get_text(), "if ready:\n    run()\n    done()")

    def test_styles_have_one_unframed_block_and_natural_image_size(self):
        css = (ROOT / "assets" / "translated.css").read_text(encoding="utf-8")
        self.assertIn("border: 0 !important; border-radius: 0 !important", css)
        self.assertIn("pre code, .ProgramCode code", css)
        self.assertIn(".LineGroup, .FixedLine", css)
        self.assertNotIn("border: 1px solid #aeb7c2", css)
        self.assertNotRegex(css, r"(?m)^\s*width: 85% !important")
        self.assertIn("li.translated-zh { display: block !important; list-style: none !important", css)
        self.assertIn(".translated-noop { display: none !important; }", css)

    def test_cloned_table_does_not_duplicate_anchor_ids(self):
        soup = BeautifulSoup('<table id="t1"><tr><td id="cell1">Source text</td></tr></table>', "html.parser")
        translator.CACHE["Source text"] = "译文"
        try:
            translator.translate_table_node(soup, soup.table)
            self.assertEqual([tag.get("id") for tag in soup.find_all(id=True)], ["t1", "cell1"])
            self.assertEqual(len(soup.find_all("table")), 2)
        finally:
            translator.CACHE.pop("Source text", None)

    def test_css_injection_keeps_code_whitespace_and_is_idempotent(self):
        work = ROOT / ".test-scratch" / "css-injection"
        try:
            work.mkdir(parents=True, exist_ok=True)
            chapter = work / "chapter.xhtml"
            opf = work / "content.opf"
            source = ('<html xmlns="http://www.w3.org/1999/xhtml"><head><title>T</title></head>'
                      '<body><pre><code>  first()\n\n    second()</code></pre></body></html>')
            chapter.write_text(source, encoding="utf-8")
            opf.write_text('<package><manifest><item id="chapter" href="chapter.xhtml"/></manifest></package>', encoding="utf-8")
            translator.inject_css(work)
            once = chapter.read_text(encoding="utf-8")
            self.assertIn('<pre><code>  first()\n\n    second()</code></pre>', once)
            self.assertIn('href="translated.css"', once)
            translator.inject_css(work)
            self.assertEqual(chapter.read_text(encoding="utf-8"), once)
            self.assertEqual(opf.read_text(encoding="utf-8").count('id="translated-css"'), 1)
        finally:
            shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
