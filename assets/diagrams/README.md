# README 工作流图 / README workflow diagrams

本目录保留中英文 PNG、对应的 HTML 图源及浏览器渲染记录。图源由 `scripts/render_workflow_diagrams.py` 统一生成，避免中英文节点或连线逐渐失去同步。

This directory contains Chinese/English PNGs, editable HTML sources and a browser render audit. A shared generator keeps both languages' topology synchronized.

| 图 / Figure | 中文 / Chinese | English |
|---|---|---|
| 翻译生成与已有产物优化 / Translation and restyling | `workflow-zh.html` / `.png` | `workflow-en.html` / `.png` |
| 截图审阅反馈 / Screenshot feedback | `visual-review-zh.html` / `.png` | `visual-review-en.html` / `.png` |

样式：Diagram Design 默认浅色，paper `#f5f5f5`、ink `#2d3142`、accent `#bf4520`。标题为 Instrument Serif，节点为 Geist，技术标记为 Geist Mono；中文扩展使用 Noto Serif SC / Noto Sans SC，保留系统回退。HTML 通过 Google Fonts 加载字体；PNG 固化本次实际渲染结果。

Style: Diagram Design default light. Instrument Serif titles, Geist node names and Geist Mono technical labels; Chinese extends the stacks with Noto Serif SC / Noto Sans SC and system fallbacks. HTML loads Google Fonts; PNGs retain the rendered typography.

两张图均使用 Flowchart 布局。生成图有 8 个节点、7 条连线；反馈图有 7 个节点、7 条连线。每图仅突出 1 个节点，所有连接正交，转角半径 8px。画布采用 `doc-inline` 960×600，PNG 导出倍率为 2，即 1920×1200；仅导出 SVG 区域。

Both figures use flowchart layouts: 8 nodes/7 edges for generation and 7 nodes/7 edges for review. Each has one focal node, orthogonal connectors and 8px rounded elbows. The `doc-inline` canvas is 960×600; SVG-only PNG capture at 2× produces 1920×1200 assets.

## 内容边界 / Content boundaries

- 第一图展示规范检查成功的主线。缓存、模型兼容与结构保护合并在阶段说明中；详细参数、翻译失败兜底及诊断保留规则留在 README / SKILL.md。
- 已有产物通过 `optimize_epub.py` 走共享样式处理，不经过 LLM；其内容保留审计不应误认为普通翻译阶段的通用逐字节审计。
- 第二图描述项目要求的 agent 工作流。脚本产生截图与审阅任务；图片分析和修改由 agent 执行。`--style-review` 为 CLI 可选入口，`--scan-all` 也需显式启用。
- `agent_reviewed` 表示反馈已登记，不代表所有阅读器或固定分页的视觉认证。允许有理由地接受限制，渲染错误或截图覆盖缺失则不能结项。

- Figure 1 shows the successful validation path. Detailed parameters, translation fallbacks and failure diagnostics remain in the README / SKILL.md.
- Restyling shares layout code without LLM calls. Its content integrity audit is specific to that path.
- Figure 2 documents the agent procedure: scripts capture evidence and prepare a review task; an agent performs visual analysis and edits. Both capture and the all-document scan are explicit CLI options.
- `agent_reviewed` records feedback, not universal reader certification. Accepted limitations need reasons; render errors and missing screenshot coverage prevent closure.

## 生成与验证 / Build and verify

```powershell
uv run --locked --extra review python scripts/render_workflow_diagrams.py --export
# 或 / or:
uv run --locked --extra review python scripts/render_workflow_diagrams.py --export --browser-executable <chromium-path>
```

不带 `--export` 时只生成 HTML。安装了 Diagram Design 时，可对每份 HTML 运行其 `scripts/self_check.py`。导出器检查节点内文字边界、390px 视口的局部滚动和字体加载情况，记录到 `render-audit.json`。字体网络请求超时时，须报告字体回退并检查导出的 PNG。

Without `--export`, only HTML is regenerated. Run Diagram Design's `scripts/self_check.py` against each HTML when the skill is installed. Export verifies node text bounds, local scrolling at 390px and loaded fonts in `render-audit.json`. If font loading times out, report fallback typography and inspect the PNGs.
