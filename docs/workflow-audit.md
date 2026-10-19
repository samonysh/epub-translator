# 整体流程审计

日期：2026-10-19。范围：当前工作区的翻译、写回、样式优化、打包、规范检查、截图与反馈登记流程。

审计快照：`891723d13e058b1a8d88b34483341ac998f7a535`，当时项目元数据为 1.3.0，已包含后续归入 1.4.0 的样式与截图反馈功能。本报告及 JSON 中的环境版本、测试结果和文件哈希属于该快照；当前项目版本另见 `pyproject.toml` 与 [CHANGELOG.md](../CHANGELOG.md)。

发现并修复了内容完整性、失败处理和截图反馈校验的疏漏。最终 **93 项测试全部通过，无跳过**；参考书另完成 237 个文档的内容审计和窄屏扫描、55 张截图审阅，EPUBCheck 为 0 fatal / 0 error / 0 warning。机器可读记录见 [验证记录](audits/2026-10-19-workflow.json)。

## 修复的问题

| 环节 | 复现的问题 | 修复与回归覆盖 |
| --- | --- | --- |
| 公式写回 | MathML 在中文伴随段落中消失；中文表格克隆会移除公式图片 | 行内公式使用位置标记，写回原始 DOM 副本；译文表格保留公式图片；去除副本锚点以避免重复 ID |
| 代码完整性 | 压缩 `<pre>` 内连续空行会改变多行字符串内容 | 保留实际代码空行和缩进，只清除出版社独立行元素之间的排版空白；拒绝模型改写行内代码标记内容 |
| XML 序列化 | HTML 解析器小写化 SVG `viewBox`、`linearGradient` 等名称 | 从 XML 恢复外来内容的元素与属性大小写；样式优化在最终序列化后再次核对内容签名 |
| 翻译覆盖 | 嵌入式目录被视为纯目录文件，正文漏译；普通图注、表注漏收集 | 目录链接与章节正文分别收集；补收图注和表注；目录列表不重复插入译文 |
| 图注结构 | 直接翻译图注会新增第二个 `figcaption`，EPUBCheck 拒绝 | 中文放在原图注内，保留唯一 `figcaption` / `caption`；真实 EPUB 3 检查覆盖该结构 |
| 失败处理 | 章节扫描或写回异常后仍继续打包；空译文可能以成功状态结束 | 任一失败或缺失有效译文即停止；返回非零状态，保留有效缓存供重试 |
| 批次与缓存 | 部分 JSON 响应重试丢失此前成功条目；空值缓存掩盖失败；标记丢失未被严格拒绝 | 累积批次结果，校验代码、公式与 URL/LaTeX 标记；仅缓存有效译文；无效缓存条目重新请求 |
| 工作目录与产物 | 清理整个工作目录可能删除不相关文件；打包失败或校验失败可能覆盖旧产物 | 校验目录归属与输入/输出/配置包含关系，仅重建解包子目录；拒绝 ZIP 路径越界；原子打包，候选包检查通过后才替换正式输出 |
| EPUB 2 与命名空间 | 新增 `aria-hidden` 不符合 EPUB 2；标准 XHTML 命名实体无法通过默认 XML 预检；带前缀的 head/manifest 注入错误 | EPUB 2 去除项目新增的 ARIA 属性，本地解析标准实体并拒绝未知实体，保留注入元素的 XML 前缀且避免 ID 冲突；真实 EPUB 2 + NCX 流程验证 |
| 截图失败状态 | 选择器/渲染失败仍可能返回成功；空样本或越界样本无严格检查 | 校验采样计划；渲染错误记录 `render_failed`，返回非零状态；翻译后的自动截图失败同样上报 |
| 审阅反馈 | 修改 PNG 后仍能接受旧反馈；一条反馈可掩盖多项候选问题 | 绑定 EPUB 与逐张 PNG 的 SHA-256；每个候选问题有 ID，反馈逐项关联且证据须匹配同文档/视口；未消失的问题须说明接受原因，否则修复后重新截图 |
| 文档一致性 | 仍描述“空译文不阻塞”“压缩代码空行”“超长段落按句号切分” | README 中英文版与 SKILL 同步当前实现：失败阻断、显式续跑、代码保真、超预算单段独立成批 |

