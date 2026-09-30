#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Repair bilingual EPUBs whose illustrations were clamped to ~24px, and unify
every project baseline to the 85% illustration ceiling.

Background (see ``.epub-work/EPUB插图尺寸问题分析与优化方案.md``): the reading
baseline used to size illustrations with ``img.epub-image { max-width: 82% }``
while ``img.epub-inline-image { max-width: 1.5em }`` is about 24px at 16px text.
Any illustration the generator tagged as "inline" -- because its parent happened
to contain caption text -- therefore rendered as a thumbnail.

This tool repairs already-generated files, in place:

1. ``epub-inline-image`` -> ``epub-image`` for images that really are block
   illustrations (figure/cover container, or a large resource plus caption-like
   alt text).  A tiny placeholder stuffed into a code listing is demoted the
   other way instead, so it cannot break the code line.
2. Every project-owned illustration rule is rewritten to the unified 85% block
   (``assets/translated.css`` is the source of truth for that block).
3. ``mimetype`` stays the first entry and STORED, entry order and ZipInfo
   metadata are preserved, and every untouched payload must stay byte-identical.

HTML is edited with targeted string replacement on the raw text, never
re-serialised, so translated prose, entities and whitespace stay untouched.

Usage::

    python scripts/fix_epub_image_size.py --backup-dir <dir> [--dry-run] <book.epub> [...]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import posixpath
import re
import shutil
import sys
import zipfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

import translate_epub as T  # noqa: E402  (shared classifier and image sniffing)

REPO = Path(__file__).resolve().parents[1]
ASSET_CSS = REPO / "assets" / "translated.css"

UNIFIED_MARKER = "/* === Project illustration sizing"
UNIFIED_BLOCK = """/* === Project illustration sizing: preserve intrinsic size, cap at 85% ========
   Container selectors are listed explicitly so this rule outranks the
   publisher's own `img { max-width: ... }`, `#sbo-rt-content img` and
   `.Figure img` declarations whatever order the stylesheets load in. */
.epub-image-container { text-align: center; }
img.epub-image,
figure img.epub-image,
div.figure img.epub-image,
div.informalfigure img.epub-image,
div.mediaobject img.epub-image,
div.MediaObject img.epub-image,
.CoverFigure img.epub-image,
.epub-image-container img.epub-image,
#sbo-rt-content img.epub-image {
  display: block !important;
  width: auto !important;
  max-width: 85% !important;
  height: auto !important;
  max-height: 96vh;
  margin: 1em auto !important;
  padding: 0 !important;
}
/* Second line of defence: an illustration mis-tagged as an inline glyph inside a
   figure or cover container must not collapse to 1.5em (~24px). */
figure img.epub-inline-image,
div.figure img.epub-inline-image,
div.informalfigure img.epub-inline-image,
div.mediaobject img.epub-inline-image,
div.MediaObject img.epub-inline-image,
.CoverFigure img.epub-inline-image,
.epub-image-container img.epub-inline-image,
#sbo-rt-content img.epub-inline-image {
  display: block !important;
  width: auto !important;
  max-width: 85% !important;
  height: auto !important;
  max-height: 96vh !important;
  margin: 1em auto !important;
}
/* Publisher-authored sizes (width=/height= attributes) are honoured inside a text
   run instead of being crushed by the sentence-height cap. */
img.epub-fixed-image {
  display: inline-block !important;
  max-width: 85% !important;
  height: auto !important;
  max-height: none !important;
  margin: 0.4em auto !important;
  vertical-align: middle;
}
/* An inline width/height style is a publisher size hint; keep it within the page. */
img.epub-styled-image {
  display: inline-block !important;
  max-width: 85% !important;
  max-height: none !important;
  margin: 0.4em auto !important;
  vertical-align: middle;
}
/* Genuine inline glyphs -- inline maths, registry icons -- keep a sentence height. */
img.epub-inline-image { width: auto !important; max-width: 1.5em !important; max-height: 1.5em !important; }
/* Glyphs inside code listings stay inline and never widen the code column. */
pre img.epub-inline-image,
code img.epub-inline-image,
.ProgramCode img.epub-inline-image,
.LineGroup img.epub-inline-image,
.FixedLine img.epub-inline-image,
.programlisting img.epub-inline-image,
.screen img.epub-inline-image {
  display: inline !important;
  vertical-align: middle !important;
  width: auto !important;
  max-width: 85% !important;
  max-height: 1.4em !important;
  margin: 0 0.1em !important;
}
/* Chapter banners and heading icons are not sentence glyphs: keep natural size. */
h1 img.epub-image, h1 img.epub-inline-image,
h2 img.epub-image, h2 img.epub-inline-image,
h3 img.epub-image, h3 img.epub-inline-image,
h4 img.epub-image, h4 img.epub-inline-image,
h5 img.epub-image, h5 img.epub-inline-image,
h6 img.epub-image, h6 img.epub-inline-image {
  display: inline !important;
  width: auto !important;
  max-width: 85% !important;
  height: auto !important;
  max-height: none !important;
  margin: 0 auto !important;
}
/* Publisher `figure.width-N` hints also shrink the figure box itself, which would
   turn an 85% image into ~40% of the page; neutralise them. */
figure[class*="width-"], div[class*="width-"] {
  width: auto !important;
  max-width: 100% !important;
}"""

