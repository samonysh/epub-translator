import importlib.util
import unittest
from pathlib import Path

from bs4 import BeautifulSoup


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "translate_epub.py"
SPEC = importlib.util.spec_from_file_location("translate_epub", SCRIPT)
translator = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(translator)


class InlineCodeTranslationTests(unittest.TestCase):
    def test_inline_code_is_sent_as_placeholder_and_restored_as_code(self):
        soup = BeautifulSoup(
            '<p class="Para">Call <code class="literal">client.connect()</code> before retrying.</p>',
            "html.parser",
        )
        source = soup.p
        text = translator.node_translation_text(source)
        self.assertEqual(text, "Call ⟦KEEP_0⟧ before retrying.")

        translator.insert_translation_node(soup, source, "在重试前调用 ⟦KEEP_0⟧。")
        translated = source.find_next_sibling("p")
        self.assertIsNotNone(translated)
        self.assertEqual(translated.code.get_text(), "client.connect()")
        self.assertEqual(translated.code["class"], ["literal"])
        self.assertEqual(translated.get_text(), "在重试前调用 client.connect()。")

    def test_preformatted_code_is_not_included_in_paragraph_text(self):
        soup = BeautifulSoup('<div><pre><code>x = 1</code></pre><p>Result: <code>x</code>.</p></div>', "html.parser")
        self.assertEqual(translator.node_translation_text(soup.p), "Result: ⟦KEEP_0⟧ .")


if __name__ == "__main__":
    unittest.main()
