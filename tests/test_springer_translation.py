"""Springer/Apress 章节结构的翻译回归检查。"""
import importlib.util
import unittest
from pathlib import Path

from bs4 import BeautifulSoup


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("translate_epub", ROOT / "scripts" / "translate_epub.py")
translator = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(translator)


class SpringerTranslationTests(unittest.TestCase):
    def test_caption_is_collected_even_inside_skipped_container(self):
        soup = BeautifulSoup(
            '<div class="Caption"><span class="CaptionContent">Figure 7-1 A publishing timeline</span></div>',
            "html.parser",
        )
        units, overrides = translator.collect_units_in_html(soup)
        caption = soup.select_one(".CaptionContent")
        self.assertIn(caption, units)
        self.assertIn("Figure 7-1", overrides[id(caption)])

    def test_publisher_monospace_span_is_model_visible_and_restored(self):
        soup = BeautifulSoup(
            '<p>Run <span class="EmphasisFontCategoryNonProportional">make build</span> now.</p>',
            "html.parser",
        )
        text = translator.node_translation_text(soup.p)
        self.assertIn('<code data-epub-inline-code="0">make build</code>', text)
        translated = soup.new_tag("p")
        translator.set_translated_contents(
            soup, translated, '现在运行 <code data-epub-inline-code="0">make build</code>。', soup.p
        )
        self.assertEqual(translated.span.get_text(), "make build")
        self.assertIn("EmphasisFontCategoryNonProportional", translated.span["class"])

    def test_nested_list_lead_is_separate_from_subitems(self):
        soup = BeautifulSoup(
            '<ul><li>First, prepare the build:<ul><li>Install the compiler.</li></ul></li></ul>',
            "html.parser",
        )
        units, overrides = translator.collect_units_in_html(soup)
        outer = soup.select_one("ul > li")
        self.assertIn(outer, units)
        self.assertIn("First, prepare the build", overrides[id(outer)])
        self.assertNotIn("Install the compiler", overrides[id(outer)])

    def test_quote_normalizer_uses_current_dom_nodes(self):
        soup = BeautifulSoup('<p>「示例」和 "配置"</p>', "html.parser")
        translator.normalize_cjk_quotes(soup.p)
        self.assertEqual(soup.p.get_text(), '“示例”和 “配置”')


if __name__ == "__main__":
    unittest.main()
