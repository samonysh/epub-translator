---
name: "epub-translator"
description: "将英文 EPUB 文件逐段翻译为中文（保留原文，译文紧跟其下），自动跳过代码块、保留公式样式、表格采取双表对照。基于 Deepseek 大模型并行翻译。当用户提供 EPUB 并要求翻译/中英对照/汉化时调用。"
tags: [epub, 翻译, 中英对照, DeepSeek, 并行翻译, 双语]
---

# EPUB Translator (英→中 双语对照)

本 skill 将英文 EPUB 文件中的内容**逐段翻译为中文**，并把中文译文**放在原英文段落的紧下方**，形成"英在上、中在下"的双语对照排版。翻译完成后**必须**调用 `epub-reader-optimizer` skill 对成品进行样式美化。

## 何时调用

满足以下任一条件时调用本 skill：

- 用户提供英文（或其他外文）EPUB 文件，要求"翻译成中文" / "做中英对照版" / "汉化这本书"。
- 用户希望保留原文阅读体验的同时增加中文译文（不是替换原文）。
- 用户希望把一本英文电子书做成"段落级双语对照"版本，方便边读边对照。

## 何时不调用

- 用户提供的是中文 EPUB 想翻成英文 → 不在本 skill 范围（可参考本 skill 改 prompt）。
- 用户只想优化排版而不翻译 → 改用 `epub-reader-optimizer`。
- 用户提供的不是 EPUB（如 PDF/mobi/azw3）→ 先转 EPUB 再来。
- 用户希望"机器翻译重写原文"（即替换而非追加）→ 不属于本 skill 默认行为，需要显式确认后修改脚本。

## 核心约束

1. **代码块不翻译**：识别 `<pre>`、`<code>` 标签包裹的内容，原样保留，不调用翻译 API。
2. **公式保留样式**：识别 `<math>`、MathML、`<img>` 公式（含 alt LaTeX）、行内 `$...$` / `\(...\)`、块级 `$$...$$` / `\[...\]` 等结构，**整段跳过翻译**或仅翻译纯文本上下文，公式本身原样保留。
3. **表格翻译策略**：对每个 `<table>`，**生成一份中文翻译表追加到原表下方**（而非原地覆盖），保持原表完整。
4. **段落级双语对照**：对普通段落（`<p>`、`<li>`、标题 `<h1>~<h6>`、`<blockquote>` 等），在该元素的紧下方插入一个**同类型**的中文节点，并加上 `class="translated-zh"` 便于后续 CSS 样式化。
5. **批量 JSON 翻译协议 + 并行**：多条原文组装为 `[{"id":"<8位base36>","text":"..."}]` 一次请求，模型返回 `[{"id":"...","zh":"..."}]`（id 为批内顺序 8 位 base36 码，如 `0000000a`）。批次大小按 token 预算动态调节（默认单批输入侧约 12000 tokens，128K 上下文模型下总量安全；批数不足并发数×2 时自动收缩预算保持线程池满载）。大幅摊薄 system_prompt 开销并减少请求数。
6. **翻译完成后必须调用 `epub-reader-optimizer`** 对最终 EPUB 做排版美化（字体、行距、双语段落样式、白底白字修复等）。

## 模型配置（OpenAI 兼容）

**不再硬编码** API key / base_url / model_name！全部通过 `config.json` 配置，且自动兼容任意 OpenAI 兼容服务（DeepSeek、OpenAI、通义千问、智谱、自建网关 ...）。

### 配置文件位置（按优先级查找）

1. 命令行 `--config <path>` 显式指定
2. 脚本同目录 `assets/config.json`
3. skill 根目录 `config.json`（推荐放这里）
4. 当前工作目录 `config.json`
5. 兜底使用 `config.example.json`

### config.json 结构

