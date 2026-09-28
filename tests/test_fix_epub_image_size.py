"""Repair tool for already-generated EPUBs: CSS rewrite, re-classing, zip safety."""
import importlib.util
import json
import shutil
import struct
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRATCH = ROOT / ".test-scratch" / "repair"


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


fix = load("fix_epub_image_size", ROOT / "scripts" / "fix_epub_image_size.py")

LEGACY_ONELINE = (
    "img.epub-image { display: block; width: auto !important; max-width: 82% !important; "
    "max-height: 65vh; margin: 0.9em auto; }\n"
    "img.epub-inline-image { max-width: 1.5em !important; max-height: 1.5em; }\n"
)
GENERIC_BLOCK = """
/* 独立插图：居中，最大宽度 82%，最大高度 65vh */
div.figure img,
div.informalfigure img,
figure img,
div.mediaobject img,
img.epub-image {
  display: block !important;
  width: auto !important;
  max-width: 82% !important;
  max-height: 65vh !important;
  margin: 0.9em auto !important;
}
/* figure 容器居中 */
div.figure, div.informalfigure, figure, div.mediaobject, .epub-image-container {
  margin: 1em auto !important;
}
.epub-image-container { text-align: center; }
"""


def gif_bytes(width: int, height: int) -> bytes:
    return b"GIF89a" + struct.pack("<HH", width, height) + b"\x00" * 16


class BlockDriftTests(unittest.TestCase):
    def test_embedded_block_matches_the_asset(self):
        asset = (ROOT / "assets" / "translated.css").read_text(encoding="utf-8")
        start = asset.index(fix.UNIFIED_MARKER)
        end = asset.index('figure[class*="width-"]', start)
        end = asset.index("}", end) + 1
        canonical = asset[start:end].strip()
        self.assertEqual(canonical, fix.UNIFIED_BLOCK.strip())
        self.assertEqual(fix.load_unified_block().strip(), fix.UNIFIED_BLOCK.strip())


class CssPatchTests(unittest.TestCase):
    def test_legacy_oneline_is_replaced_by_the_unified_block(self):
        new, notes = fix.patch_css(LEGACY_ONELINE)
        self.assertIn("width: 85% !important", new)
        self.assertNotIn("82%", new)
        self.assertNotIn("65vh", new)
        self.assertIn("baseline epub-image rule -> unified block", notes)
        self.assertEqual(new.count(fix.UNIFIED_MARKER), 1)
        self.assertFalse(fix.css_needs_patch(new))

    def test_generic_figure_block_is_raised_not_removed(self):
        new, notes = fix.patch_css(GENERIC_BLOCK)
        self.assertIn("generic figure block -> 85%", notes)
        self.assertIn("max-width: 85% !important", new)
        self.assertIn("figure img", new)          # still caps unclassified figures
        self.assertIn("div.figure, div.informalfigure, figure", new)  # centring kept
        self.assertIn(".epub-image-container { text-align: center; }", new)
        self.assertIn("最大宽度 85%", new)
        self.assertNotIn("82%", new)

    def test_global_img_cap_is_raised(self):
        new, notes = fix.patch_css(
            "/* Images */\nimg { max-width: 82% !important; max-height: 65vh !important; }\n"
        )
        self.assertIn("global img cap 82%/65vh -> 85%/96vh", notes)
        self.assertIn("max-width: 85% !important", new)
        self.assertIn("max-height: 96vh !important", new)

    def test_publisher_rules_are_untouched(self):
        css = ".Figure img { height: auto; margin: 0; max-width: 100%; padding: 2px; }\n"
        new, notes = fix.patch_css(css, add_block=False)
        self.assertEqual(new, css)
        self.assertEqual(notes, [])

    def test_comment_braces_do_not_break_rule_parsing(self):
        css = (
            "/* publisher CSS overrides `img { max-width: 95% }` here */\n"
            + LEGACY_ONELINE
            + ".Figure img { height: auto; max-width: 100%; }\n"
        )
        new, _ = fix.patch_css(css)
        # the publisher rule after the legacy block must survive untouched
        self.assertIn(".Figure img { height: auto; max-width: 100%; }", new)
        self.assertIn("width: 85% !important", new)
        self.assertNotIn("82%", new)

    def test_patch_is_idempotent(self):
        once, _ = fix.patch_css(GENERIC_BLOCK + LEGACY_ONELINE)
        twice, _ = fix.patch_css(once)
        self.assertEqual(once, twice)
        self.assertEqual(twice.count(fix.UNIFIED_MARKER), 1)


