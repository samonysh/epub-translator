"""Illustration sizing: class assignment, pixel-size sniffing and the baseline CSS."""
import importlib.util
import shutil
import struct
import unittest
from pathlib import Path

from bs4 import BeautifulSoup


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "translate_epub.py"
SPEC = importlib.util.spec_from_file_location("translate_epub", SCRIPT)
translator = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(translator)


def gif_bytes(width: int, height: int) -> bytes:
    return b"GIF89a" + struct.pack("<HH", width, height) + b"\x00" * 16


def png_bytes(width: int, height: int) -> bytes:
    return b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + struct.pack(">II", width, height) + b"\x08\x06\x00\x00\x00"


class PixelSizeSniffingTests(unittest.TestCase):
    def test_png_and_gif_headers(self):
        self.assertEqual(translator._sniff_image_size(png_bytes(1166, 853)), (1166, 853))
        self.assertEqual(translator._sniff_image_size(gif_bytes(22, 22)), (22, 22))

    def test_unknown_payload_returns_none(self):
        self.assertIsNone(translator._sniff_image_size(b"not an image"))

    def test_size_is_resolved_relative_to_the_html_file(self):
        # A fixed scratch directory (not tempfile) keeps the test usable inside
        # restricted sandboxes where temp-dir cleanup cannot chmod.
        root = ROOT / ".test-scratch" / "images"
        try:
            (root / "text").mkdir(parents=True, exist_ok=True)
            (root / "images").mkdir(parents=True, exist_ok=True)
            (root / "images" / "big fig.png").write_bytes(png_bytes(900, 600))
            size = translator.image_pixel_size("../images/big%20fig.png", root / "text")
            self.assertEqual(size, (900, 600))
            self.assertIsNone(translator.image_pixel_size("missing.png", root / "text"))
        finally:
            shutil.rmtree(root, ignore_errors=True)


class AltProseTests(unittest.TestCase):
    def test_long_caption_is_illustration_prose(self):
        self.assertTrue(translator.alt_describes_illustration(
            'A chart compares two groups, labeled "Control" and "Treatment", '
            "showing their average scores on a test."
        ))

    def test_latex_and_short_labels_are_not_prose(self):
        self.assertFalse(translator.alt_describes_illustration(r"\sum_{i=1}^{n} x_i = \mu"))
        self.assertFalse(translator.alt_describes_illustration("x"))
        self.assertFalse(translator.alt_describes_illustration("Terminal icon"))
        self.assertFalse(translator.alt_describes_illustration(""))

    def test_publisher_placeholder_labels_are_not_formulas(self):
        for label in ("PIC", "pic", "image", "Image", "logo", "PIC."):
            self.assertFalse(translator.has_math_formula(label), label)
        # ... while genuine short maths symbols still are.
        for symbol in ("x", "1", "x_i", "v^2"):
            self.assertTrue(translator.has_math_formula(symbol), symbol)


