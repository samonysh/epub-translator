---
name: "epub-translator"
version: "1.3.0"
updated: "2026-09-30"
description: "将英文 EPUB 文件逐段翻译为中文（保留原文，译文紧跟其下），自动跳过代码块、保留公式样式、表格采取双表对照。基于 Deepseek 大模型并行翻译。当用户提供 EPUB 并要求翻译/中英对照/汉化时调用。"
tags: [epub, 翻译, 中英对照, DeepSeek, 并行翻译, 双语]
---

# EPUB Translator (英→中 双语对照)

> 版本：1.3.0 · 最后更新：2026-09-30

本 skill 将英文 EPUB 文件中的内容**逐段翻译为中文**，并把中文译文**放在原英文段落的紧下方**，形成"英在上、中在下"的双语对照排版。翻译完成时会直接写入项目内建的阅读样式，无需依赖外部样式组件。

## 何时调用

满足以下任一条件时调用本 skill：

- 用户提供英文（或其他外文）EPUB 文件，要求"翻译成中文" / "做中英对照版" / "汉化这本书"。
- 用户希望保留原文阅读体验的同时增加中文译文（不是替换原文）。
- 用户希望把一本英文电子书做成"段落级双语对照"版本，方便边读边对照。

## 何时不调用

- 用户提供的是中文 EPUB 想翻成英文 → 不在本 skill 范围（可参考本 skill 改 prompt）。
- 用户只想优化排版而不翻译 → 使用项目内的阅读样式处理流程。
- 用户提供的不是 EPUB（如 PDF/mobi/azw3）→ 先转 EPUB 再来。
- 用户希望"机器翻译重写原文"（即替换而非追加）→ 不属于本 skill 默认行为，需要显式确认后修改脚本。

## 核心约束

1. **代码块不翻译**：识别 `<pre>`、`<code>` 及出版社代码容器，不调用翻译 API；保留代码文本、缩进与原有空行，只清理出版社独立代码行元素之间的排版空白，避免改变多行字符串语义。
2. **公式保留样式**：识别 `<math>`、MathML、`<img>` 公式（含 alt LaTeX）、行内 `$...$` / `\(...\)`、块级 `$$...$$` / `\[...\]` 等结构，**整段跳过翻译**或仅翻译纯文本上下文，公式本身原样保留。公式图片识别（`has_math_formula`）命中 TeX 命令、数学符号、`x_i`/`v^2` 这类短符号与短标记，但**不会**因散文 alt 中的裸下划线（如 `fact_sales`、`get_user_name`）误判，避免示意图被包成 `span.math-inline` 后压缩成行内小图。
3. **表格翻译策略**：对每个 `<table>`，**生成一份中文翻译表追加到原表下方**（而非原地覆盖），保持原表完整。
4. **段落级双语对照**：对普通段落（`<p>`、`<li>`、标题 `<h1>~<h6>`、`<blockquote>` 等），在该元素的紧下方插入一个**同类型**的中文节点，并加上 `class="translated-zh"` 便于后续 CSS 样式化。
5. **批量 JSON 翻译协议 + 并行**：多条原文组装为 `[{"id":"<8位base36>","text":"..."}]` 一次请求，模型返回 `[{"id":"...","zh":"..."}]`（id 为批内顺序 8 位 base36 码，如 `0000000a`）。批次大小按 token 预算动态调节（默认单批输入侧约 12000 tokens，128K 上下文模型下总量安全；批数不足并发数×2 时自动收缩预算保持线程池满载）。大幅摊薄 system_prompt 开销并减少请求数。
6. **阅读样式内建**：正文衬线、标题无衬线、代码等宽字体；英文/中文行高 1.70/1.78，原译文间隔约 0.28em、中文段后约 1.05em；正文限宽 46rem。代码只在最外层设置浅色面板、内边距与局部横向滚动，不给嵌套行重复加空白。宽表、块公式和超长行内 MathML 使用局部滚动容器。
7. **目录与标题同一行**：章节标题和 EPUB3 目录链接均为“英文 · 中文”，以低干扰圆点分隔；正文仍为“英文段落后紧跟中文段落”。
8. **图片分类与尺寸**：结构、资源名、图注和真实像素联合判定；结构中的插图不能因描述里引用 TeX 而缩成公式。普通插图最大占满正文可用宽度，最高 80vh；较小插图适度放大至 18rem；超宽图使用 48–64rem 的局部横向滚动视图，避免缩成细条；作者照片限宽 10rem、封面限高 85vh。公式与图标保留行内尺度，明确的公式资源名优先保护。翻译阶段按资源去重；已有产物仅优化样式时不去重、不重采样图片。
9. **目录兼容**：EPUB3 目录与 EPUB2 NCX 均纳入翻译；NCX 使用 Python 标准库解析，不依赖可选的 `lxml`。
10. **中文成品文件名**：从 OPF 的 `dc:title` 读取书名并纳入翻译，成品自动命名为其中文译名（会清理 Windows 非法字符）；命令中的输出路径仅用于指定输出目录。
11. **出版社结构补充**：Springer/Apress 的 `CaptionContent`、印刷目录、索引词条和等宽行内 span 分别处理；译文中的原样标识符不重复显示，翻译表格不复制原表锚点。