COMMENT_RE = re.compile(r"/\*(?:[^*]|\*(?!/))*\*/")
RULE_RE = re.compile(r"(?P<pre>[^{}]+?\{)(?P<body>[^{}]*)\}", re.S)
IMG_TAG = re.compile(r"<img\b[^>]*>")
CLASS_ATTR = re.compile(r'(class=")([^"]*)(")')
GENERIC_CONTAINER_RE = re.compile(
    r"(figure|informalfigure|mediaobject|coverfigure)\s+img", re.I
)
WIDTH_N_SEL_RE = re.compile(r'\[class\*="width-"\]')
# `.epub-image` / `img.epub-inline-image`, but not `.epub-image-container`.
EPUB_CLASS_RE = re.compile(r"(?<![\w-])epub-(?:inline-)?image(?![\w-])")
# Braces and semicolons inside comments must not be read as CSS structure.
_MASK_CHARS = {"{": "\x00", "}": "\x01", ";": "\x02"}
_UNMASK_CHARS = {value: key for key, value in _MASK_CHARS.items()}


def _mask_comments(css: str) -> str:
    return COMMENT_RE.sub(lambda m: m.group(0).translate(str.maketrans(_MASK_CHARS)), css)


def _unmask(text: str) -> str:
    return text.translate(str.maketrans(_UNMASK_CHARS))


def load_unified_block() -> str:
    """Return the canonical block from the asset when available (drift guard)."""
    try:
        css = ASSET_CSS.read_text(encoding="utf-8")
    except OSError:
        return UNIFIED_BLOCK
    start = css.find(UNIFIED_MARKER)
    if start == -1:
        return UNIFIED_BLOCK
    end = css.find('figure[class*="width-"]', start)
    if end == -1:
        return UNIFIED_BLOCK
    end = css.find("}", end) + 1
    return css[start:end].strip()


# ============ CSS ============
def _selectors(pre: str) -> List[str]:
    """Comma-separated selectors of a rule, comments removed and whitespace folded."""
    sel = re.sub(COMMENT_RE, " ", pre.rstrip("{").rstrip())
    return [s.strip() for s in sel.split(",") if s.strip()]


