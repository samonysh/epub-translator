#!/usr/bin/env python3
"""Apply the generator's reading layout to an existing EPUB, without translation."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
import zipfile
from pathlib import Path

from bs4 import BeautifulSoup

import translate_epub as T


def content_signature(soup):
    """Layout hooks may change; reader content and resource references may not."""
    return {
        "text": soup.get_text(),
        "images": [i.get("src") for i in soup.find_all("img")],
        "math": [str(m) for m in soup.find_all("math")],
        "svg": [str(m) for m in soup.find_all("svg")],
        "code": [p.get_text() for p in soup.find_all("pre")],
        "anchors": [(n.get("id"), n.get("href")) for n in soup.find_all(True)
                    if n.has_attr("id") or n.has_attr("href")],
    }


def optimize_epub(source: Path, output: Path, *, check: bool = True) -> dict:
    source, output = source.resolve(), output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    with tempfile.TemporaryDirectory(prefix=".epub-style-", dir=output.parent) as temp:
        root = Path(temp) / "extract"
        T.unpack_epub(source, root)
        chapters = T.document_files(root)
        if not chapters:
            raise ValueError("EPUB contains no HTML documents")
        epub_version = T.read_epub_version(root)
        for chapter in chapters:
            raw = chapter.read_text(encoding="utf-8")
            # Reject malformed input rather than silently repairing it before
            # the integrity baseline is recorded.
            T.parse_xml_document(raw)
            soup = T.parse_document(raw)
            before = content_signature(soup)
            T.normalise_images_for_reading(soup, set(), html_dir=chapter.parent, deduplicate=False)
            T.normalise_reading_layout(soup)
            if content_signature(soup) != before:
                raise ValueError(f"Content integrity check failed: {chapter.relative_to(root)}")
            serialized = T.serialize_document(soup, epub_version)
            if content_signature(T.parse_document(serialized)) != before:
                raise ValueError(f"Content changed during serialization: {chapter.relative_to(root)}")
            chapter.write_text(serialized, encoding="utf-8", newline="")
        T.inject_css(root)
        candidate = Path(temp) / "candidate.epub"
        T.pack_epub(root, candidate)
        # Everything except HTML and the project-owned stylesheet must remain
        # byte-identical. OPF changes are limited to registering that stylesheet.
        with zipfile.ZipFile(source) as old, zipfile.ZipFile(candidate) as new:
            for name in old.namelist():
                if name.endswith("/") or Path(name).suffix.lower() in {".xhtml", ".html", ".htm", ".opf"} or Path(name).name == "translated.css":
                    continue
                if old.read(name) != new.read(name):
                    raise ValueError(f"Resource changed unexpectedly: {name}")
        result = {"source_sha256": source_hash, "chapters": len(chapters),
                  "content_preserved": True, "epubcheck": "not_run"}
        if check:
            valid, report, fatal, errors, warnings = T.validate_epub(candidate)
            result["epubcheck"] = {"valid": valid, "fatal": fatal, "errors": errors, "warnings": warnings}
            # Always preserve diagnostics, including a failed candidate's report.
            output.with_suffix(".epubcheck.json").write_bytes(report.read_bytes())
            if not valid:
                raise ValueError(f"EPUBCheck failed; output not replaced. See {output.with_suffix('.epubcheck.json')}")
        os.replace(candidate, output)
    result["output_sha256"] = hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_suffix(".style.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("input", type=Path)
    ap.add_argument("output", type=Path, help="Exact output filename; may equal input for explicit in-place optimization")
    ap.add_argument("--skip-epubcheck", action="store_true", help="Development only; result is labelled unchecked")
    ap.add_argument("--style-review", action="store_true")
    ap.add_argument("--review-dir", type=Path)
    ap.add_argument("--browser-executable")
    args = ap.parse_args()
    try:
        result = optimize_epub(args.input, args.output, check=not args.skip_epubcheck)
        print(json.dumps(result, ensure_ascii=False))
        if args.style_review:
            from review_epub import review_epub
            review_epub(args.output, args.review_dir or args.output.parent / f".style-review-{args.output.stem}",
                        browser_executable=args.browser_executable)
    except Exception as exc:
        ap.exit(2, f"[ERR] {exc}\n")


if __name__ == "__main__":
    main()