class IllustrationClassificationTests(unittest.TestCase):
    def classify(self, html: str, size=None, selector="img"):
        soup = BeautifulSoup(html, "html.parser")
        return translator.classify_image(soup.select_one(selector), size)

    def test_captioned_div_figure_is_an_illustration(self):
        # The real O'Reilly/Springer shape that used to collapse to 24px.
        html = (
            '<figure><div class="figure" id="ch01fig01">'
            '<img alt="Deployment diagram of the service" src="a.png"/>'
            "<h6><span class=\"label\">Figure 1-1. </span>Deployment overview</h6>"
            "</div></figure>"
        )
        self.assertEqual(self.classify(html, (1166, 853)), translator.ILLUSTRATION_CLASS)

    def test_cover_containers_and_doc_cover_role(self):
        self.assertEqual(
            self.classify('<div class="CoverFigure"><img src="c.png" alt=""/></div>'),
            translator.ILLUSTRATION_CLASS,
        )
        self.assertEqual(
            self.classify('<p><img role="doc-cover" src="c.png" alt=""/></p>'),
            translator.ILLUSTRATION_CLASS,
        )

    def test_paragraph_illustration_with_caption_alt(self):
        alt = ("This flow chart explains a data processing pipeline that turns raw "
               "events into experiment results for the analysis dashboard.")
        self.assertEqual(
            self.classify(f'<div class="Para">Prose text <img alt="{alt}" src="f.png"/> more.</div>', (1200, 700)),
            translator.ILLUSTRATION_CLASS,
        )

    def test_tiny_placeholder_with_caption_alt_stays_inline(self):
        alt = ("A black diamond shape with a white question mark inside, centered on a "
               "gray background. The symbol suggests an unknown or missing image.")
        self.assertEqual(
            self.classify(f'<p class="Para">Text <img alt="{alt}" src="p.gif"/> text.</p>', (18, 18)),
            translator.GLYPH_CLASS,
        )

    def test_code_listing_glyphs_stay_inline(self):
        self.assertEqual(
            self.classify('<pre><code><img alt="" src="t.png"/></code></pre>', (16, 16)),
            translator.GLYPH_CLASS,
        )
        self.assertEqual(
            self.classify('<div class="ProgramCode"><div class="FixedLine">'
                          '<img alt="Star" src="t.png"/></div></div>', (58, 19)),
            translator.GLYPH_CLASS,
        )

    def test_large_captioned_image_inside_code_still_shows_as_illustration(self):
        alt = ("A scatter plot with a diagonal line from the bottom left to the top "
               "right, showing a positive relationship between two variables.")
        self.assertEqual(
            self.classify(f'<div class="FixedLine"><img alt="{alt}" src="f.png"/></div>', (900, 600)),
            translator.ILLUSTRATION_CLASS,
        )

    def test_unknown_size_falls_back_to_structure_and_alt(self):
        self.assertEqual(
            self.classify('<p><img alt="" src="u.png"/></p>', None),
            translator.GLYPH_CLASS,
        )
        self.assertEqual(
            self.classify('<figure><img alt="" src="u.png"/></figure>', None),
            translator.ILLUSTRATION_CLASS,
        )

    def test_large_image_in_a_plain_wrapper_is_still_an_illustration(self):
        self.assertEqual(
            self.classify('<div><img alt="" src="big.png"/></div>', (900, 600)),
            translator.ILLUSTRATION_CLASS,
        )
        # ... but a large glyph inside a sentence is not promoted without caption alt.
        self.assertEqual(
            self.classify('<p>see <img alt="" src="icon.png"/> here</p>', (200, 200)),
            translator.GLYPH_CLASS,
        )

    def test_small_resource_never_becomes_a_block(self):
        self.assertEqual(
            self.classify('<div><img alt="" src="tiny.gif"/></div>', (22, 22)),
            translator.GLYPH_CLASS,
        )

    def test_publisher_authored_size_is_honoured_in_a_paragraph(self):
        # Packt-style 85x85 QR code: crushed to ~21px by the p > img glyph cap.
        self.assertEqual(
            self.classify('<p><img alt="PIC" width="85" height="85" src="qr.png"/></p>', (493, 493)),
            translator.FIXED_SIZE_CLASS,
        )
        self.assertEqual(
            self.classify('<p><img alt="PIC" width="85px" height="85px" src="qr.png"/></p>', (493, 493)),
            translator.FIXED_SIZE_CLASS,
        )

    def test_authored_size_does_not_override_figure_or_formula_handling(self):
        # A captioned figure remains an illustration and keeps its intrinsic size.
        self.assertEqual(
            self.classify('<figure><img alt="" width="1166" height="853" src="f.png"/></figure>', (1166, 853)),
            translator.ILLUSTRATION_CLASS,
        )
        self.assertEqual(
            self.classify('<p><img alt="PIC" width="20" height="20" src="qr.png"/></p>', (22, 22)),
            translator.GLYPH_CLASS,
        )

    def test_inline_style_size_is_kept_within_page_cap(self):
        self.assertEqual(
            self.classify('<p><img alt="QR" style="width:25%" src="qr.png"/></p>', (493, 493)),
            translator.STYLED_SIZE_CLASS,
        )

    def test_figure_caption_with_math_symbols_is_not_formula(self):
        soup = BeautifulSoup(
            '<figure><img alt="Figure 6.1: This diagram compares an N×M network with an N+M network across systems." src="diagram.png"/></figure>',
            "html.parser",
        )
        translator.normalise_images_for_reading(soup, set())
        self.assertIn(translator.ILLUSTRATION_CLASS, soup.img["class"])
        self.assertNotIn("formula-img", soup.img["class"])


class NormaliseImagesTests(unittest.TestCase):
    def normalise(self, html: str, html_dir=None):
        soup = BeautifulSoup(html, "html.parser")
        translator.normalise_images_for_reading(soup, set(), html_dir=html_dir)
        return soup

    def test_figure_container_and_caption_are_marked(self):
        soup = self.normalise(
            '<figure><div class="figure"><img alt="Deployment diagram" src="a.png"/>'
            "<h6>Figure 1-1. Deployment overview</h6></div></figure>"
        )
        image = soup.find("img")
        self.assertIn("epub-image", image["class"])
        self.assertNotIn("epub-inline-image", image["class"])
        self.assertIn("epub-image-container", soup.find("div")["class"])

    def test_formula_images_are_left_alone(self):
        soup = self.normalise('<p>Text <img alt="x_i" src="e.png"/> text.</p>')
        self.assertIn("formula-img", soup.find("img")["class"])
        self.assertNotIn("epub-image", soup.find("img")["class"])

    def test_duplicate_resources_are_dropped_once(self):
        soup = self.normalise(
            '<div><figure><img alt="" src="same.png"/></figure>'
            '<figure><img alt="" src="same.png"/></figure></div>'
        )
        self.assertEqual(len(soup.find_all("img")), 1)


class BaselineStylesheetTests(unittest.TestCase):
    def setUp(self):
        self.css = (ROOT / "assets" / "translated.css").read_text(encoding="utf-8")

    def test_illustrations_are_capped_at_85_percent(self):
        self.assertIn("width: auto !important;", self.css)
        self.assertIn("max-width: 85% !important", self.css)
        self.assertNotRegex(self.css, r"(?m)^\s*width: 85% !important")
        self.assertNotIn("max-width: 82%", self.css)
        self.assertNotIn("65vh", self.css)

    def test_container_rescue_and_inline_cap_are_both_present(self):
        self.assertIn("figure img.epub-inline-image", self.css)
        self.assertIn("div.figure img.epub-inline-image", self.css)
        self.assertIn(
            "img.epub-inline-image { width: auto !important; max-width: 1.5em !important;",
            self.css,
        )

    def test_code_glyph_heading_and_width_n_rules_exist(self):
        self.assertIn("pre img.epub-inline-image", self.css)
        self.assertIn("h1 img.epub-image, h1 img.epub-inline-image", self.css)
        self.assertIn('figure[class*="width-"], div[class*="width-"]', self.css)

    def test_authored_size_rule_exists(self):
        self.assertIn("img.epub-fixed-image {", self.css)
        self.assertIn("img.epub-styled-image {", self.css)
        self.assertIn("max-height: none !important", self.css)


if __name__ == "__main__":
    unittest.main()
