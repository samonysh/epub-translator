#!/usr/bin/env python3
"""Build bilingual README diagrams; optionally export their SVG frames to PNG.

Uses Diagram Design's default light palette and doc-inline 960x600 preset.
HTML is the editable source. PNG export requires the optional review extra.
"""
from __future__ import annotations

import argparse
import html
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "assets" / "diagrams"
FONTS = ("https://fonts.googleapis.com/css2?family=Instrument+Serif:ital@0;1"
         "&family=Geist:wght@400;500;600&family=Geist+Mono:wght@400;500;600"
         "&family=Noto+Sans+SC:wght@400;500;600&family=Noto+Serif+SC:wght@400&display=swap")


def text(x, y, value, role="name", anchor="start"):
    return f'<text x="{x}" y="{y}" class="{role}" text-anchor="{anchor}">{html.escape(value)}</text>'


def box(key, x, y, w, h, title, lines=(), tag="", kind="step", focal=False, tech=None):
    radius = 20 if kind == "terminal" else 6
    fill = "#f5f5f5" if kind == "terminal" else "#ffffff"
    stroke = "#4f5d75" if kind == "terminal" else "#2d3142"
    if focal:
        fill, stroke = "rgba(191,69,32,0.08)", "#bf4520"
    chunks = [f'<g id="{key}" data-node="{key}" data-x="{x}" data-y="{y}" data-w="{w}" data-h="{h}">',
              f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{radius}" fill="#f5f5f5"/>',
              f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{radius}" fill="{fill}" stroke="{stroke}"/>']
    if tag:
        tw = max(28, ((len(tag) * 5 + 15) // 4) * 4)
        chunks += [f'<rect x="{x+12}" y="{y+8}" width="{tw}" height="12" rx="2" fill="#f5f5f5" stroke="#bfc0c0"/>',
                   text(x + 12 + tw / 2, y + 17, tag, "tag", "middle")]
    start = y + (40 if tag else 24)
    chunks.append(text(x + 16, start, title))
    for i, value in enumerate(lines):
        chunks.append(text(x + 16, start + 20 * (i + 1), value, "detail"))
    if tech:
        chunks.append(text(x + 16, y + h - 12, tech, "tech"))
    chunks.append("</g>")
    return "".join(chunks)


def edge(path, *, dashed=False):
    dash = ' stroke-dasharray="5,4"' if dashed else ""
    return f'<path class="edge" d="{path}" fill="none" stroke="#4f5d75" stroke-width="1.2"{dash} marker-end="url(#arrow)"/>'


def label(x, y, value, chinese=False, width=None):
    w = width or max(32, ((len(value) * (12 if chinese else 5) + 20) // 4) * 4)
    return (f'<rect x="{x-w/2}" y="{y-15}" width="{w}" height="20" rx="2" fill="#f5f5f5"/>'
            + text(x, y, value, "branch-zh" if chinese else "branch", "middle"))


def document(slug, title, subtitle, body, legend, zh):
    style = """
    :root { --font-sans:'Geist','Noto Sans SC','Microsoft YaHei',system-ui,sans-serif;
      --font-serif:'Instrument Serif','Noto Serif SC','Songti SC',SimSun,serif;
      --font-mono:'Geist Mono',ui-monospace,monospace; }
    * { box-sizing:border-box; }
    body { margin:0;padding:24px;background:#f5f5f5;color:#2d3142;font-family:var(--font-sans); }
    main { max-width:960px;min-width:0;margin:0 auto; }
    .diagram-container { width:100%;overflow-x:auto; }
    svg { display:block;width:100%;min-width:960px; }
    svg text { fill:#2d3142; }
    .title { font:400 28px var(--font-serif); }
    .name { font:600 12px var(--font-sans); }
    .detail,.subtitle,.legend,.branch-zh { font:400 12px var(--font-sans);fill:#4f5d75; }
    .subtitle { fill:#5a6580; }
    .tech { font:400 9px var(--font-mono);fill:#5a6580; }
    .tag,.branch,.eyebrow { font:500 8px var(--font-mono);letter-spacing:.06em;fill:#5a6580; }
    .branch-zh { font-weight:500; }
    @media print { .diagram-container { overflow-x:visible; } svg { min-width:0; } }
    """
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 960 600" role="img" aria-labelledby="{slug}-title {slug}-desc">'
           f'<title id="{slug}-title">{html.escape(title)}</title>'
           f'<desc id="{slug}-desc">{html.escape(subtitle)}</desc>'
           '<defs><marker id="arrow" markerWidth="8" markerHeight="6" refX="7" refY="3" orient="auto"><polygon points="0 0,8 3,0 6" fill="#4f5d75"/></marker>'
           '<marker id="arrow-accent" markerWidth="8" markerHeight="6" refX="7" refY="3" orient="auto"><polygon points="0 0,8 3,0 6" fill="#bf4520"/></marker>'
           '<marker id="arrow-link" markerWidth="8" markerHeight="6" refX="7" refY="3" orient="auto"><polygon points="0 0,8 3,0 6" fill="#2e5aa8"/></marker></defs>'
           '<rect width="960" height="600" fill="#f5f5f5"/>'
           + text(40, 48, "EPUB TRANSLATOR / WORKFLOW", "eyebrow")
           + text(40, 88, title, "title") + text(40, 116, subtitle, "subtitle") + body
           + '<line x1="40" y1="552" x2="920" y2="552" stroke="#bfc0c0"/>' + legend + '</svg>')
    return (f'<!doctype html><html lang="{"zh-CN" if zh else "en"}"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(title)}</title>'
            f'<link href="{html.escape(FONTS, quote=True)}" rel="stylesheet"><style>{style}</style></head>'
            f'<body><main><div class="diagram-container">{svg}</div></main></body></html>')


def generation(zh):
    slug = "workflow-zh" if zh else "workflow-en"
    t = lambda cn, en: cn if zh else en
    # Flow is a two-row snake, plus one independent restyling entry point.
    # Each route has its own port and open corridor; no connector crosses a node.
    body = ''.join([
        edge("M 280 208 H 328"), edge("M 568 208 H 616"), edge("M 736 256 V 312"),
        edge("M 616 368 H 568"), edge("M 328 368 H 280"), edge("M 160 424 V 468"),
        edge("M 616 500 H 456 Q 448 500 448 492 V 424", dashed=True),
        label(520, 486, "RESTYLE", width=64),
        box('source', 40, 160, 240, 96, t('输入英文 EPUB', 'Source EPUB'),
            (t('配置模型、接口与并发', 'Configure model and concurrency'),), kind='terminal', tech='config.json + environment'),
        box('scan', 328, 160, 240, 96, t('解包与结构扫描', 'Unpack and scan'),
            (t('章节、导航、表格和元数据', 'Chapters, navigation, tables, title'),), tag='SCAN', tech='HTML / MathML / OPF / NCX'),
        box('translate', 616, 160, 240, 96, t('批量并行翻译', 'Batch translation'),
            (t('缓存去重、断点续传、重试', 'Cache, resume, retries, ID pairing'),), tag='LLM', focal=True, tech='OpenAI-compatible / JSON batches'),
        box('bilingual', 616, 312, 240, 112, t('写回双语内容', 'Bilingual write-back'),
            (t('英文在前，中文紧随其后', 'English first, Chinese underneath'),
             t('双表对照；标题与目录同行', 'Paired tables; inline title and TOC')), tag='DOM'),
        box('layout', 328, 312, 240, 112, t('共享阅读样式', 'Shared reading layout'),
            (t('图片分类、段落节奏与代码', 'Image sizing, spacing and code'),
             t('公式与宽表局部滚动', 'Local scrolling for math and tables')), tag='STYLE', tech='translated.css + DOM hooks'),
        box('package', 40, 312, 240, 112, t('打包与规范检查', 'Package and validate'),
            (t('mimetype 首项且不压缩', 'First, uncompressed mimetype'),
             t('EPUBCheck 检查与 JSON 报告', 'EPUBCheck + JSON diagnostics')), tag='CHECK'),
        box('artifact', 40, 468, 240, 64, t('规范检查后的 EPUB', 'Validated EPUB'),
            (t('可进入截图反馈流程 → 下一图', 'Visual review follows in figure 2'),), kind='terminal'),
        box('existing', 616, 468, 240, 64, t('已有 EPUB：仅优化样式', 'Existing EPUB: restyle only'),
            (t('不调用模型；执行内容保留审计', 'No LLM calls; content integrity audit'),), kind='terminal'),
    ])
    legend = text(40, 578, t('实线：翻译生成   ·   虚线：已有产物样式优化', 'Solid: translation path   ·   Dashed: existing-artifact restyling'), 'legend')
    return slug, document(slug, t('从英文 EPUB 到双语阅读产物', 'From source EPUB to a bilingual edition'),
        t('两种入口共享排版与规范检查；视觉审阅单独进行。', 'Translation and restyling share layout and validation; visual review is a separate step.'), body, legend, zh)


def review(zh):
    slug = 'visual-review-zh' if zh else 'visual-review-en'
    t = lambda cn, en: cn if zh else en
    body = ''.join([
        edge('M 104 216 V 256'), edge('M 140 352 V 416'), edge('M 240 464 H 328'),
        edge('M 400 416 V 392 Q 400 384 408 384 H 728 Q 736 384 736 376 V 352'),
        edge('M 472 464 H 544 Q 552 464 552 456 V 440 Q 552 432 560 432 H 616'),
        edge('M 736 464 V 488'),
        edge('M 616 304 H 584 Q 576 304 576 296 V 240 Q 576 232 568 232 H 184 Q 176 232 176 240 V 256', dashed=True),
        label(536, 372, t('需要改进', 'REVISE'), chinese=zh, width=72),
        label(584, 420, t('可结项', 'READY'), chinese=zh, width=56),
        box('epub', 40, 152, 200, 64, t('最终 EPUB', 'Built EPUB'),
            ('--style-review / review_epub.py',), kind='terminal'),
        box('capture', 40, 256, 200, 96, t('抽样截图与布局指标', 'Capture and measure'),
            (t('宽窄屏、长内容右端', 'Wide, narrow, right edge'),), tag='RENDER', tech='1050px / 420px / --scan-all'),
        box('agent', 40, 416, 200, 96, t('Agent 查看真实截图', 'Agent inspects screenshots'),
            (t('图片、间距、代码与溢出', 'Images, spacing, code, overflow'),), tag='AGENT', focal=True, tech='awaiting_agent_review'),
        box('revise', 616, 256, 240, 96, t('修改样式并重建', 'Fix layout and rebuild'),
            (t('共享 CSS / 分类逻辑；再做审计', 'Shared CSS / DOM; repeat checks'),), tag='ITERATE', tech='optimize_epub.py / EPUBCheck'),
        box('feedback', 616, 400, 240, 64, t('记录处理结果与接受理由', 'Record resolutions and exceptions'),
            (t('同一产物哈希；覆盖全部截图', 'Match artifact hash + all screenshots'),)),
        box('complete', 616, 488, 240, 48, t('视觉审阅已登记', 'Visual review recorded'),
            (), kind='terminal', tech='agent_reviewed'),
        '<g data-node="decision" data-x="328" data-y="416" data-w="144" data-h="96">'
        '<polygon points="400,416 472,464 400,512 328,464" fill="#ffffff" stroke="#2d3142"/>'
        + text(400, 460, t('仍需修改？', 'More changes?'), 'name', 'middle')
        + text(400, 480, t('问题或缺项', 'Issues / gaps'), 'detail', 'middle') + '</g>',
    ])
    legend = text(40, 576, t('虚线：修改后重用样本再截图；生成截图与指标不等于视觉审阅通过。',
                            'Dashed: rerender the same samples after changes; screenshots and metrics alone are not visual approval.'), 'legend')
    return slug, document(slug, t('基于产物截图的样式反馈闭环', 'Screenshot-driven style feedback'),
        t('截图入口可选；agent 交付按项目工作流完成实际看图、改进与登记。',
          'Capture is opt-in; the project agent workflow requires actual inspection, correction and feedback.'), body, legend, zh)


def export_diagrams(browser_executable=None):
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError, sync_playwright

    records = []
    with sync_playwright() as p:
        options = {'headless': True}
        if browser_executable:
            options['executable_path'] = browser_executable
        browser = p.chromium.launch(**options)
        context = browser.new_context(viewport={'width': 1100, 'height': 800}, device_scale_factor=2)
        try:
            for source in sorted(OUTPUT.glob('*.html')):
                page = context.new_page()
                page.goto(source.resolve().as_uri() + '?motion=static', wait_until='domcontentloaded')
                fallback = False
                try:
                    page.wait_for_load_state('networkidle', timeout=15000)
                except PlaywrightTimeoutError:
                    page.evaluate('window.stop()')
                    page.wait_for_timeout(4000)
                    fallback = True
                page.evaluate('document.fonts.ready')
                # Check actual text bounds, including CJK fallback metrics.
                overflow = page.locator('[data-node]').evaluate_all('''nodes => nodes.flatMap(n=>{
                  const x=+n.dataset.x,y=+n.dataset.y,w=+n.dataset.w,h=+n.dataset.h;
                  return [...n.querySelectorAll('text')].flatMap(t=>{const b=t.getBBox();
                    return b.x<x+4||b.x+b.width>x+w-4||b.y<y||b.y+b.height>y+h
                      ? [{node:n.dataset.node,text:t.textContent,bounds:{x:b.x,y:b.y,width:b.width,height:b.height}}] : [];});})''')
                if overflow:
                    raise ValueError(f'{source.name}: text overflows node: {overflow}')
                fonts = page.evaluate('''() => ({
                  title:getComputedStyle(document.querySelector('.title')).fontFamily,
                  name:getComputedStyle(document.querySelector('.name')).fontFamily,
                  loaded:[...new Set([...document.fonts].filter(f=>f.status==='loaded').map(f=>f.family))]
                })''')
                svg = page.locator('svg').first
                svg.evaluate('''el=>{const b=el.viewBox.baseVal;
                  for(const [axis,size] of [['width',b.width],['height',b.height]])
                    for(const prefix of ['', 'min-', 'max-'])el.style.setProperty(prefix+axis,size+'px','important');
                  for(let a=el.parentElement;a;a=a.parentElement)a.style.setProperty('overflow','visible','important');}''')
                output = source.with_suffix('.png')
                svg.screenshot(path=str(output), omit_background=True)
                page.set_viewport_size({'width':390,'height':800})
                page.reload(wait_until='domcontentloaded')
                page.evaluate('document.fonts.ready')
                narrow = page.evaluate('''()=>({pageWidth:document.documentElement.scrollWidth,
                  viewport:innerWidth,localScroll:document.querySelector('.diagram-container').scrollWidth>
                    document.querySelector('.diagram-container').clientWidth})''')
                if narrow['pageWidth'] > 392 or not narrow['localScroll']:
                    raise ValueError(f'{source.name}: narrow-screen scrolling failed: {narrow}')
                records.append({'file': output.name, 'pixels': [1920,1200], 'fallback_timeout':fallback,
                                'fonts': fonts, 'narrow_screen':narrow, 'text_overflow':overflow})
                page.close()
        finally:
            browser.close()
    (OUTPUT/'render-audit.json').write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(records, ensure_ascii=False, indent=2))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--export', action='store_true', help='Export PNGs using the installed Playwright runtime')
    ap.add_argument('--browser-executable', help='Optional Chromium executable; otherwise Playwright default')
    args = ap.parse_args()
    OUTPUT.mkdir(parents=True, exist_ok=True)
    for zh in (True, False):
        for build in (generation, review):
            slug, source = build(zh)
            (OUTPUT/f'{slug}.html').write_text(source, encoding='utf-8', newline='')
    if args.export:
        export_diagrams(args.browser_executable)


if __name__ == '__main__':
    main()