def patch_css(css: str, *, add_block: bool = True) -> Tuple[str, List[str]]:
    """Rewrite project-owned illustration rules; append the unified block once.

    Idempotent: the appended block starts with ``UNIFIED_MARKER``, so a second
    run removes it first and re-appends exactly the same text.
    """
    notes: List[str] = []
    stripped = css
    if UNIFIED_MARKER in stripped:
        stripped = stripped[: stripped.index(UNIFIED_MARKER)].rstrip() + "\n"
    masked = _mask_comments(stripped)

    def rewrite(match: re.Match) -> str:
        pre = match.group("pre")
        # Keep any at-rule (e.g. @charset) that shares the match with the rule.
        head, sep, tail = pre.rpartition(";")
        if not sep or "@" not in head:
            head, sep, tail = "", "", pre
        selectors = _selectors(tail)
        sel = " ".join(" ".join(selectors).split())
        body = match.group("body")
        legacy = "82%" in body or "65vh" in body
        mentions_epub_class = bool(EPUB_CLASS_RE.search(sel))
        # A legacy project block that also sizes unclassified figure images must
        # survive (it is the only cap those images have): rewrite, never drop.
        plain_generic = (
            bool(GENERIC_CONTAINER_RE.search(sel))
            and any(not EPUB_CLASS_RE.search(s) for s in selectors)
        )
        if WIDTH_N_SEL_RE.search(sel):
            notes.append("dropped duplicate width-N rule")
            return head + sep
        if plain_generic:
            if not legacy:
                return match.group(0)
            notes.append("generic figure block -> 85%")
        elif mentions_epub_class:
            notes.append("baseline epub-image rule -> unified block")
            return head + sep
        elif sel == "img" and legacy:
            notes.append("global img cap 82%/65vh -> 85%/96vh")
        else:
            return match.group(0)
        if legacy:  # keep the explanatory comment in sync with the rule
            tail = tail.replace("82%", "85%").replace("65vh", "96vh")
        return head + sep + tail + body.replace("82%", "85%").replace("65vh", "96vh") + "}"

    patched = _unmask(RULE_RE.sub(rewrite, masked))
    if add_block:
        patched = patched.rstrip("\n") + "\n\n" + load_unified_block() + "\n"
    return patched, notes


def css_needs_patch(css: str) -> bool:
    """True while any project-owned legacy illustration cap is still present."""
    if UNIFIED_MARKER not in css:
        return True
    for match in RULE_RE.finditer(_mask_comments(css)):
        selectors = _selectors(match.group("pre"))
        sel = " ".join(" ".join(selectors).split())
        body = match.group("body")
        if WIDTH_N_SEL_RE.search(sel):
            continue
        if not ("82%" in body or "65vh" in body):
            continue
        plain_generic = (
            bool(GENERIC_CONTAINER_RE.search(sel))
            and any(not EPUB_CLASS_RE.search(s) for s in selectors)
        )
        if plain_generic or sel == "img":
            return True
    return False


# ============ HTML ============
def _size_from_zip(zf: zipfile.ZipFile, names: set, html_name: str, src: str,
                   cache: Dict[str, Optional[Tuple[int, int]]]):
    target = posixpath.normpath(
        posixpath.join(posixpath.dirname(html_name), T.unquote(src.split("#", 1)[0]))
    )
    if target not in cache:
        candidates = [target] if target in names else [
            n for n in names if posixpath.basename(n) == posixpath.basename(target)
        ]
        size = None
        for candidate in candidates[:1]:
            try:
                size = T._sniff_image_size(zf.read(candidate)[:4096])
            except (KeyError, OSError):
                size = None
        cache[target] = size
    return cache[target]


def _caption_alt(alt: str) -> bool:
    """Caption-like alt for the stale-`formula-img` check.

    Looser than ``translate_epub.alt_describes_illustration``: these captions
    often quote code or maths excerpts (``Bank_account_message``, ``x<0,1``,
    ``struct {...}``), so only an actual LaTeX command disqualifies them.
    """
    value = " ".join((alt or "").split())
    if len(value) < 60 or len(value.split()) < 8:
        return False
    if re.search(r"\\[A-Za-z]+", value):
        return False
    letters = sum(1 for char in value if char.isalpha())
    return letters / len(value) >= 0.5