## 模型配置（OpenAI 兼容）

**不再硬编码** API key / base_url / model_name！全部通过 `config.json` 配置，且自动兼容任意 OpenAI 兼容服务（DeepSeek、OpenAI、通义千问、智谱、自建网关 ...）。

### 配置文件位置（按优先级查找）

1. 命令行 `--config <path>` 显式指定
2. 脚本同目录 `scripts/config.json`
3. 项目根目录 `config.json`（推荐放这里）
4. 当前工作目录 `config.json`
5. 兜底使用 `config.example.json`（脚本同目录或项目根目录）

环境变量只覆盖字段，不改变配置文件的查找位置。

### config.json 结构

```jsonc
{
    "model_name": "deepseek-v4-flash",
    "api_key":    "sk-YOUR_API_KEY_HERE",
    "base_url":   "https://opencode.ai/zen/go/v1",
    "reasoning_mode": "disabled",
    "reasoning_provider": "auto",
    "extra_body": {},
    "batch_tokens": 12000,
    "system_prompt": "你是一个英译中批量翻译引擎...（可选，有默认值）"
}
```

字段说明：

- `model_name`：OpenAI 兼容模型名。例：`deepseek-v4-flash`、`deepseek-chat`、`gpt-4o-mini`、`qwen-plus`、`glm-4-flash`。
- `api_key`：API key。**敏感字段，不要提交到公共仓库。** 推荐用环境变量 `API_KEY` 覆盖。
- `base_url`：服务根地址（**不含 `/chat/completions`**）。脚本会自动剥掉末尾的 `/chat/completions` 与多余斜杠。例：
  - DeepSeek 官方：`https://api.deepseek.com`
  - OpenCode 网关：`https://opencode.ai/zen/go/v1`
  - OpenAI 官方：`https://api.openai.com/v1`
  - 通义千问兼容模式：`https://dashscope.aliyuncs.com/compatible-mode/v1`
  - 智谱：`https://open.bigmodel.cn/api/paas/v4`
- `reasoning_mode`：翻译推理强度，`disabled`（默认，尽量关闭）或 `low`；可由 `REASONING_MODE` 覆盖。
- `reasoning_provider`：供应商思考参数方言。默认 `auto` 从 base_url 识别；也可显式设为 `openai`、`deepseek`、`qwen`、`zhipu`、`ark` 或 `generic`，并用 `REASONING_PROVIDER` 覆盖。关闭时，OpenAI 支持模型使用 `reasoning_effort: none`（旧模型降为 `low`），DeepSeek/方舟/智谱使用 `thinking.type: disabled`，Qwen 使用 `enable_thinking: false`。未知网关不猜测参数，避免接口报错。
- `extra_body`：附加请求体。翻译所需的推理控制字段由脚本强制写入，同名字段不能意外开启思考。
- `batch_tokens`：单批翻译的预估 token 预算（输入侧，默认 12000）。输出约与输入同量级，128K 上下文模型下总量安全。可用 CLI `--batch-tokens` 或环境变量 `BATCH_TOKENS` 覆盖。
- `system_prompt`：翻译用 system prompt（**批量 JSON 协议专用**，与请求/响应格式耦合，改协议需同步改脚本）。脚本内置默认值，按需覆盖。

### 环境变量覆盖（最高优先级）

适合临时切换 / CI：

