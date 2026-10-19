#!/usr/bin/env python3
"""Render representative EPUB locations and create an evidence-based agent review."""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import tempfile
from pathlib import Path
from xml.etree import ElementTree as ET
from urllib.parse import unquote

from bs4 import BeautifulSoup

from translate_epub import unpack_epub, image_pixel_size, document_files


CATEGORIES = [(f"heading-h{n}", f"h{n}") for n in range(1, 7)] + [
    ("body-pair", "p.reading-source, p.Para, p"),
    ("long-body", "p.reading-source, p.Para, p"),
    ("code", "pre, .ProgramCode, .programcode, .CodeBlock, .code-block, .programlisting, .listing, .screen"),
    ("figure", "figure img, .Figure img, img.epub-image"),
    ("portrait", ".BiographyFigure img, img.reading-portrait"),
    ("display-math", ".Equation, .equation, .formula-block, math[display='block']"),
    ("inline-math", "p:has(math), p:has(img.formula-img)"),
    ("table", "table"), ("list", "ul, ol"), ("navigation", "nav"),
]


def digest(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest() if hasattr(hashlib, "file_digest") else hashlib.sha256(handle.read()).hexdigest()


def book_documents(root: Path) -> list[Path]:
    """Spine order first, then non-spine documents such as separate table pages."""
    ordered = []
    container = root / "META-INF" / "container.xml"
    opfs = []
    if container.exists():
        opfs = [root / n.attrib["full-path"] for n in ET.parse(container).getroot().iter()
                if n.tag.rsplit("}", 1)[-1] == "rootfile" and "full-path" in n.attrib]
    for opf in opfs or sorted(root.rglob("*.opf")):
        opf.resolve().relative_to(root.resolve())
        tree = ET.parse(opf).getroot()
        items = {n.get("id"): (opf.parent / unquote(n.get("href", "").split("#")[0])).resolve()
                 for n in tree.iter() if n.tag.rsplit("}", 1)[-1] == "item"}
        for node in tree.iter():
            target = items.get(node.get("idref")) if node.tag.rsplit("}", 1)[-1] == "itemref" else None
            if target:
                target.relative_to(root.resolve())
            if target and target.is_file() and target.suffix.lower() in {".html", ".xhtml", ".htm"} and target not in ordered:
                ordered.append(target)
    for path in document_files(root):
        if path.resolve() not in ordered:
            ordered.append(path.resolve())
    return ordered


def sample_locations(root: Path, spread: int = 6) -> tuple[list[dict], list[str]]:
    root = root.resolve()
    documents = book_documents(root)
    best = {}
    spread_samples = []
    indices = {round(i * (len(documents) - 1) / max(1, spread - 1)) for i in range(spread)} if documents else set()
    for index, file in enumerate(documents):
        soup = BeautifulSoup(file.read_text(encoding="utf-8"), "html.parser")
        relative = file.relative_to(root).as_posix()
        if index in indices:
            spread_samples.append({"category": f"spine-{index + 1}", "file": relative,
                                   "selector": "body", "index": 0, "spine_position": index + 1})
        for category, selector in CATEGORIES:
            nodes = soup.select(selector)
            if not nodes:
                continue
            def node_score(node):
                length = len(node.get_text())
                if category == "body-pair":
                    sibling = node.find_next_sibling()
                    paired = sibling and "translated-zh" in sibling.get("class", [])
                    return 10000 - abs(length - 180) if paired and length >= 60 else -10000
                return len(node.get("alt", "")) if category in {"figure", "portrait"} else length
            chosen = max(range(len(nodes)), key=lambda i: node_score(nodes[i]))
            score = node_score(nodes[chosen])
            if category in {"figure", "portrait"}:
                # Avoid choosing only the cover; prefer richly captioned images.
                score = len(nodes[chosen].get("alt", ""))
            if category not in best or score > best[category][0]:
                best[category] = (score, {"category": category, "file": relative, "selector": selector,
                                          "index": chosen, "spine_position": index + 1})
        for image_index, image in enumerate(soup.find_all("img")):
            classes = set(image.get("class", []))
            if not ({"epub-image", "reading-illustration"} & classes) or "reading-portrait" in classes:
                continue
            size = image_pixel_size(image.get("src", ""), file.parent)
            if not size or min(size) < 60:
                continue
            w, h = size
            for category, score in (("image-tall", h / w), ("image-wide", w / h), ("image-small", -w * h)):
                if category not in best or score > best[category][0]:
                    best[category] = (score, {"category": category, "file": relative, "selector": "img",
                                              "index": image_index, "spine_position": index + 1})
    missing = [name for name, _ in CATEGORIES if name not in best]
    return [value[1] for value in best.values()] + spread_samples, missing


METRICS_JS = r"""() => {
 const rect = e => { const r=e.getBoundingClientRect(); return {width:r.width,height:r.height}; };
 const images = [...document.images].map(e => {
   const r=rect(e), c=getComputedStyle(e);
   const portrait=e.classList.contains('reading-portrait') || !!e.closest('.BiographyFigure');
   const formula=e.classList.contains('formula-img');
   const localScroll=!!e.closest('.reading-image-scroll');
   const illustration=(!formula && e.matches('.epub-image,.reading-illustration,.reading-portrait')) ||
     (!formula && !!e.closest('figure,.Figure'));
   return {src:e.getAttribute('src'),classes:e.className,...r,naturalWidth:e.naturalWidth,naturalHeight:e.naturalHeight,
     display:c.display,broken:!e.complete || !e.naturalWidth,portrait,illustration,
     tooSmall:illustration && !portrait && e.naturalWidth>=300 && e.naturalHeight>=100 &&
       ((r.width<96 && r.height<96) || (e.naturalWidth/e.naturalHeight>=4 && r.height<72)),
     tooLarge:illustration && ((!localScroll && r.width>innerWidth-12) || r.height>innerHeight*.92),
     distorted:illustration && r.width>0 && r.height>0 && Math.abs((r.width/r.height)/(e.naturalWidth/e.naturalHeight)-1)>.08};
 });
 const code=[...document.querySelectorAll('.reading-code-block,pre,.ProgramCode')].map(e=>{
   const c=getComputedStyle(e);return {...rect(e),font:c.fontFamily,fontSize:c.fontSize,lineHeight:c.lineHeight,
     whiteSpace:c.whiteSpace,overflowX:c.overflowX,clientWidth:e.clientWidth,scrollWidth:e.scrollWidth};
 });
 const pairs=[...document.querySelectorAll('.reading-source')].slice(0,12).map(e=>{
   const z=e.nextElementSibling,c=getComputedStyle(e);return {sourceMargin:c.marginBottom,sourceLineHeight:c.lineHeight,
     translatedMargin:z?getComputedStyle(z).marginBottom:null,translatedLineHeight:z?getComputedStyle(z).lineHeight:null};
 });
 return {viewport:innerWidth,documentWidth:document.documentElement.scrollWidth,
   pageOverflow:document.documentElement.scrollWidth>innerWidth+2,images,code,pairs};
}"""


def validate_feedback(report: dict, feedback: dict) -> None:
    if feedback.get("artifact_sha256") != report["artifact_sha256"]:
        raise ValueError("Feedback does not identify the rendered EPUB hash")
    if feedback.get("status") != "reviewed":
        raise ValueError("An agent must inspect the screenshots and set status=reviewed")
    names = {row["screenshot"] for row in report["screenshots"] if "screenshot" in row}
    reviewed = set(feedback.get("reviewed_screenshots", []))
    if not names or reviewed != names:
        raise ValueError("Feedback must account for every screenshot in this run")
    findings = feedback.get("findings", [])
    if not isinstance(findings, list) or not feedback.get("summary"):
        raise ValueError("Feedback needs a summary and a findings list")
    for finding in findings:
        if finding.get("status") not in {"resolved", "accepted"} or finding.get("screenshot") not in names or not finding.get("reason"):
            raise ValueError("Each finding needs screenshot evidence, reason and resolved/accepted status")
    issue_rows = {issue["id"]: issue for issue in report.get("issues", [])}
    issues = set(issue_rows)
    screenshot_rows = {row["screenshot"]: row for row in report["screenshots"] if "screenshot" in row}
    accounted = set()
    for finding in findings:
        linked = set(finding.get("issue_ids", []))
        if not linked <= issues:
            raise ValueError("Feedback refers to unknown layout issue IDs")
        # An automatic candidate still present in the final report needs an
        # explicit acceptance reason; a fixed defect belongs to an earlier run.
        if linked and finding["status"] != "accepted":
            raise ValueError("Current layout candidates need explicit accepted status or a new render")
        evidence = screenshot_rows[finding["screenshot"]]
        for issue_id in linked:
            issue = issue_rows[issue_id]
            if (issue.get("file"), issue.get("width")) != (evidence.get("file"), evidence.get("width")):
                raise ValueError("Layout issue evidence must show the same document and viewport; capture an additional sample")
        accounted.update(linked)
    if issues - accounted:
        raise ValueError("Every reported layout issue ID needs a disposition")
    if report["render_errors"]:
        raise ValueError("Cannot close a review with render errors")
    if report["issue_count"] and not findings:
        raise ValueError("Explain the reported layout findings before closing the review")


def accept_feedback(epub: Path, output: Path, feedback_path: Path) -> dict:
    report_path = output / "report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if digest(epub) != report["artifact_sha256"]:
        raise ValueError("EPUB changed since screenshots; rerender before closing review")
    for row in report["screenshots"]:
        screenshot = (output / row["screenshot"]).resolve()
        screenshot.relative_to(output.resolve())
        if not row.get("sha256") or digest(screenshot) != row["sha256"]:
            raise ValueError("Screenshot is missing, changed or lacks a hash; rerender")
    feedback = json.loads(feedback_path.read_text(encoding="utf-8"))
    validate_feedback(report, feedback)
    report["status"] = "agent_reviewed"
    report["agent_feedback"] = feedback
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def write_gallery(output: Path, report: dict, compare: Path | None):
    previous = json.loads(compare.read_text(encoding="utf-8")) if compare else {}
    lookup = {(s["category"], s["width"]): s for s in previous.get("screenshots", []) if "screenshot" in s}
    blocks = []
    for sample in report["screenshots"]:
        if "screenshot" not in sample:
            continue
        images = []
        old = lookup.get((sample["category"], sample["width"]))
        if old:
            src = (compare.parent / old["screenshot"]).resolve().as_uri()
            images.append(f'<figure><figcaption>Before</figcaption><img src="{html.escape(src)}"/></figure>')
        images.append(f'<figure><figcaption>Current</figcaption><img src="{html.escape(sample["screenshot"])}"/></figure>')
        blocks.append(f'<section><h2>{html.escape(sample["category"])} · {sample["width"]}px</h2><p>{html.escape(sample["file"])}</p><div>{"".join(images)}</div></section>')
    (output / "gallery.html").write_text('<!doctype html><meta charset="utf-8"><title>EPUB visual review</title>'
        '<style>body{font:16px system-ui;margin:2rem;background:#e9eef2}section{margin:2rem 0}section>div{display:flex;gap:1rem;align-items:start}figure{margin:0;flex:1;min-width:0}img{max-width:100%;border:1px solid #bbb}p{overflow-wrap:anywhere}</style>'
        '<h1>EPUB visual review</h1><p>Chromium reflowed viewports; these are not fixed EPUB page numbers. Agent review is required.</p>' + ''.join(blocks), encoding="utf-8")


def review_epub(epub: Path, output: Path, *, browser_executable: str | None = None,
                spread: int = 6, plan: Path | None = None, compare: Path | None = None,
                scan_all: bool = False, widths: tuple[int, ...] = (1050, 420)) -> dict:
    from playwright.sync_api import sync_playwright

    epub, output = epub.resolve(), output.resolve()
    if spread < 1 or not widths or any(not isinstance(w, int) or w < 1 for w in widths):
        raise ValueError("Sampling count and viewport widths must be positive")
    output.mkdir(parents=True, exist_ok=True)
    if (output / "report.json").exists():
        raise ValueError("Review directory already has a report; use a new directory for each iteration")
    report = {"artifact": str(epub), "artifact_sha256": digest(epub), "status": "awaiting_agent_review",
              "render_errors": [], "screenshots": [], "layout_scan": [], "issue_count": 0,
              "renderer": "Chromium", "widths": list(widths), "height": 1080}
    with tempfile.TemporaryDirectory(prefix=".render-", dir=output) as temp:
        root = Path(temp).resolve()
        unpack_epub(epub, root)
        samples, missing = sample_locations(root, spread)
        if plan:
            samples = json.loads(plan.read_text(encoding="utf-8"))["samples"]
        if not isinstance(samples, list) or not samples:
            raise ValueError("A review needs at least one sample location")
        for sample in samples:
            if not isinstance(sample, dict) or not re.fullmatch(r"[A-Za-z0-9_-]+", sample.get("category", "")):
                raise ValueError("Invalid sample category")
            if not isinstance(sample.get("index"), int) or sample["index"] < 0 or not sample.get("selector"):
                raise ValueError("Invalid sample selector/index")
            target = (root / sample["file"]).resolve()
            target.relative_to(root)
            if not target.is_file():
                raise ValueError(f"Sample file does not exist: {sample['file']}")
        report["missing_categories"] = missing
        (output / "samples.json").write_text(json.dumps({"samples": samples}, ensure_ascii=False, indent=2), encoding="utf-8")
        with sync_playwright() as p:
            kwargs = {"headless": True}
            if browser_executable:
                kwargs["executable_path"] = browser_executable
            browser = p.chromium.launch(**kwargs)
            try:
                for width in widths:
                    context = browser.new_context(viewport={"width": width, "height": 1080}, device_scale_factor=1)
                    # Rendering should never contact publisher analytics/remote resources.
                    context.route("**/*", lambda route: route.continue_() if route.request.url.startswith(("file:", "data:")) else route.abort())
                    page = context.new_page()
                    page.set_default_timeout(15000)
                    for index, sample in enumerate(samples):
                        row = dict(sample, width=width)
                        try:
                            target_file = (root / sample["file"]).resolve()
                            target_file.relative_to(root)
                            page.goto(target_file.as_uri(), wait_until="load")
                            page.evaluate("document.fonts.ready")
                            target = page.locator(sample["selector"]).nth(sample["index"])
                            target.evaluate("e => window.scrollTo(0, Math.max(0,e.getBoundingClientRect().top+scrollY-56))")
                            row["metrics"] = page.evaluate(METRICS_JS)
                            row["target"] = target.evaluate("e=>{const c=getComputedStyle(e),r=e.getBoundingClientRect();return {width:r.width,height:r.height,fontSize:c.fontSize,lineHeight:c.lineHeight,marginBottom:c.marginBottom}}")
                            name = f"{index:02d}-{sample['category']}-{width}.png"
                            page.screenshot(path=str(output / name))
                            row["screenshot"] = name
                            row["sha256"] = digest(output / name)
                            # Include the far edge of wide code/tables/equations.
                            scrolled = target.evaluate("""e=>{
                              const selector='.reading-table-wrap,.reading-equation-scroll,.reading-code-block,.reading-inline-math,.reading-image-scroll';
                              const boxes=[e.closest(selector)||e,...document.querySelectorAll(selector)];let changed=false;
                              for(const b of new Set(boxes)){const r=b.getBoundingClientRect();
                                if(r.top>=innerHeight||r.bottom<=0||b.scrollWidth<=b.clientWidth+2)continue;
                                b.scrollLeft=b.scrollWidth;changed ||= b.scrollLeft>0;}return changed;}""")
                            if scrolled:
                                edge = dict(row, category=sample["category"] + "-scroll-right", screenshot=name.replace(".png", "-right.png"))
                                page.screenshot(path=str(output / edge["screenshot"]))
                                edge["sha256"] = digest(output / edge["screenshot"])
                                report["screenshots"].append(edge)
                        except Exception as exc:
                            row["error"] = str(exc)
                            report["render_errors"].append({"file": sample["file"], "width": width, "error": str(exc)})
                        report["screenshots"].append(row)
                    if scan_all and width == min(widths):
                        for file in book_documents(root):
                            try:
                                page.goto(file.as_uri(), wait_until="load")
                                page.evaluate("document.fonts.ready")
                                report["layout_scan"].append({"file": file.relative_to(root).as_posix(), "width": width, "metrics": page.evaluate(METRICS_JS)})
                            except Exception as exc:
                                report["render_errors"].append({"file": file.relative_to(root).as_posix(), "error": str(exc)})
                    context.close()
            finally:
                browser.close()
    issue_map = {}
    for row in report["screenshots"] + report["layout_scan"]:
        m = row.get("metrics", {})
        candidates = [("pageOverflow", None)] if m.get("pageOverflow") else []
        for index, item in enumerate(m.get("images", [])):
            candidates.extend((kind, index) for kind in ("tooSmall", "tooLarge", "broken", "distorted") if item.get(kind))
        for kind, index in candidates:
            key = (row["file"], row["width"], kind, index)
            issue_map.setdefault(key, {"file": row["file"], "width": row["width"], "kind": kind, "image_index": index})
    report["issues"] = [dict(issue, id=f"issue-{i+1:04d}") for i, issue in enumerate(issue_map.values())]
    report["issue_count"] = len(report["issues"])
    if digest(epub) != report["artifact_sha256"]:
        report["render_errors"].append({"error": "EPUB changed during rendering"})
    if report["render_errors"]:
        report["status"] = "render_failed"
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    write_gallery(output, report, compare)
    template = {"artifact_sha256": report["artifact_sha256"], "status": "pending", "summary": "",
                "reviewed_screenshots": [], "findings": []}
    (output / "feedback.template.json").write_text(json.dumps(template, indent=2), encoding="utf-8")
    (output / "review-task.md").write_text(
        "# Agent visual review\n\nInspect the actual PNGs, including both viewport widths and scroll-right images. "
        "Read report.json; automatic metrics do not establish visual quality.\n\n"
        "Check: tiny/huge/distorted images (distinguish portraits and formula glyphs), bilingual paragraph gaps, "
        "heading hierarchy, nested code padding and indentation, clipped tables/formulas, broken resources and page overflow.\n\n"
        "Record observations with screenshot filenames and reasons. Fix shared CSS/DOM classification in the project, "
        "run optimize_epub.py to a new EPUB, then rerender with --plan pointing to samples.json and --compare pointing "
        "to this report.json. Never inject screenshot-only CSS. Rerun EPUBCheck and content preservation checks.\n\n"
        "Repeat until resolved or explicitly accepted with reasons. Fill feedback.template.json into feedback.json: "
        "status=reviewed, summary, every reviewed_screenshots filename, and findings [{screenshot, issue, change, "
        "status: resolved|accepted, reason, issue_ids: []}]. Each current report.issues ID must be explicitly "
        "accepted with a reason, or fixed and rerendered. The hash must identify the FINAL rendered artifact. "
        "Close with review_epub.py FINAL.epub --output FINAL_REVIEW_DIR --accept-feedback feedback.json. "
        "Missing coverage/render errors or unreviewed screenshots must be reported, not called a pass.\n\n"
        "These are reflowed XHTML viewport screenshots, not stable page numbers or a real-reader certification.\n",
        encoding="utf-8")
    print(f"[STYLE REVIEW] {output / 'gallery.html'}; screenshots={len(report['screenshots'])}, issues={report['issue_count']}, status={report['status']}")
    if report["render_errors"]:
        raise RuntimeError(f"Screenshot rendering failed; see {output / 'report.json'}")
    return report


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("epub", type=Path)
    ap.add_argument("--output", required=True, type=Path)
    ap.add_argument("--browser-executable")
    ap.add_argument("--spread", type=int, default=6)
    ap.add_argument("--plan", type=Path, help="Reuse sample locations across iterations")
    ap.add_argument("--compare", type=Path, help="Previous report.json for before/current gallery")
    ap.add_argument("--scan-all", action="store_true", help="Scan all HTML documents at narrow width")
    ap.add_argument("--accept-feedback", type=Path, help="Validate and record agent review without rendering again")
    args = ap.parse_args()
    try:
        if args.accept_feedback:
            accept_feedback(args.epub, args.output, args.accept_feedback)
            print("[STYLE REVIEW] Agent feedback recorded for the rendered artifact")
        else:
            if args.spread < 1:
                raise ValueError("--spread must be positive")
            review_epub(args.epub, args.output, browser_executable=args.browser_executable,
                        spread=args.spread, plan=args.plan, compare=args.compare, scan_all=args.scan_all)
    except Exception as exc:
        ap.exit(2, f"[ERR] Screenshot review incomplete: {exc}\n")


if __name__ == "__main__":
    main()