class HtmlPatchTests(unittest.TestCase):
    def resolve(self, size):
        return lambda src: size

    def test_figure_illustration_is_upgraded(self):
        html = ('<figure><div class="figure">'
                '<img alt="Deployment diagram of the service" class="ImageStyle3 epub-inline-image" src="a.png"/>'
                "<h6>Figure 1-1. Deployment overview</h6></div></figure>")
        new, up, down, fixed, formula = fix.patch_html(html, self.resolve((1166, 853)))
        self.assertEqual((up, down, fixed, formula), (1, 0, 0, 0))
        self.assertIn('class="ImageStyle3 epub-image"', new)
        self.assertNotIn("epub-inline-image", new)

    def test_stale_formula_class_on_a_flow_chart_is_corrected(self):
        alt = ("A flow chart explaining a data processing pipeline that transforms raw "
               "events into experiment results for the analysis dashboard.")
        html = f'<div class="MediaObject"><img alt="{alt}" class="ImageStyle4 formula-img" src="f.png"/></div>'
        new, up, down, fixed, formula = fix.patch_html(html, self.resolve((1692, 900)))
        self.assertEqual((up, down, fixed, formula), (0, 0, 0, 1))
        self.assertIn("epub-image", new)
        self.assertNotIn("formula-img", new)
        # captions that quote code/maths excerpts still count as captions
        code_alt = ("Diagram showing different data representations of a bank account message. "
                    "On the left a C struct defines Bank_account_message with fields, followed "
                    'by an instance with holder "Rumpelstiltskin" and a comment { x<0,1 }.')
        self.assertTrue(fix._caption_alt(code_alt))
        code_html = f'<div class="MediaObject"><img alt="{code_alt}" class="ImageStyle8 formula-img" src="g.png"/></div>'
        self.assertEqual(fix.patch_html(code_html, self.resolve((1347, 948)))[4], 1)
        # a real formula image is untouched
        real = '<p><img alt="x_i" class="formula-img" src="e.png"/></p>'
        self.assertEqual(fix.patch_html(real, self.resolve((300, 60)))[0], real)
        self.assertFalse(fix._caption_alt(r"\sum_{i=1}^{n} x_i over the whole training corpus"))
        # MathML/SVG formulas are never touched even with a long alt
        markup = ('<math><img alt="A long caption that mentions a = b and c_d in prose." '
                  'class="formula-img" src="e.png"/></math>')
        self.assertEqual(fix.patch_html(markup, self.resolve((900, 400)))[0], markup)

    def test_unclassed_authored_size_image_is_classed(self):
        html = '<p><img alt="PIC" width="85" height="85" src="../media/file1.png"/></p>'
        new, up, down, fixed, formula = fix.patch_html(html, self.resolve((493, 493)))
        self.assertEqual((up, down, fixed, formula), (0, 0, 1, 0))
        self.assertIn('class="epub-fixed-image"', new)

    def test_tiny_placeholder_in_code_is_demoted(self):
        html = ('<div class="ProgramCode"><div class="FixedLine">"hello '
                '<img alt="A scatter plot showing a positive relationship." class="ImageStyle4 epub-image" src="b.gif"/>'
                ' world"</div></div>')
        new, up, down, fixed, formula = fix.patch_html(html, self.resolve((13, 13)))
        self.assertEqual((up, down, fixed, formula), (0, 1, 0, 0))
        self.assertIn("epub-inline-image", new)
        self.assertNotIn("epub-image", new.replace("epub-inline-image", ""))

    def test_formula_images_are_never_reclassed(self):
        html = '<p>Text <img alt="x_i" class="epub-inline-image" src="e.png"/> text.</p>'
        new, up, down, fixed, formula = fix.patch_html(html, self.resolve((18, 18)))
        self.assertEqual(new, html)
        self.assertEqual((up, down, fixed, formula), (0, 0, 0, 0))