```bash
$env:API_KEY     = "sk-..."
$env:MODEL_NAME  = "deepseek-chat"
$env:BASE_URL    = "https://api.deepseek.com"
$env:SYSTEM_PROMPT = "...（可选）"
```

### 安全建议

- `config.json` 默认提供示例 key 仅供测试；生产请改为占位符或加入 `.gitignore`。
- 推荐做法：`config.json` 仅写 `model_name` / `base_url` / `extra_body` / `system_prompt`，而 `api_key` 通过环境变量 `API_KEY` 注入。

调用示例（脚本内部，批量 JSON 协议）：

```python
from openai import OpenAI

client = OpenAI(api_key=CONFIG["api_key"], base_url=CONFIG["base_url"])

# 多段原文组装为一个 JSON 数组（id 为批内顺序 8 位 base36 码）
user_text = json.dumps([
    {"id": "00000000", "text": "First paragraph ..."},
    {"id": "00000001", "text": "Second paragraph ..."},
], ensure_ascii=False)

resp = client.chat.completions.create(
    model=CONFIG["model_name"],
    messages=[
        {"role": "system", "content": CONFIG["system_prompt"]},   # 批量协议专用 prompt
        {"role": "user",   "content": user_text},
    ],
    stream=False,
    extra_body=CONFIG.get("extra_body") or {},
)
raw = resp.choices[0].message.content
# 要求模型返回 [{"id":"...","zh":"..."}]；解析后按 id 配对写回
```

依赖安装：优先运行 `uv sync --locked`；`pyproject.toml` 声明依赖，`uv.lock` 锁定版本。`lxml` 可选，需要时运行 `uv sync --locked --extra xml`；也可用 `pip install -r requirements.txt`。PyPI `epubcheck==5.3.0` 附带 Java 检查器，但系统仍须提供 `java` 命令。成品打包后自动检查，报告写入同目录的 `.epubcheck.json`；检查错误或工具不可用时脚本返回非零退出码。

## 工作流总览

```
1. 准备工作目录，复制输入 EPUB 到临时目录
2. 解包 EPUB（unzip / Python zipfile）
3. 探查 content.opf，列出所有 (x)html 章节文件
4. 遍历章节与 EPUB3 目录：用 BeautifulSoup 解析 HTML
   - 收集所有需要翻译的"翻译单元"（段落、列表项、标题、表格单元格、引用）
   - 跳过：<pre>/<code>、公式（math/含 LaTeX 的 img/含 $...$ 的纯文本段）
5. 按 token 预算分批，批量 JSON 协议并行调用翻译（ThreadPoolExecutor + 缓存去重 + 整批重试/单条兜底）
6. 写回 HTML：
   - 普通段落 → 紧跟一个 class="translated-zh" 的同类型节点
   - <table> → 在其后追加一个 class="translated-zh table-zh" 的整表翻译版
7. 注入 CSS：把 `assets/translated.css` 复制进 EPUB（与 content.opf 同级或 OEBPS 根），为每个章节 `<head>` 追加引用并登记进 OPF manifest（幂等）
8. 重新打包 EPUB（mimetype STORED 第一个条目）
9. 输出 EPUB 写入第二个参数指定的目录，文件名为 OPF 书名的中文译名
```

## 翻译单元识别规则（关键）

**跳过翻译**的节点：
- 标签：`<pre>`、`<code>`、`<math>`、`<svg>`、`<script>`、`<style>`、`<noscript>`、`<kbd>`、`<samp>`、`<tt>`
- div class（Springer/O'Reilly/Apress 等常见出版社）：
  - `ProgramCode`、`LineGroup`、`FixedLine`、`EmphasisFontCategoryNonProportional`
  - `Code`/`code-block`/`CodeBlock`/`listing`/`Listing`
  - `informalexample`、`programlisting`、`screen`、`literallayout`
  - 公式相关：`MathJax`、`equation`、`Equation`、`EquationContent`
- `<img>`（公式图片或装饰图片）
- 纯文本只包含 LaTeX/公式（如 `$E = mc^2$`、`\(x_i\)`、`\[ \sum ... \]`）的段落
- 文本长度 < 2 或只含数字/标点的段落
- 文本已超过 50% 是 CJK（已经是中文）的段落

