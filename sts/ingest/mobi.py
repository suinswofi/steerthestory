"""MOBI/AZW3 ingestion via the optional `mobi` package (pip install mobi).

`mobi.extract` unpacks to either an EPUB (KF8 books) or a directory with one big
`book.html` plus `toc.ncx` / `content.opf` (MOBI7). For the latter we split the HTML on
the NCX navPoint anchors, which gives clean chapter boundaries and titles, and read
title/author from the OPF. Non-story sections (title page, copyright, contents,
forewords...) are dropped, and recorded in `Book.extra["skipped_sections"]`.
"""
from __future__ import annotations

import os
import re
import shutil
from xml.etree import ElementTree as ET

from . import Book, Chapter, UnsupportedFormat

# Section titles that are not part of the story proper.
_NON_STORY_RE = re.compile(
    r"^\s*(?:e?-?(?:foreword|preface|introduction|afterword|epilogue\s+by)|title\s*page|cover|copyright|"
    r"(?:table\s+of\s+)?contents|dedication|epigraph|acknowledg(?:e)?ments?|about\s+the\s+author|"
    r"also\s+by|other\s+books|books\s+by|bibliography|notes?|glossary|colophon|praise\s+for)\b",
    re.I,
)


def _opf_meta(opf_path: str) -> dict:
    meta: dict = {}
    try:
        root = ET.parse(opf_path).getroot()
    except (OSError, ET.ParseError):
        return meta
    for el in root.iter():
        t = el.tag.rsplit("}", 1)[-1]
        if t in ("title", "creator", "language") and t not in meta and (el.text or "").strip():
            meta[t] = el.text.strip()
    return meta


def _ncx_points(ncx_path: str) -> list[tuple[str, str]]:
    """[(label, fragment)] in playOrder from a toc.ncx."""
    try:
        root = ET.parse(ncx_path).getroot()
    except (OSError, ET.ParseError):
        return []
    pts = []
    for np_ in root.iter():
        if np_.tag.rsplit("}", 1)[-1] != "navPoint":
            continue
        label = ""
        src = ""
        for el in np_.iter():
            t = el.tag.rsplit("}", 1)[-1]
            if t == "text" and not label:
                label = " ".join((el.text or "").split())
            elif t == "content" and not src:
                src = el.get("src", "")
        frag = src.split("#", 1)[1] if "#" in src else ""
        if frag:
            try:
                order = int(np_.get("playOrder", "0"))
            except ValueError:
                order = 0
            pts.append((order, label, frag))
    pts.sort(key=lambda p: p[0])
    return [(label, frag) for _, label, frag in pts]


def _split_html_by_toc(html: str, points: list[tuple[str, str]]) -> list[Chapter]:
    from .epub import html_to_text
    from .txt import _heading_family

    positions = []
    for label, frag in points:
        m = re.search(r"<a\b[^>]*\bid=[\"']" + re.escape(frag) + r"[\"']", html)
        if m:
            positions.append((m.start(), label))
    positions.sort()
    if len(positions) < 2:
        return []
    chapters: list[Chapter] = []
    for n, (start, label) in enumerate(positions):
        end = positions[n + 1][0] if n + 1 < len(positions) else len(html)
        text, headings = html_to_text(html[start:end])
        # A nav label like "Chapter 1" beats an in-page heading like "1"; otherwise use the heading.
        title = label or (headings[0] if headings else "")
        # Drop a leading bare heading line ("1", "IV", "Chapter 3") that duplicates the title.
        first, _, rest = text.partition("\n")
        f = first.strip()
        if f and (f.lower() == title.lower() or _heading_family(f)) and len(f) <= 60:
            text = rest.lstrip("\n")
        chapters.append(Chapter(title, text))
    return chapters


def _drop_non_story(chapters: list[Chapter]) -> tuple[list[Chapter], list[str]]:
    kept, skipped = [], []
    for ch in chapters:
        if ch.words < 20 or _NON_STORY_RE.match(ch.title or ""):
            skipped.append(ch.title or f"(untitled, {ch.words} words)")
        else:
            kept.append(ch)
    # Only drop by title if there is a real story left; never return nothing.
    if sum(c.words for c in kept) < 1000:
        return chapters, []
    return kept, skipped


def load_mobi(path: str) -> Book:
    try:
        import mobi  # type: ignore
    except ImportError as e:
        raise UnsupportedFormat(
            "MOBI support needs the optional 'mobi' package: pip install mobi  "
            "(or convert the file to EPUB with Calibre)"
        ) from e
    tmpdir, out = mobi.extract(path)
    try:
        ext = os.path.splitext(out)[1].lower()
        if ext == ".epub":
            from .epub import load_epub
            return load_epub(out)
        if ext in (".html", ".htm", ".xhtml"):
            from .epub import load_html_file
            from .txt import normalize, read_text

            outdir = os.path.dirname(out)
            meta = _opf_meta(os.path.join(outdir, "content.opf"))
            points = _ncx_points(os.path.join(outdir, "toc.ncx"))
            chapters = _split_html_by_toc(read_text(out), points) if points else []
            if len(chapters) >= 2:
                chapters = [Chapter(c.title, normalize(c.text)) for c in chapters]
                chapters, skipped = _drop_non_story(chapters)
                book = Book(meta.get("title", ""), meta.get("creator", ""), chapters,
                            language=meta.get("language", ""))
                if skipped:
                    book.extra["skipped_sections"] = skipped
                return book
            book = load_html_file(out)
            book.title = meta.get("title", "") or book.title
            book.author = meta.get("creator", "")
            book.language = meta.get("language", "")
            return book
        if ext in (".txt", ".text"):
            from .txt import load_txt
            return load_txt(out)
        raise UnsupportedFormat(f"mobi extraction produced unexpected file {out!r} (DRM-protected?)")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
