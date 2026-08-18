# epub-translator

将英文 EPUB 文件**逐段翻译为中文**，并把中文译文放在原英文段落的紧下方，形成「英在上、中在下」的双语对照排版。翻译基于任意 OpenAI 兼容模型（DeepSeek / OpenAI / 通义千问 / 智谱 / 自建网关 等），通过线程池并行调度，支持缓存去重与断点续传。

- **批量 JSON 翻译协议**：多条原文组装为 `[{"id":"<8位ID>","text":"..."}]` 一次请求，模型返回 `[{"id":"...","zh":"..."}]`；id 为批内顺序 8 位 base36 码，条目按 id 精确配对。大幅摊薄 system_prompt 开销、减少请求数
- **动态分批**：按 CJK 感知的 token 估算动态调节批次大小（默认单批输入侧 12000 tokens，128K 上下文模型下总量安全）；批数不足时自动收缩预算以保持线程池满载
- 代码块不翻译，原样保留
- 公式（MathML / 含 LaTeX 的 img / 行内 `$...$`、块级 `$$...$$`）保留样式，整体跳过
- 表格：在原表下方追加一份整表中文翻译版（双表对照），不破坏原表
- 段落级双语对照：每个待翻元素紧下方插入一个同类中文节点，并加 `class="translated-zh"`
- **健壮解析与兜底**：剥 markdown 围栏 / 截取最外层数组 / 兼容对象包裹；id 校验；整批指数退避重试 + 缺失条目单条兜底

## 何时使用

- 用户提供英文（或其他外文）EPUB，要求「翻译成中文 / 做中英对照版 / 汉化这本书」。
- 希望保留原文阅读体验的同时增加中文译文（不替换原文）。

## 目录结构（遵循 SkillHub 推荐 Skill 格式）

```
epub-translator/
├── SKILL.md                  # Skill 唯一必需文件：YAML 元数据 + Markdown 指令
├── README.md / README.en.md  # 中英文说明
├── config.example.json       # 配置模板（仓库内，含占位符，安全）
├── config.json               # 真实配置（gitignored，含 API key，勿提交）
├── scripts/
│   └── translate_epub.py     # 主脚本：解包 → 提取 → 并行翻译 → 写回 → 打包
└── assets/
    └── translated.css        # 双语段落基础 CSS（.translated-zh 样式）
```

## 配置（OpenAI 兼容，不硬编码密钥）

所有敏感与服务字段通过 `config.json` 配置，或用同名大写环境变量覆盖（最高优先级）。
仓库只提供 `config.example.json` 模板：

```jsonc
{
    "model_name": "deepseek-v4-flash",
    "api_key":    "sk-YOUR_API_KEY_HERE",   // 建议改用环境变量 API_KEY
    "base_url":   "https://api.deepseek.com", // 不含 /chat/completions
    "extra_body": {},
    "batch_tokens": 12000,                   // 单批预估 token 预算（输入侧，可选）
    "system_prompt": "..."                   // 批量 JSON 协议专用 prompt
}
```

使用前：`cp config.example.json config.json`，填入真实 key（或留空，用环境变量注入）。
查找优先级：命令行 `--config` > 环境变量 > `scripts` 同级/上一级目录的 `config.json` > `config.example.json`。

> `batch_tokens` 亦可用 CLI `--batch-tokens` 或环境变量 `BATCH_TOKENS` 覆盖。
> 若自定义 `system_prompt`，必须与批量协议一致（输入 `[{"id","text"}]`，输出 `[{"id","zh"}]`），否则脚本无法解析模型返回。

## 依赖

```bash
pip install openai beautifulsoup4 lxml
```

（公式即图片型场景若还需字体子集化，`pip install fonttools brotli`，见 epub-reader-optimizer。）

## 用法

```bash
# 基本用法
python scripts/translate_epub.py input.epub output.epub

# 可选参数
python scripts/translate_epub.py input.epub output.epub \
    --concurrency 64 \     # 默认 64；可到 96 仍近线性扩展
    --batch-tokens 12000 \ # 单批预估 token 预算（输入侧，默认 12000；可按需调大）
    --work-dir <path> \   # 工作目录
    --resume              # 断点续传：复用 cache.json
```

### 批量协议带来的请求数压缩

整本 ~3300 段（平均 141 字符）的书，旧版每段一次请求需 ~3300 次 API 调用；
批量协议下单批 12000 tokens（约 90~120 段），总请求数降至 **~30 次**（1~2 个数量级），
且 system_prompt 开销从占总输入约 45% 摊薄到 <5%。实测并发仍可近线性扩展：

| 并发 | 耗时 | 速率（段/秒） |
|---|---|---|
| 16 | 15.5s | ~12.9 |
| 64 | 4.0s  | ~49.5 |
| 96 | 3.2s  | ~62.8 |

> 提速上限受 API 侧并发/限流影响；若网关对长输出不稳定，调小 `--batch-tokens`（如 6000~8000），缺失条目会自动单条兜底。

## 安全建议

- `config.json` 含真实 API key，已被 `.gitignore` 排除，**切勿提交**。
- 推荐：`config.json` 只写 `model_name` / `base_url` / `extra_body` / `system_prompt`，
  `api_key` 通过环境变量 `API_KEY` 注入。
- 复刻他人的 Kindle/O'Reilly/Apress 等受版权电子书用于翻译仅限个人学习，请勿传播。

## 后续步骤（必做）

翻译打包完成后，应调用 `epub-reader-optimizer` skill 对成品做最终样式美化
（中文字体、`.translated-zh` 颜色与左色条、表格双语视觉区分、白底白字修复等）。

完整翻译单元识别规则、出版社适配经验、System Prompt、占位符保护、异常处理等见 [SKILL.md](SKILL.md)。