**需要翻译**的节点（按类型保留原标签）：
- 标签：`<p>`、`<blockquote>`、`<li>`、`<h1>`~`<h6>`、`<figcaption>`、`<caption>`、`<dd>`、`<dt>`
- div class：`<div class="Para">`（出版社段落容器，常见于 Springer/Apress）
- 表格 `<table>` 的 `<th>`、`<td>` 文本
- EPUB3 目录文件（`nav.xhtml` / `navigation.xhtml` / `toc.xhtml`）中的 `<a>` 链接文字，以及 EPUB2 `toc.ncx` 的导航标签；中文均追加在英文后。

**特殊处理**：
- 段落内的 inline `<code>`：翻译时以模型可见的 `<code data-epub-inline-code="N">代码正文</code>` 标记保留在正文中。模型必须按中文语序原样回显该标记；写回时以原 EPUB 节点替换标记，从而保留原 class 与行内代码样式。若模型漏掉标记，脚本仍会追加原节点兜底。
- 行内公式 `$...$` 等非文本片段仍以占位符 `⟦KEEP_n⟧` 保护，翻译完后还原。
- 段落级 div 内部含有代码块子节点时，**提取文本时自动跳过 skippable 子树**，只翻译散文部分。
- `class` 属性可能是 list 或 str，统一用 `_classes_of()` 辅助函数取规范化的 list。

## 出版社适配经验（实战）

**Springer / Apress** 的常见 EPUB 结构：
- 段落用 `<p class="Para">` 或 `<div class="Para">`（两种都要收集）
- 代码块嵌套：`<div class="ProgramCode"><div class="LineGroup"><div class="FixedLine">...</div></div></div>`（完全没有 `<pre>` / `<code>`！）
- 行内等宽文字：`<span class="EmphasisFontCategoryNonProportional">`
- 章节标题：`<h1 class="ChapterTitle">`、`<h2 class="Heading">`
- 内联术语：`<span id="ITerm1">...</span>` — 不必处理，文本提取会自动忽略 id

**O'Reilly** 类 EPUB 通常用语义化 `<pre><code>` —— 已经覆盖。

**注意**：处理新出版社时，先用 `class` 出现频率统计快速识别其代码块 class 名，必要时把它加入 `SKIP_DIV_CLASSES`。

## System Prompt（批量 JSON 协议，翻译质量关键）

单段一请求的旧模式里 system_prompt 占比过高；批量模式下多段共享一次 prompt 开销。id 采用批内顺序 8 位 base36 码（`00000000` -> `0000000z` -> `00000010`）：零碰撞、模型回显出错率低、与输入顺序天然对齐。

```
你是一个英译中批量翻译引擎。用户消息是一个 JSON 数组，每个元素形如 {"id":"<8位ID>","text":"<待翻译的英文原文>"}。
你的任务：把每个元素的 text 翻译成简体中文，然后只输出一个 JSON 数组作为回答，每个元素形如 {"id":"<对应的原ID>","zh":"<中文译文>"}。
硬性规则：
1. 只输出 JSON 数组本身，不要输出任何解释、前后缀或 markdown 代码块标记。
2. id 必须与输入完全一致，顺序与输入一致，条目数与输入相同；不得遗漏、合并或拆分任何条目。
3. 译文准确、自然、符合中文技术写作习惯，不要逐字硬译；标题类短句保持标题语气。
4. 保留所有 ⟦KEEP_0⟧ ⟦KEEP_1⟧ 之类的占位符原样不变，不要翻译、删除或改写它们。
5. 保留专有名词、API 名、类名、函数名、产品名的英文原文（必要时可加中文解释）。
6. text 中出现的 <code data-epub-inline-code="N">...</code> 是正文中的行内代码：必须原样保留该标签、data-epub-inline-code 属性和其中代码文本，并按中文语序放在合适位置；不要把它替换成占位符、Markdown 反引号或普通文本。
7. 保留其他 Markdown / HTML 标记（如 **bold**、<em>）不变。
8. 译文中的换行写成 \n 转义，确保输出始终是合法 JSON。
```

**解析健壮性**（脚本内置，勿依赖模型完美输出）：剥 markdown 围栏 -> 直接 `json.loads` -> 兜底截取最外层 `[...]` -> 兼容 `{"items":[...]}` 对象包裹；按 id 校验（必须在批内、zh 非空、重复取首个）；非法 JSON / 条目缺失 -> 整批指数退避重试（5 次，累积已收到的条目）-> 仍缺失或保护标记无效的条目单条兜底（单元素数组）-> 最终失败停止生成，保留有效缓存供 `--resume` 重试。代码标记内容、公式位置标记和 URL/LaTeX 占位符必须保全，不能把空译文当缓存命中。