## 验证方式

新增 23 项回归测试，原有 70 项继续通过。测试代码主要位于 [test_pipeline.py](../tests/test_pipeline.py)，样式、图片、行内代码与 EPUBCheck 的原有测试继续参与全量验证。

- 通过本地 OpenAI 兼容 HTTP 服务走真实 SDK 请求、批量 JSON、保护标记、缓存、双语写回和打包，不调用付费模型。
- 生成含嵌入目录、行内代码、MathML、公式图片、SVG、图注、表注及表格的 EPUB 3；真实 EPUBCheck 验证成品，再以 `--resume` 确认不重复发起请求。
- 生成 EPUB 2，翻译 NCX 导航并通过真实 EPUBCheck；覆盖旧版 XHTML 命名实体。
- 翻译追加 `--style-review`，通过真实 Chromium 生成截图，状态停在 `awaiting_agent_review`，等待实际看图。
- 注入扫描/写回、缺失译文、打包、EPUBCheck 和渲染故障，确认失败阻断及旧输出保留；模拟 PNG/EPUB 修改和反馈遗漏，确认不能关闭审阅。
- `uv lock --check`、脚本/测试语法编译和 `git diff --check` 通过。README 引用的四张中英文工作流 PNG 均存在，分辨率 1920 × 1200。

运行环境：Windows、Python 3.14.2、BeautifulSoup 4.15.0、OpenAI SDK 3.22.1、epubcheck 5.3.0、Playwright 1.64.0、Temurin JDK 25.0.4.1。版本以本次锁定环境为准。

复现测试：

```powershell
uv sync --locked --extra review
uv run --locked --extra review python -m playwright install chromium
# 指向本机可用的 Chromium，启用真实浏览器测试；不设置会跳过相应测试。
$env:EPUB_REVIEW_BROWSER = 'C:\path\to\chrome.exe'
uv run --locked --extra review python -m unittest discover -s tests -v
```

本次受限 Windows 环境需要将 `TEMP` / `TMP` 指向工作区可写目录；未设置时部分旧测试在系统沙箱临时目录出现权限错误，设置后全部通过：

```powershell
New-Item -ItemType Directory -Force .test-scratch/tmp | Out-Null
$env:TEMP = (Resolve-Path .test-scratch/tmp).Path
$env:TMP = $env:TEMP
```

## 参考书复核

在工作区副本上运行当前优化器，未修改 Desktop/Reading 下的原书。

- 源副本：`.style-review-springer/施普林格数据工程手册-优化完成.epub`。
- 本轮产物：`.style-review-springer/施普林格数据工程手册-流程复核.epub`。
- 237 个 HTML 文档通过内容签名和资源保留检查；EPUBCheck 0 fatal / 0 error / 0 warning。
- 复用此前抽样计划，以 1050px / 420px 渲染 55 张 PNG，包含横向滚动右端截图；另扫描 237 个文档的窄屏布局。自动候选问题和渲染错误均为 0。
- 实际浏览全部 55 张截图的联系表，另放大检查 15 张原始 PNG；核对图片尺度/比例、头像、正文间距、代码缩进与面板、公式、宽表左右端。
- 本轮反馈已通过产物和 PNG 哈希验证，状态为 `agent_reviewed`。本机证据位于 `.style-review-springer/pipeline-audit/`，含 `report.json`、`gallery.html`、`samples.json`、`feedback.json` 及 PNG；这些书籍内容和截图已被 Git 忽略，不随仓库分发。

## 验证边界

本轮验证翻译协议和内容保留，不评价真实模型的译文质量，也未对参考书重新调用翻译 API。浏览器截图验证可重排视口；未认证 Kindle、Apple Books、Calibre 等阅读器的分页、字体选择或深色模式。

窄屏下超宽图片、代码、公式和密集表格保留局部横向滚动，已检查左右端；这是明确接受的阅读取舍。参考书没有 h6，报告如实列入 `missing_categories`。抽样审阅与全书几何扫描不能证明所有位置都达到最佳视觉效果。

测试中的反馈登记用于状态机验证；参考书的 `agent_reviewed` 另基于实际看图后登记，不能把自动测试通过当成任意新书已完成视觉审阅。
