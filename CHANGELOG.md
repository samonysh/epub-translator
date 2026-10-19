# 更新记录 / Changelog

## 1.4.0 — 2026-10-19

更新时间 / Updated: 2026-10-19

- 优化图片分类与尺寸、双语段落间距、代码块、公式和宽表的阅读样式。
- 新增已有 EPUB 的内容保留样式优化，以及抽样截图、全书布局扫描、agent 看图改进与反馈登记流程。
- 修复公式与代码保真、目录/图注覆盖、EPUB 2 兼容、无效缓存、失败阻断和旧产物保留问题。
- 加入中英文工作流 PNG、可编辑图源及流程审计记录。功能快照通过 93 项测试；记录见 `docs/workflow-audit.md`。

- Improve image sizing, bilingual spacing, code blocks, equations and wide tables.
- Add content-preserving restyling and screenshot-driven agent review with layout scans and validated feedback.
- Fix content preservation, translation coverage, EPUB 2 compatibility, invalid caches and failure handling.
- Add bilingual workflow PNGs, editable sources and audit evidence. The feature snapshot passed 93 tests.

发布：[`v1.4.0`](https://github.com/samonysh/epub-translator/releases/tag/v1.4.0)。版本号从 1.3.0 提升至 1.4.0，以反映新增的样式优化与截图审阅入口；依赖版本继续由 `uv.lock` 锁定，`epubcheck==5.3.0` 保持项目指定的固定版本。

Release: [`v1.4.0`](https://github.com/samonysh/epub-translator/releases/tag/v1.4.0). The minor-version increment reflects the new restyling and screenshot-review entry points. Dependency versions remain locked; EPUBCheck stays explicitly pinned to 5.3.0.

## 1.3.0 — 2026-09-30

- 增加 PyPI `epubcheck==5.3.0` 成品检查、JSON 诊断及 uv 项目依赖管理。
- Add packaged EPUB validation, JSON diagnostics and uv dependency management.
- 已有 Git 标签：[`v1.3.0`](https://github.com/samonysh/epub-translator/tree/v1.3.0)。该标签不是 GitHub Release 条目。

## 发布状态核对 / Publication status

截至 / Verified on 2026-10-19:

| 项目 / Item | 版本 / Version |
| --- | --- |
| 当前版本 / Current version | 1.4.0 |
| 最新 Git 标签 / Latest Git tag | v1.4.0 |
| 最新 GitHub Release 条目 / Latest GitHub Release entry | [v1.4.0](https://github.com/samonysh/epub-translator/releases/tag/v1.4.0) · 2026-10-19 |

此前 GitHub Release 条目为 [`v1.1.0`](https://github.com/samonysh/epub-translator/releases/tag/v1.1.0)（2026-07-02）；`v1.3.0` 仅有 Git 标签。本次发布使项目元数据、标签和 Release 保持一致。

更新版本时同步 `pyproject.toml`、`uv.lock` 的项目条目、README 中英文版、`SKILL.md` 的元数据与标题。日期使用实际更新日期；历史审计、测试夹具日期、EPUB/XML 协议版本和第三方依赖版本不随项目版本整体替换。

Keep project metadata, the lockfile project entry, both READMEs and SKILL metadata/header synchronized. Preserve historical evidence, fixture dates, EPUB/XML protocol versions and independently versioned dependencies.