def _formula_markup(image) -> bool:
    """Unambiguous formula markup: MathML/SVG ancestry or an explicit math class.

    The alt-text heuristics are deliberately excluded here: a long caption that
    happens to contain '=' or '_' is prose, and the stale ``formula-img`` class
    on such an image is exactly what needs correcting.
    """
    if image.find_parent(["math", "svg"]) is not None:
        return True
    return bool({"math-img", "math-formula"} & set(T._classes_of(image)))


def _with_classes(tag: str, classes: List[str]) -> str:
    class_match = CLASS_ATTR.search(tag)
    if not class_match:
        return tag.replace("<img ", '<img class="%s" ' % " ".join(classes), 1)
    return tag[: class_match.start()] + 'class="%s"' % " ".join(classes) + tag[class_match.end():]


def patch_html(text: str, resolve_size) -> Tuple[str, int, int, int, int]:
    """Re-class images using the shared classifier.

    Returns (new_text, upgraded, demoted, honour_authored_size, formula_fixed).
    """
    soup = T.BeautifulSoup(text, "html.parser")
    images = soup.find_all("img")
    tags = IMG_TAG.findall(text)
    if len(images) != len(tags):
        raise RuntimeError("img tag count mismatch between parser and regex; aborting")

    known = (T.ILLUSTRATION_CLASS, T.GLYPH_CLASS, T.FIXED_SIZE_CLASS)
    state = {"i": -1, "up": 0, "down": 0, "fixed": 0, "formula": 0}

    def repl(match: re.Match) -> str:
        state["i"] += 1
        tag = match.group(0)
        image = images[state["i"]]
        classes = T._classes_of(image)
        size = resolve_size(image.get("src", "") or "")
        if "formula-img" in classes and not _formula_markup(image) \
                and _caption_alt(image.get("alt", "") or "") \
                and T._is_big_image(size):
            # Stale `formula-img` on a captioned flow chart: those images escape
            # the 85% illustration ceiling, so re-class them as illustrations.
            new_classes = [c for c in classes if c != "formula-img"]
            if T.ILLUSTRATION_CLASS not in new_classes:
                new_classes.append(T.ILLUSTRATION_CLASS)
            state["formula"] += 1
            return _with_classes(tag, new_classes)
        if T._is_formula_image(image):
            return tag
        wanted = T.classify_image(image, size)
        in_code = T._ancestor_matches(image, T._is_code_container, levels=5)
        if wanted == T.ILLUSTRATION_CLASS and T.GLYPH_CLASS in classes:
            new_classes = [c for c in classes if c != T.GLYPH_CLASS]
            if T.ILLUSTRATION_CLASS not in new_classes:
                new_classes.append(T.ILLUSTRATION_CLASS)
            state["up"] += 1
        elif (wanted == T.GLYPH_CLASS and in_code
              and T.ILLUSTRATION_CLASS in classes and T.GLYPH_CLASS not in classes):
            new_classes = [c for c in classes if c != T.ILLUSTRATION_CLASS] + [T.GLYPH_CLASS]
            state["down"] += 1
        elif not any(cls in classes for cls in known) and wanted == T.FIXED_SIZE_CLASS:
            # Unclassed image whose publisher authored an explicit size: keep it
            # instead of letting the paragraph glyph cap crush it.
            new_classes = classes + [wanted]
            state["fixed"] += 1
        else:
            return tag
        return _with_classes(tag, new_classes)

    return IMG_TAG.sub(repl, text), state["up"], state["down"], state["fixed"], state["formula"]


def _clamped_illustrations(html_files: Dict[str, str]) -> int:
    """Count images still clamped small although they sit in a figure container."""
    count = 0
    for text in html_files.values():
        soup = T.BeautifulSoup(text, "html.parser")
        for image in soup.find_all("img"):
            if T.GLYPH_CLASS not in T._classes_of(image):
                continue
            if T._ancestor_matches(image, T._is_figure_container):
                count += 1
    return count


