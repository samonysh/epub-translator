"""使用真实 EPUBCheck 验证成品检查接口与报告保存。"""

import importlib.util
import json
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("translate_epub", ROOT / "scripts" / "translate_epub.py")
translator = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(translator)


def make_epub(path: Path, *, broken: bool = False) -> None:
    """生成无需翻译 API 的最小 EPUB 3 样本；损坏样本含无效 XHTML。"""
    chapter = (
        '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Chapter</title></head>'
        '<body><p>Hello.</p></body></html>'
    )
    if broken:
        chapter = chapter.replace("</p>", "")
    files = {
        "META-INF/container.xml": (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
            '<rootfiles><rootfile full-path="OEBPS/content.opf" '
            'media-type="application/oebps-package+xml"/></rootfiles></container>'
        ),
        "OEBPS/content.opf": (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id">'
            '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
            '<dc:identifier id="id">urn:uuid:12345678-1234-1234-1234-123456789abc</dc:identifier>'
            '<dc:title>Sample</dc:title><dc:language>en</dc:language>'
            '<meta property="dcterms:modified">2026-01-01T00:00:00Z</meta></metadata>'
            '<manifest><item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>'
            '<item id="chapter" href="chapter.xhtml" media-type="application/xhtml+xml"/></manifest>'
            '<spine><itemref idref="chapter"/></spine></package>'
        ),
        "OEBPS/nav.xhtml": (
            '<html xmlns="http://www.w3.org/1999/xhtml" '
            'xmlns:epub="http://www.idpf.org/2007/ops"><head><title>Contents</title></head>'
            '<body><nav epub:type="toc"><h1>Contents</h1><ol>'
            '<li><a href="chapter.xhtml">Chapter</a></li></ol></nav></body></html>'
        ),
        "OEBPS/chapter.xhtml": chapter,
    }
    with zipfile.ZipFile(path, "w") as book:
        book.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        for name, content in files.items():
            book.writestr(name, content, compress_type=zipfile.ZIP_DEFLATED)


@unittest.skipUnless(shutil.which("java") and importlib.util.find_spec("epubcheck"),
                     "需要 Java 与 PyPI epubcheck")
class EpubCheckIntegrationTests(unittest.TestCase):
    def test_valid_and_broken_epub_have_distinct_results_and_reports(self):
        with tempfile.TemporaryDirectory() as directory:
            for broken in (False, True):
                with self.subTest(broken=broken):
                    path = Path(directory) / ("broken.epub" if broken else "valid.epub")
                    make_epub(path, broken=broken)
                    valid, report, fatal, errors, warnings = translator.validate_epub(path)
                    self.assertEqual(valid, not broken)
                    self.assertTrue(report.is_file())
                    data = json.loads(report.read_text(encoding="utf-8"))
                    self.assertEqual(data["checker"]["nError"], errors)
                    self.assertEqual(data["checker"]["nWarning"], warnings)
                    self.assertEqual(data["checker"]["nFatal"], fatal)
                    if broken:
                        self.assertGreater(fatal + errors, 0)


if __name__ == "__main__":
    unittest.main()