```jsonc
{
    "model_name": "deepseek-v4-flash",
    "api_key":    "sk-YOUR_API_KEY_HERE",
    "base_url":   "https://opencode.ai/zen/go/v1",
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
- `extra_body`：附加请求体。DeepSeek 关闭推理：`{"thinking": {"type": "disabled"}}`，其它模型一般留空 `{}`。
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

依赖安装：`pip install openai beautifulsoup4 lxml`

## 工作流总览

```
1. 准备工作目录，复制输入 EPUB 到临时目录
2. 解包 EPUB（unzip / Python zipfile）
3. 探查 content.opf，列出所有 (x)html 章节文件
4. 遍历章节：用 BeautifulSoup 解析 HTML
   - 收集所有需要翻译的"翻译单元"（段落、列表项、标题、表格单元格、引用）
   - 跳过：<pre>/<code>、公式（math/含 LaTeX 的 img/含 $...$ 的纯文本段）
5. 按 token 预算分批，批量 JSON 协议并行调用翻译（ThreadPoolExecutor + 缓存去重 + 整批重试/单条兜底）
6. 写回 HTML：
   - 普通段落 → 紧跟一个 class="translated-zh" 的同类型节点
   - <table> → 在其后追加一个 class="translated-zh table-zh" 的整表翻译版
7. 注入 CSS（追加到现有 stylesheet 或新建 translated.css）
8. 重新打包 EPUB（mimetype STORED 第一个条目）
9. 调用 epub-reader-optimizer 做最终样式美化
10. 把成品放到用户的 workspace 目录并给出 computer:// 链接
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

**特殊处理**：
- 段落内的 inline `<code>`、行内公式 `$...$`：翻译时把这些片段用占位符 `⟦KEEP_n⟧` 替换，翻译完后再还原。
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
6. 保留 Markdown / HTML 标记（如 **bold**、<em>）不变。
7. 译文中的换行写成 \n 转义，确保输出始终是合法 JSON。
```

**解析健壮性**（脚本内置，勿依赖模型完美输出）：剥 markdown 围栏 -> 直接 `json.loads` -> 兜底截取最外层 `[...]` -> 兼容 `{"items":[...]}` 对象包裹；按 id 校验（必须在批内、zh 非空、重复取首个）；非法 JSON / 条目缺失 -> 整批指数退避重试（5 次）-> 仍缺失的条目单条兜底（单元素数组）-> 最终失败保留原文。

## 可复用 Asset

- `scripts/translate_epub.py` — 主脚本：解包 → 提取翻译单元 → 并行翻译 → 写回 → 打包。
- `assets/translated.css` — 双语段落基础 CSS（`.translated-zh` 样式）。

## 使用方式（执行命令）

```bash
# 基本用法（输入英文 EPUB，输出中英对照 EPUB）
python scripts/translate_epub.py <input.epub> <output.epub>

# 可选参数
python scripts/translate_epub.py input.epub output.epub \
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

### 5. 占位符保护公式 / 行内代码

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

### 6. CSS 注入

往现有 stylesheet 末尾追加（或新建 `translated.css` 并在 OPF/HTML head 中引用）：

```css
.translated-zh {
    color: #1a4d8c;
    font-family: "LXGW WenKai", "Source Han Serif SC", "Noto Serif CJK SC", serif;
    margin-top: 0.2em;
    margin-bottom: 1em;
    line-height: 1.75;
}
.translated-zh.table-zh {
    margin-top: 0.5em;
    border-top: 2px dashed #888;
}
```

## 异常处理

- **API 限流**：捕获 429 / RateLimitError，指数退避重试（最多 5 次）。
- **超长段落**：>3000 字符的段落，先按句号切分再翻，最后拼回。
- **空响应**：如果 Deepseek 返回空，回退为"[翻译失败，原文保留]"标记，不阻塞整体流程。
- **断点续传**：每翻译 50 段落 flush 一次 `cache.json`，意外中断后下次启动自动复用。

## 文件位置约定

- 输入 EPUB：用户提供的路径
- 临时工作目录：`c:\Users\<USER>\.trae-cn\work\<SESSION>\epub-translate\`
- 输出 EPUB：`d:\TARE-WORK\<原文件名>_中英对照.epub`
- 翻译缓存：临时工作目录下的 `cache.json`

## 后续步骤（必做）

翻译完成并打包出 EPUB 后，**必须**调用 `epub-reader-optimizer` skill 对成品做样式美化，重点处理：
- 中文段落字体（LXGW WenKai）
- `.translated-zh` 颜色与左侧色条
- 表格双语对照视觉区分
- 修复阅读器白底白字、行距过挤等通用问题