# ============ ZIP ============
def repack(path: Path, entries: List[Tuple[zipfile.ZipInfo, bytes]]) -> None:
    """Rewrite the archive, preserving entry order and ZipInfo metadata.

    ``compresslevel`` must be passed to ``writestr`` as well: a ``ZipInfo`` read
    from an existing archive carries its own level, so the ``ZipFile``
    constructor's level is ignored and untouched entries would be re-deflated at
    the default level (~5% larger on already-compressed PNGs).
    """
    tmp = path.with_name(path.name + ".tmp")
    mimetype = next((payload for info, payload in entries if info.filename == "mimetype"), None)
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as out:
        if mimetype is not None:
            out.writestr(zipfile.ZipInfo("mimetype"), mimetype, zipfile.ZIP_STORED)
        for info, payload in entries:
            if info.filename == "mimetype":
                continue
            out.writestr(info, payload, compresslevel=9)
    os.replace(tmp, path)


def recompress(path: Path) -> dict:
    """Repack at deflate level 9 without touching any payload (undo size creep)."""
    with zipfile.ZipFile(path) as zin:
        infos = zin.infolist()
        entries = [(info, zin.read(info.filename)) for info in infos]
    size_before = path.stat().st_size
    originals = {info.filename: payload for info, payload in entries}
    repack(path, entries)
    with zipfile.ZipFile(path) as zout:
        after = {i.filename: zout.read(i.filename) for i in zout.infolist()}
        checks = {
            "mimetype_first": zout.namelist()[0] == "mimetype",
            "mimetype_stored": zout.getinfo("mimetype").compress_type == zipfile.ZIP_STORED,
            "zip_intact": zout.testzip() is None,
            "entry_order": zout.namelist() == [i.filename for i in infos],
            "payloads_unchanged": after == originals,
        }
    return {"book": path.name, "size_before": size_before, "size_after": path.stat().st_size,
            "checks": checks}


def backup(path: Path, backup_dir: Path) -> str:
    """Copy the pristine file aside; never overwrite a different earlier backup."""
    backup_dir.mkdir(parents=True, exist_ok=True)
    target = backup_dir / path.name
    payload = path.read_bytes()
    if target.exists():
        if target.read_bytes() == payload:
            return "reused"
        digest = hashlib.sha1(str(path.resolve()).encode("utf-8")).hexdigest()[:8]
        target = backup_dir / ("%s__%s%s" % (path.stem, digest, path.suffix))
        if target.exists():
            if target.read_bytes() == payload:
                return "reused"
            raise SystemExit(
                "[ABORT] %s already exists with different content; refusing to overwrite a "
                "real pre-fix backup. Use a fresh --backup-dir." % target
            )
    target.write_bytes(payload)
    return "created"