## 可复用 Asset

- `scripts/translate_epub.py` — 主脚本：解包 → 提取翻译单元 → 并行翻译 → 写回 → 打包。
- `assets/translated.css` — 完整双语阅读 CSS（正文节奏、标题、代码、图片分类、公式与宽表）。
- `scripts/optimize_epub.py` — 已有 EPUB 的完整样式优化，内容审计与 EPUBCheck 后写入；不调用翻译 API。
- `scripts/review_epub.py` — 实际产物截图、布局扫描、前后图库、agent 审阅任务与反馈登记。
- `scripts/fix_epub_image_size.py` — **旧版图片专用修复工具**（85% 兼容规则保存在 `assets/legacy-image-sizing.css`）：把被钳成 ~24px 的插图改回
  `epub-image`、统一插图上限为 85%、保留出版社给定的 `width/height` 尺寸，
  并顺带把未改条目按 deflate level 9 重压缩（体积通常变小）。
  ```bash
  python scripts/fix_epub_image_size.py --backup-dir <备份目录> --dry-run 书1.epub 书2.epub
  python scripts/fix_epub_image_size.py --backup-dir <备份目录> 书1.epub 书2.epub
  python scripts/fix_epub_image_size.py --recompress-only 书1.epub      # 只重压缩，不改内容
  ```

## 使用方式（执行命令）

```bash
# 基本用法（第二个参数用于指定输出目录；最终文件名自动使用中文书名）
uv run --locked python scripts/translate_epub.py <input.epub> <output-directory>/placeholder.epub

# 可选参数
uv run --locked python scripts/translate_epub.py input.epub output.epub \
    --concurrency 64 \                                # 默认 64；测试 96 仍可线性 scale
    --batch-tokens 12000 \                            # 单批预估 token 预算（输入侧，默认 12000）
    --work-dir <path> \                              # 工作目录（解包/缓存所在）
    --resume                                          # 断点续传：复用 cache.json
```

### 并发性能基准（实测）

200 条段落（平均 141 字符），DeepSeek `deepseek-v4-flash`：

| 并发 | 耗时 | 速率 | 相对提升 |
|---|---|---|---|
| 16 | 15.5 s | 12.9/段·秒 | 1.0× |
| 32 | 7.0 s | 28.4/段·秒 | 2.2× |
| 64 | 4.0 s | 49.5/段·秒 | 3.8× |
| 96 | 3.2 s | 62.8/段·秒 | 4.9× |

DeepSeek API 几乎线性扩展，**推荐默认并发 64，急速档 96**。整本 ~3300 段的书在 96 并发下约 1 分钟可全量翻译完。

### 幂等性 / 重复运行安全

- 重复运行**不会重复插入译文**。脚本在三处都做幂等检查：
  1. `collect_units_in_html`：跳过 class 含 `translated-zh` 的节点 + 跳过紧邻已是 translated-zh 的源节点
  2. `insert_translation_node`：插入前再次检查 next sibling
  3. `translate_table_node`：同样检查 next sibling 是否为已生成的翻译表
- `--resume` 模式下**强制重新解包源 EPUB**（不复用已写过译文的 extract 目录），从干净 DOM 上重做"复用缓存 → 写回译文"，避免污染。
- class 去重：`["Para", "translated-zh"]` 不会变成 `["Para", "translated-zh", "translated-zh"]`。

### 全局两阶段并行 + 批量 JSON 请求（核心性能优化）

旧版本按"章节串行"循环翻译，章节切换时线程池闲置；且每段一次请求，system_prompt 重复发送数千次。现改为**全局两阶段 + 批量协议**：

1. **PHASE 1**：扫描所有章节，把全部 unique 待翻文本汇总到一个全局列表（DOM 解析快，串行即可）
2. **PHASE 2**：按 token 预算分批（`est_tokens` 估算：中文 ≈0.7 token/字，英文 ≈3.5 字符/token；每条加 JSON 包装开销约 14 tokens），批级线程池并行翻译。**自适应调节**：批数 < 并发数×2 时自动收缩单批预算（下限 600 tokens）保持满载；单批条目上限 128 防止模型回显长 ID 列表漂移
3. **PHASE 3**：按缓存把译文写回各章节 DOM 并保存

