# epub-translator

> Version: 1.2.0 · Last updated: 2026-08-29

Translate English EPUBs **paragraph by paragraph into Chinese**, placing each Chinese
translation directly under the original English paragraph to form a bilingual layout
("English on top, Chinese below"). Built on any OpenAI-compatible model (DeepSeek / OpenAI /
Qwen / GLM / self-hosted gateway), with a thread pool for parallel dispatch, cache
deduplication and resume support.

- **Batch JSON translation protocol**: multiple paragraphs are packed into one request as
  `[{"id":"<8-char>","text":"..."}]` and the model returns `[{"id":"...","zh":"..."}]`; the
  id is a per-batch sequential 8-char base36 code for exact pairing. This amortizes the
  system-prompt cost and cuts the number of requests by 1-2 orders of magnitude.
- **Dynamic batching**: batch size is tuned by a CJK-aware token estimate (default ~12000
  input tokens per batch, well within a 128K-context window); the budget auto-shrinks when
  there are too few batches to keep the thread pool saturated.
- Code blocks are never translated; kept verbatim.
- Formulas (MathML / LaTeX-bearing `<img>` / inline `$...$`, block `$$...$$`) keep their
  styling and are skipped as a whole.
- Tables: a full Chinese copy of the table is appended below the original (two-table
  stacked view); the original table stays intact.
- Paragraph-level bilingual layout: body text is English followed by Chinese; headings and
  EPUB3 TOC entries use an inline `English · Chinese` format.
- Both EPUB3 navigation documents and EPUB2 NCX labels are translated; NCX parsing does not
  require `lxml`.
- Built-in reader stylesheet: LXGW WenKai for both languages with matched rhythm, plain
  Chinese translations, body-size LXGW WenKai Mono code blocks with borders, and
  reader-safe white background/black text, formula-image, table, and responsive-image rules.
- Images are deduplicated by resource across the book; illustrations are centred and capped
  at 82% width / 65vh height while formula images keep their special handling.
- The finished EPUB is named automatically from the translated OPF title, with Windows-unsafe
  filename characters removed.
- **Robust parsing & fallback**: strips code fences, falls back to the outermost `[...]`,
  tolerates object-wrapped responses; id validation; whole-batch exponential-backoff retry
  plus per-item fallback for missing ids.

## When to use

- You have an English (or other foreign-language) EPUB and want it translated to Chinese,
  made into a bilingual edition, or "localized".
- You want to keep the original text for reference while adding Chinese translations
  (not replacing the original).

## Directory layout (follows the SkillHub recommended Skill format)

```
epub-translator/
├── SKILL.md                  # The only required file: YAML metadata + Markdown instructions
├── README.md / README.en.md  # CN / EN docs
├── config.example.json       # Config template (committed, placeholder, safe)
├── config.json               # Real config (gitignored, contains API key, never commit)
├── scripts/
│   └── translate_epub.py     # Main: unpack -> extract -> parallel translate -> write back -> repack
├── assets/
│   └── translated.css        # Built-in bilingual reader stylesheet
└── tests/
    └── test_inline_code.py   # Unit tests for inline-code protection
```

## Configuration (OpenAI-compatible, no hardcoded keys)

All sensitive / service fields live in `config.json`; upper-case environment variables
override individual fields with highest priority. The repo ships only the
`config.example.json` template:

```jsonc
{
    "model_name": "deepseek-v4-flash",
    "api_key":    "sk-YOUR_API_KEY_HERE",   // prefer the API_KEY env var
    "base_url":   "https://api.deepseek.com", // without /chat/completions
    "reasoning_mode": "disabled",            // disable thinking for translation by default
    "reasoning_provider": "auto",            // auto / openai / deepseek / qwen / zhipu / ark / generic
    "extra_body": {},
    "batch_tokens": 12000,                   // per-batch token budget (input side, optional)
    "system_prompt": "..."                   // prompt for the batch JSON protocol
}
```

