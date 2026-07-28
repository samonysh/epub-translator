# epub-translator

Translate English EPUBs **paragraph by paragraph into Chinese**, placing each Chinese
translation directly under the original English paragraph to form a bilingual layout
("English on top, Chinese below"). Built on any OpenAI-compatible model (DeepSeek / OpenAI /
Qwen / GLM / self-hosted gateway), with a thread pool for parallel dispatch, cache
deduplication and resume support.

- Code blocks are never translated; kept verbatim.
- Formulas (MathML / LaTeX-bearing `<img>` / inline `$...$`, block `$$...$$`) keep their
  styling and are skipped as a whole.
- Tables: a full Chinese copy of the table is appended below the original (two-table
  side-by-side view); the original table stays intact.
- Paragraph-level bilingual contrast: insert a same-tag Chinese node right after each
  source node, tagged with `class="translated-zh"`.

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
    "system_prompt": "..."
}
```

Before first use: `cp config.example.json config.json` and fill in your key (or leave it
empty and inject via env). Lookup priority: `--config` > env vars > `config.json` next to
/parent of `scripts/` > `config.example.json`.

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
    --work-dir <path> \
    --resume               # resume: reuse cache.json
```

Measured throughput (DeepSeek `deepseek-v4-flash`, 200 paragraphs avg 141 chars):
| Concurrency | Time | Rate (para/s) |
|---|---|---|
| 16 | 15.5s | ~12.9 |
| 64 | 4.0s  | ~49.5 |
| 96 | 3.2s  | ~62.8 |

A ~3300-paragraph book finishes in about 1 minute at concurrency 96.

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