def process(path: Path, backup_dir: Optional[Path], dry_run: bool) -> dict:
    report = {"book": path.name, "path": str(path), "size_before": path.stat().st_size,
              "upgraded": 0, "demoted": 0, "authored_size": 0, "formula_fixed": 0,
              "css_files": [], "css_notes": [], "checks": {}}
    patched: Dict[str, bytes] = {}

    with zipfile.ZipFile(path) as zin:
        infos = zin.infolist()
        original = {info.filename: zin.read(info.filename) for info in infos}
        names = set(original)
        size_cache: Dict[str, Optional[Tuple[int, int]]] = {}
        html_before: Dict[str, str] = {}
        html_after: Dict[str, str] = {}

        def resolve(src: str, html_name: str):
            return _size_from_zip(zin, names, html_name, src, size_cache)

        base_css = next((n for n in original if n.lower().endswith("translated.css")), None)
        if base_css is None:
            raise SystemExit("[ABORT] %s has no translated.css baseline" % path.name)

        for info in infos:
            name = info.filename
            payload = original[name]
            low = name.lower()
            if low.endswith((".xhtml", ".html", ".htm")):
                text = payload.decode("utf-8")
                html_before[name] = text
                html_after[name] = text
                if "<img" in text:
                    new_text, up, down, fixed, formula = patch_html(
                        text, lambda src, n=name: resolve(src, n))
                    if new_text != text:
                        payload = new_text.encode("utf-8")
                        html_after[name] = new_text
                        patched[name] = payload
                        report["upgraded"] += up
                        report["demoted"] += down
                        report["authored_size"] += fixed
                        report["formula_fixed"] += formula
            elif low.endswith(".css"):
                css = payload.decode("utf-8")
                new_css, notes = patch_css(css, add_block=(name == base_css))
                if new_css != css:
                    payload = new_css.encode("utf-8")
                    patched[name] = payload
                    report["css_files"].append(name)
                    report["css_notes"].extend(notes)

    report["changed_entries"] = sorted(patched)
    report["clamped_before"] = _clamped_illustrations(html_before)
    report["clamped_after"] = _clamped_illustrations(html_after)
    if dry_run:
        report["checks"] = {"dry_run": True}
        return report

    if backup_dir is not None:
        report["backup"] = backup(path, backup_dir)

    ordered = [(info, patched.get(info.filename, original[info.filename])) for info in infos]
    repack(path, ordered)

    with zipfile.ZipFile(path) as zout:
        after = {info.filename: zout.read(info.filename) for info in zout.infolist()}
        report["checks"] = {
            "mimetype_first": zout.namelist()[0] == "mimetype",
            "mimetype_stored": zout.getinfo("mimetype").compress_type == zipfile.ZIP_STORED,
            "mimetype_payload": zout.read("mimetype") == b"application/epub+zip",
            "zip_intact": zout.testzip() is None,
            "entries_unchanged": set(after) == names,
            "payloads_unchanged_except_patched": all(
                after[n] == patched.get(n, original[n]) for n in names
            ),
            "baseline_has_85_percent": "max-width: 85% !important" in after[base_css].decode("utf-8"),
            "baseline_has_authored_size_rule": "img.epub-fixed-image" in after[base_css].decode("utf-8"),
            "baseline_has_no_legacy_cap": not css_needs_patch(after[base_css].decode("utf-8")),
            "no_clamped_illustration_left": report["clamped_after"] == 0,
        }
    report["size_after"] = path.stat().st_size
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("epubs", nargs="+", type=Path)
    parser.add_argument("--backup-dir", type=Path, default=None)
    parser.add_argument("--report", type=Path, default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-backup", action="store_true",
                        help="skip the pre-fix copy; only for re-runs whose originals are "
                             "already preserved in an earlier backup directory")
    parser.add_argument("--recompress-only", action="store_true",
                        help="repack at deflate level 9 without changing any payload")
    args = parser.parse_args()
    if args.no_backup:
        args.backup_dir = None

    results = []
    if args.recompress_only:
        for epub in args.epubs:
            result = recompress(epub)
            results.append(result)
            print("[RC] %-44s %.2fMB -> %.2fMB  %s"
                  % (result["book"], result["size_before"] / 1e6,
                     result["size_after"] / 1e6, "OK" if all(result["checks"].values()) else result["checks"]))
        if args.report:
            args.report.write_text(json.dumps(results, ensure_ascii=False, indent=1),
                                   encoding="utf-8")
            print("[OK] report ->", args.report)
        return 0 if all(all(r["checks"].values()) for r in results) else 1

    for epub in args.epubs:
        result = process(epub, args.backup_dir, args.dry_run)
        results.append(result)
        print("[%s] %-44s upgraded=%-3d demoted=%-3d authored-size=%-3d formula-fixed=%-3d clamped %d->%d css=%s"
              % ("dry" if args.dry_run else "OK", result["book"],
                 result["upgraded"], result["demoted"], result["authored_size"],
                 result["formula_fixed"], result["clamped_before"], result["clamped_after"],
                 ",".join(result["css_files"]) or "none"))
    if args.report:
        args.report.write_text(json.dumps(results, ensure_ascii=False, indent=1),
                               encoding="utf-8")
        print("[OK] report ->", args.report)
    failures = [r for r in results
                if not r["checks"].get("dry_run", False)
                and not all(r["checks"].values())]
    if failures:
        print("[FAIL] check failures:", [r["book"] for r in failures], file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