效果：API 调用 100% 重叠 + 每请求携带数十至上百段原文，**请求数下降 1~2 个数量级，system_prompt 开销摊薄至可忽略**。参考：128K 上下文模型下单批默认 12000 输入 tokens（输出同量级），总量约 25K，安全余量充足。


## 关键实现要点

### 1. 段落抽取与去重缓存

把"待翻译文本 → 译文"存到一个 `dict` 缓存（同时序列化为 `cache.json`），同一本书里重复出现的短语只翻一次，节省 API 调用。

### 2. 批量并行调度

```python
import json
from concurrent.futures import ThreadPoolExecutor, as_completed

def batch_id(i: int) -> str:          # 00000000 / 00000001 / ... / 0000000z / 00000010
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    out = ""
    while i:
        out = digits[i % 36] + out
        i //= 36
    return out.rjust(8, "0")

def run_batch(texts):
    items = [{"id": batch_id(i), "text": protect(t)[0]} for i, t in enumerate(texts)]
    resp = client.chat.completions.create(
        model=MODEL_NAME,
        messages=[{"role": "system", "content": SYSTEM_PROMPT},   # 批量协议专用
                  {"role": "user", "content": json.dumps(items, ensure_ascii=False)}],
        stream=False,
    )
    got = {el["id"]: el["zh"].strip()                              # 解析+校验后按 id 配对
           for el in parse_llm_json_array(resp.choices[0].message.content)}
    return restore_and_fallback(texts, items, got)                 # 缺失条目单条兜底

with ThreadPoolExecutor(max_workers=64) as pool:                   # 批级并行
    futures = {pool.submit(run_batch, b): b for b in batches}      # batches 已按 token 预算切好
    for fu in as_completed(futures):
        update_cache(fu.result())
```

### 3. 写回 HTML（保留段落标签类型）

```python
from bs4 import BeautifulSoup

def insert_translation(soup, node, zh_text):
    new_tag = soup.new_tag(node.name)
    new_tag['class'] = (node.get('class', []) or []) + ['translated-zh']
    new_tag.string = zh_text
    node.insert_after(new_tag)
```

### 4. 表格翻译（整表克隆 + 翻译所有单元格 + 追加）

```python
import copy
def translate_table(soup, table_node, translator):
    cloned = copy.copy(table_node)  # 浅拷贝后递归处理或直接 deepcopy
    # ... 遍历 cloned 的 th/td，把 text 替换为译文 ...
    cloned['class'] = (cloned.get('class', []) or []) + ['translated-zh', 'table-zh']
    table_node.insert_after(cloned)
```

### 5. 公式占位符保护与模型可见的行内代码

```python
import re
PLACEHOLDER_RE = re.compile(r'(`[^`]+`|\$[^$\n]+\$|\\\([^)]+\\\))')

def protect(text):
    keeps = []
    def _sub(m):
        keeps.append(m.group(0))
        return f"⟦KEEP_{len(keeps)-1}⟧"
    masked = PLACEHOLDER_RE.sub(_sub, text)
    return masked, keeps

def restore(text, keeps):
    for i, k in enumerate(keeps):
        text = text.replace(f"⟦KEEP_{i}⟧", k)
    return text
