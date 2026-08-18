# epub-translator

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
  side-by-side view); the original table stays intact.
- Paragraph-level bilingual contrast: insert a same-tag Chinese node right after each
  source node, tagged with `class="translated-zh"`.
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
└── assets/
    └── translated.css        # Base CSS for bilingual paragraphs (.translated-zh)
```

## Configuration (OpenAI-compatible, no hardcoded keys)

All sensitive / service fields live in `config.json` or are overridden by upper-case
environment variables (highest priority). The repo ships only the `config.example.json`
template:

```jsonc
{
    "model_name": "deepseek-v4-flash",
    "api_key":    "sk-YOUR_API_KEY_HERE",   // prefer the API_KEY env var
    "base_url":   "https://api.deepseek.com", // without /chat/completions
    "extra_body": {},
    "batch_tokens": 12000,                   // per-batch token budget (input side, optional)
    "system_prompt": "..."                   // prompt for the batch JSON protocol
}
```

Before first use: `cp config.example.json config.json` and fill in your key (or leave it
empty and inject via env). Lookup priority: `--config` > env vars > `config.json` next to
/parent of `scripts/` > `config.example.json`.

> `batch_tokens` can also be overridden via the CLI flag `--batch-tokens` or the
> `BATCH_TOKENS` env var. If you customize `system_prompt`, it must match the batch protocol
> (input `[{"id","text"}]`, output `[{"id","zh"}]`) or the script cannot parse the reply.

## Dependencies

```bash
pip install openai beautifulsoup4 lxml
```

(For the formula-as-image font subsetting flow, also `pip install fonttools brotli`, see
epub-reader-optimizer.)

## Usage

```bash
# Basic
python scripts/translate_epub.py input.epub output.epub

# Optional flags
python scripts/translate_epub.py input.epub output.epub \
    --concurrency 64 \     # default 64; up to 96 still scales near-linearly
    --batch-tokens 12000 \ # per-batch token budget (input side, default 12000)
    --work-dir <path> \
    --resume               # resume: reuse cache.json
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

## Required next step

After translating and repacking, call the `epub-reader-optimizer` skill for final
beautification (Chinese fonts, `.translated-zh` color / left rule, bilingual table
contrast, white-text fix, etc.).

For the full translation-unit rules, publisher adaptation notes, the system prompt,
placeholder protection, and error handling, see [SKILL.md](SKILL.md).