def build_epub(path: Path) -> None:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as out:
        out.writestr(zipfile.ZipInfo("mimetype"), b"application/epub+zip", zipfile.ZIP_STORED)
        out.writestr("META-INF/container.xml", "<container/>")
        out.writestr("OEBPS/content.opf", "<package/>")
        out.writestr("OEBPS/translated.css", LEGACY_ONELINE)
        out.writestr(
            "OEBPS/ch01.xhtml",
            '<html><body><figure><div class="figure">'
            '<img class="ImageStyle3 epub-inline-image" src="images/fig.png" '
            'alt="Deployment diagram of the service layers"/>'
            "<h6>Figure 1-1. Deployment</h6></div></figure></body></html>",
        )
        out.writestr("OEBPS/images/fig.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)


class EpubRoundTripTests(unittest.TestCase):
    def setUp(self):
        if SCRATCH.exists():
            shutil.rmtree(SCRATCH, ignore_errors=True)
        SCRATCH.mkdir(parents=True)
        self.book = SCRATCH / "sample.epub"
        build_epub(self.book)
        self.backup_dir = SCRATCH / "backup"

    def tearDown(self):
        shutil.rmtree(SCRATCH, ignore_errors=True)

    def test_dry_run_changes_nothing(self):
        before = self.book.read_bytes()
        report = fix.process(self.book, self.backup_dir, dry_run=True)
        self.assertEqual(before, self.book.read_bytes())
        self.assertFalse(self.backup_dir.exists())
        self.assertEqual(report["upgraded"], 1)
        self.assertEqual(report["clamped_before"], 1)
        self.assertEqual(report["clamped_after"], 0)

    def test_real_run_repairs_css_and_classes_and_keeps_zip_sane(self):
        original_bytes = self.book.read_bytes()
        with zipfile.ZipFile(self.book) as zin:
            before = {i.filename: zin.read(i.filename) for i in zin.infolist()}
            order = zin.namelist()

        report = fix.process(self.book, self.backup_dir, dry_run=False)
        self.assertTrue(all(report["checks"].values()), report["checks"])
        self.assertEqual(report["upgraded"], 1)
        self.assertEqual(report["clamped_after"], 0)
        self.assertEqual((self.backup_dir / "sample.epub").read_bytes(), original_bytes)

        with zipfile.ZipFile(self.book) as zout:
            self.assertEqual(zout.namelist(), order)
            self.assertEqual(zout.namelist()[0], "mimetype")
            self.assertEqual(zout.getinfo("mimetype").compress_type, zipfile.ZIP_STORED)
            self.assertIsNone(zout.testzip())
            # untouched entries stay byte-identical
            self.assertEqual(zout.read("META-INF/container.xml"), before["META-INF/container.xml"])
            self.assertEqual(zout.read("OEBPS/images/fig.png"), before["OEBPS/images/fig.png"])
            css = zout.read("OEBPS/translated.css").decode("utf-8")
            self.assertIn("width: 85% !important", css)
            self.assertNotIn("82%", css)
            self.assertNotIn("65vh", css)
            html = zout.read("OEBPS/ch01.xhtml").decode("utf-8")
            self.assertIn("epub-image", html)
            self.assertNotIn("epub-inline-image", html)

    def test_second_run_is_idempotent_and_never_overwrites_backup(self):
        fix.process(self.book, self.backup_dir, dry_run=False)
        first = self.book.read_bytes()
        backup_first = (self.backup_dir / "sample.epub").read_bytes()
        fix.process(self.book, self.backup_dir, dry_run=False)
        self.assertEqual(first, self.book.read_bytes())
        self.assertEqual(backup_first, (self.backup_dir / "sample.epub").read_bytes())


if __name__ == "__main__":
    unittest.main()