```

EPUB 的 inline `<code>` 不走上述占位符：提取正文时会生成
`<code data-epub-inline-code="0">client.connect()</code>`。这让模型同时看到代码内容与
所在句子；回写阶段再以原 `<code>` 节点的深拷贝替换该标记，保留出版社的 class/样式。

### 6. CSS 注入

先运行 `normalise_reading_layout` 添加原文段标记、代码层级和公式/表格局部滚动容器，然后注入 `assets/translated.css`。CSS 引用放在出版社样式之后，已有同名样式也会刷新；注入不重序列化章节，保留代码空白。新生成的 EPUB 与 `scripts/optimize_epub.py` 使用同一套布局逻辑。

正文、标题、代码、图注、表格和公式分别设定字号/行高，不再全局强制所有 span、div 使用同一种字体与行高。完整规则以 CSS 文件为准；不要在这里另维护一份容易过期的 CSS 副本。

## 异常处理

- **API 限流**：捕获 429 / RateLimitError，指数退避重试（最多 5 次）。
- **超长段落**：超过 token 预算的单个段落独立成批，保留段落整体；按模型上下文容量选择预算。
- **空响应或标记丢失**：重试后仍无有效译文则停止生成，不写入空缓存或伪成功成品；修复后 `--resume`。
- **扫描/写回失败**：任一章节或 NCX 失败即停止；候选包必须通过 EPUBCheck 后才替换正式输出。失败候选另存 `.failed.epub` 供诊断。
- **断点续传**：每完成约 50 条 flush 一次 `cache.json`，需显式 `--resume` 复用有效缓存。工作目录含标记，只重建 `extract/`；拒绝清理不相关目录或包含输入/输出/配置的目录。

## 文件位置约定

- 输入 EPUB：命令行第一个参数指定的路径。
- 临时工作目录：默认在当前目录下的 `.epub-translate-<书名>/`（可用 `--work-dir` 覆盖），内含解包内容与 `cache.json`，已被 `.gitignore` 忽略。
- 输出 EPUB：命令行第二个参数所在目录，文件名为 OPF 书名的中文译名。
- 翻译缓存：工作目录下的 `cache.json`。

## 产物截图与 agent 样式反馈（必做）

Agent 交付 EPUB 时必须完成实际产物的截图审阅；普通用户无浏览器的纯 CLI 翻译仍可独立使用。不能以“已写入 CSS”或自动布局指标代替看图，也不能调用外部样式 skill 替代项目内流程。

1. 安装可选依赖：`uv sync --locked --extra review`，首次运行 `uv run --locked --extra review python -m playwright install chromium`。已有浏览器可传 `--browser-executable <path>`，不要把本机路径硬编码到项目。
2. 翻译命令加 `--style-review --review-dir <本轮目录>`；已有产物先运行 `scripts/review_epub.py input.epub --output <before目录>`。抽样按 OPF spine 均匀选 6 处，补齐存在的 h1-h6、长正文、代码、插图、照片、公式、表格、列表与目录，覆盖 1050px/420px；记录章节、选择器和位置，不能称为固定页码。
3. **打开并分析真实 PNG**。可先用联系表浏览，但对小图、超高图、代码、公式和密集表格要查看原图；包括横向滚动至右端的补充截图。结合 `report.json` 检查图片比例/尺度、英中段间节奏、标题层级、代码缩进/内边距和整页溢出。`missing_categories` 可能代表原书无此内容，必须说明；渲染错误必须修复或明确报告未完成。
4. 针对截图修改项目 CSS 或 DOM 分类处理。运行 `scripts/optimize_epub.py input.epub output.epub`，不调用翻译 API；默认内容保留审计与 EPUBCheck，校验成功才替换输出。修改后的 CSS 必须写进 EPUB，不能仅为截图注入覆盖。
5. 使用新目录重渲染：`scripts/review_epub.py output.epub --output <after目录> --plan <before目录>/samples.json --compare <before目录>/report.json --scan-all`。保留每轮报告、样本和前后图库；抽样位置一致，全书窄屏扫描另覆盖未截图章节。若仍有问题，继续改进与复查。
6. 对**最终产物**填写 `feedback.template.json` 为 `feedback.json`：相同产物哈希、`status=reviewed`、结论、所有已查看截图名、逐条问题/修改/原因及 `resolved` 或 `accepted` 状态。逐项关联当前 `report.json` 的 `issues[].id` 到 `issue_ids`；仍在当前报告中的候选问题需明确 `accepted` 及原因，已修复问题必须先重新截图。只在实际查看过截图后登记。执行 `scripts/review_epub.py output.epub --output <after目录> --accept-feedback <after目录>/feedback.json` 校验与保存。EPUB 和每张 PNG 均校验 SHA-256；产物或截图更改后旧反馈失效，必须重新截图。渲染错误返回非零并记录 `render_failed`。
7. 最终报告分开说明：内容保留、EPUBCheck、已看截图、全书布局扫描、接受的限制和未测阅读器。`awaiting_agent_review` 不能声称完成视觉审阅；即使 `agent_reviewed` 也不是全部阅读器分页认证。

可复现命令、安装参数和已有成品的保内容优化入口见 README.md 的“样式优化与截图反馈”。
