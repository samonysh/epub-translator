import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

from bs4 import BeautifulSoup


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "translate_epub.py"
SPEC = importlib.util.spec_from_file_location("translate_epub", SCRIPT)
translator = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(translator)


class InlineCodeTranslationTests(unittest.TestCase):
    def test_inline_code_is_model_visible_and_restored_with_source_style(self):
        soup = BeautifulSoup(
            '<p class="Para">Call <code class="literal">client.connect()</code> before retrying.</p>',
            "html.parser",
        )
        source = soup.p
        text = translator.node_translation_text(source)
        self.assertEqual(
            text,
            'Call <code data-epub-inline-code="0">client.connect()</code> before retrying.',
        )

        translator.insert_translation_node(
            soup,
            source,
            '在重试前调用 <code data-epub-inline-code="0">client.connect()</code>。',
        )
        translated = source.find_next_sibling("p")
        self.assertIsNotNone(translated)
        self.assertEqual(translated.code.get_text(), "client.connect()")
        self.assertEqual(translated.code["class"], ["literal"])
        self.assertEqual(translated.get_text(), "在重试前调用 client.connect()。")

    def test_preformatted_code_is_not_included_in_paragraph_text(self):
        soup = BeautifulSoup('<div><pre><code>x = 1</code></pre><p>Result: <code>x</code>.</p></div>', "html.parser")
        self.assertEqual(
            translator.node_translation_text(soup.p),
            'Result: <code data-epub-inline-code="0">x</code> .',
        )

    def test_inline_code_url_is_not_hidden_by_url_protection(self):
        text = 'Open <code data-epub-inline-code="0">https://example.test</code> now.'
        self.assertEqual(translator.protect(text), (text, []))

    def test_dropped_marker_still_preserves_inline_code(self):
        soup = BeautifulSoup('<p>Call <code class="literal">client.connect()</code>.</p>', "html.parser")
        translator.insert_translation_node(soup, soup.p, "请调用客户端。")
        translated = soup.p.find_next_sibling("p")
        self.assertEqual(translated.get_text(), "请调用客户端。 client.connect()")
        self.assertEqual(translated.code["class"], ["literal"])

    def test_old_custom_prompt_gets_inline_code_protocol_rule(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "api_key": "test-key",
                        "base_url": "https://api.example.test/v1",
                        "model_name": "test-model",
                        "system_prompt": "Translate precisely.",
                    }
                ),
                encoding="utf-8",
            )
            loaded = translator.load_config(config_path)
        self.assertIn("data-epub-inline-code", loaded["system_prompt"])


class ReasoningControlTests(unittest.TestCase):
    def setUp(self):
        self.original_config = translator.CONFIG

    def tearDown(self):
        translator.CONFIG = self.original_config

    def _kwargs(self, base_url, model_name="test-model", provider="auto", mode="disabled"):
        translator.CONFIG = {
            "base_url": base_url,
            "model_name": model_name,
            "reasoning_provider": provider,
            "reasoning_mode": mode,
        }
        return translator._reasoning_kwargs()

    def test_documented_provider_dialects_disable_thinking(self):
        self.assertEqual(
            self._kwargs("https://api.deepseek.com"),
            {"extra_body": {"thinking": {"type": "disabled"}}},
        )
        self.assertEqual(
            self._kwargs("https://dashscope.aliyuncs.com/compatible-mode/v1"),
            {"extra_body": {"enable_thinking": False}},
        )
        self.assertEqual(
            self._kwargs("https://open.bigmodel.cn/api/paas/v4"),
            {"extra_body": {"thinking": {"type": "disabled"}}},
        )
        self.assertEqual(
            self._kwargs("https://ark.cn-beijing.volces.com/api/v3"),
            {"extra_body": {"thinking": {"type": "disabled"}}},
        )

    def test_openai_uses_none_when_supported_and_low_when_not(self):
        self.assertEqual(
            self._kwargs("https://api.openai.com/v1", "gpt-5.6"),
            {"reasoning_effort": "none"},
        )
        self.assertEqual(
            self._kwargs("https://api.openai.com/v1", "gpt-5"),
            {"reasoning_effort": "low"},
        )
        self.assertEqual(self._kwargs("https://api.openai.com/v1", "gpt-4.1"), {})

    def test_unknown_gateway_can_use_explicit_provider_override(self):
        self.assertEqual(self._kwargs("https://gateway.example/v1"), {})
        self.assertEqual(
            self._kwargs("https://gateway.example/v1", provider="qwen"),
            {"extra_body": {"enable_thinking": False}},
        )


class FormulaDetectionTests(unittest.TestCase):
    def test_snake_case_prose_alt_is_not_a_formula(self):
        self.assertFalse(
            translator.has_math_formula(
                "Diagram illustrating a star schema, highlighting the central "
                "fact_sales table connected to dimension tables."
            )
        )
        self.assertFalse(
            translator.has_math_formula("Call the get_user_name helper first.")
        )

    def test_real_tex_and_subscripts_remain_formulas(self):
        self.assertTrue(translator.has_math_formula(r"\sum_{i=1}^{n} x_i"))
        self.assertTrue(translator.has_math_formula("x_{i}"))
        self.assertTrue(translator.has_math_formula("x_i"))
        self.assertTrue(translator.has_math_formula("v^2"))
        self.assertTrue(translator.has_math_formula("E = mc²"))

    def test_short_markers_still_count_as_formula(self):
        self.assertTrue(translator.has_math_formula("1"))
        self.assertTrue(translator.has_math_formula("x"))

    def test_empty_and_placeholder_alt_are_not_formulas(self):
        self.assertFalse(translator.has_math_formula(""))
        self.assertFalse(translator.has_math_formula("   "))
        self.assertFalse(translator.has_math_formula("{%}"))


if __name__ == "__main__":
    unittest.main()