Before first use, copy the template and fill in your key (or leave it empty and inject via
environment variables): `cp config.example.json config.json`. On PowerShell, use
`Copy-Item config.example.json config.json`. File lookup priority is: `--config` >
`config.json` in `scripts/` > `config.json` in the project root or current directory >
`config.example.json`. Environment variables override fields only; they do not choose the
configuration file.

> `batch_tokens` can also be overridden via the CLI flag `--batch-tokens` or the
> `BATCH_TOKENS` env var. If you customize `system_prompt`, it must match the batch protocol
> (input `[{"id","text"}]`, output `[{"id","zh"}]`) or the script cannot parse the reply.

### Reasoning control

Translation disables thinking by default. Supported OpenAI models receive reasoning_effort:
none (or low for older reasoning models); DeepSeek, Ark, and Zhipu receive thinking.type:
disabled; Qwen receives enable_thinking: false. The script overrides conflicting fields in
extra_body.

reasoning_provider defaults to auto and is inferred from base_url. A self-hosted or
aggregation gateway can explicitly select openai, deepseek, qwen, zhipu, ark, or generic;
unknown gateways receive no speculative reasoning field. REASONING_MODE and
REASONING_PROVIDER provide temporary overrides.

## Dependencies

```bash
pip install openai beautifulsoup4
```

`lxml` is optional: it enables a more forgiving XML parser but is not required at runtime.

## Usage

```bash
# The second argument selects the output directory; the filename is translated automatically.
python scripts/translate_epub.py input.epub output/placeholder.epub
```

Optional flags:

- `--config <path>`: explicitly select a configuration file.
- `--concurrency <n>`: concurrent requests; default `64`.
- `--batch-tokens <n>`: input-side token budget for each batch; defaults to the config
  value or `12000`.
- `--work-dir <path>`: directory for temporary extraction and cache files.
- `--resume`: reuse `cache.json` from the work directory; the source EPUB is still
  unpacked again to prevent duplicate translations.

PowerShell example:

```powershell
python scripts/translate_epub.py input.epub output/placeholder.epub --config config.json --concurrency 64 --batch-tokens 12000 --resume
```

### Request-count reduction from batching

A ~3300-paragraph book (avg 141 chars) used to need ~3300 API calls (one per paragraph).
With the batch protocol at ~12000 input tokens per batch (~90-120 paragraphs), the total
drops to **~30 requests** (1-2 orders of magnitude), and the system-prompt overhead shrinks
from ~45% of total input to <5%. Throughput still scales near-linearly with concurrency:

| Concurrency | Time | Rate (para/s) |
|---|---|---|
| 16 | 15.5s | ~12.9 |
| 64 | 4.0s  | ~49.5 |
| 96 | 3.2s  | ~62.8 |

> The speedup ceiling depends on API-side concurrency/rate limits; if a gateway is unstable
> on long outputs, lower `--batch-tokens` (e.g. 6000-8000); missing items are retried
> individually.

## Security notes

- `config.json` holds your real API key and is excluded by `.gitignore` — **never commit it**.
- Recommended: store only `model_name` / `base_url` / `extra_body` / `system_prompt` in
  `config.json`; inject `api_key` via the `API_KEY` environment variable.
- Translating copyrighted ebooks (Kindle/O'Reilly/Apress, etc.) you obtained legitimately
  is for personal study only; do not redistribute.

## Output and compatibility

- The EPUB is written to the directory supplied by the second argument and named after the
  translated OPF title, for example `Python 专家编程（第四版）.epub`.
- The reader stylesheet is already built in. Apply any font subsetting or reader-specific
  fixes directly in this project when they are needed.
- `.gitignore` excludes real configuration, generated EPUBs, caches, and both temporary
  working-directory patterns.

## Tests

```bash
python -m unittest discover -s tests -v
```

For the full translation-unit rules, publisher adaptation notes, the system prompt,
placeholder protection, and error handling, see [SKILL.md](SKILL.md).
