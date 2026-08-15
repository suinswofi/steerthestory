"""EPUB ingestion using zipfile + html.parser (no third-party deps)."""
from __future__ import annotations

import posixpath
import re
import zipfile
from html.parser import HTMLParser
from xml.etree import ElementTree as ET

from . import Book, Chapter, UnsupportedFormat

_BLOCK_TAGS = {"p", "div", "br", "h1", "h2", "h3", "h4", "h5", "h6", "li", "tr", "blockquote", "section",
               "article", "pre", "hr", "dd", "dt", "figure", "figcaption", "td", "th"}
_SKIP_TAGS = {"script", "style", "head", "title", "svg", "math", "nav"}


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0
        self.headings: list[str] = []
        self._in_heading = False
        self._heading_buf: list[str] = []
        self.body_seen = False

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in _SKIP_TAGS:
            self._skip += 1
        if tag in _BLOCK_TAGS:
            self.parts.append("\n\n" if tag not in ("br",) else "\n")
        if tag in ("h1", "h2", "h3") and not self._in_heading:
            self._in_heading = True
            self._heading_buf = []

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in _SKIP_TAGS and self._skip:
            self._skip -= 1
        if tag in _BLOCK_TAGS:
            self.parts.append("\n\n")
        if tag in ("h1", "h2", "h3") and self._in_heading:
            self._in_heading = False
            h = " ".join("".join(self._heading_buf).split())
            if h:
                self.headings.append(h)

    def handle_data(self, data):
        if self._skip:
            return
        self.parts.append(data)
        if self._in_heading:
            self._heading_buf.append(data)

    def text(self) -> str:
        raw = "".join(self.parts)
        raw = raw.replace(" ", " ")
        lines = [" ".join(l.split()) for l in raw.split("\n")]
        out = "\n".join(lines)
        out = re.sub(r"\n{3,}", "\n\n", out)
        return out.strip()


def html_to_text(html: str) -> tuple[str, list[str]]:
    # Drop XML declaration / doctype which HTMLParser handles fine but be safe
    p = _TextExtractor()
    p.feed(html)
    p.close()
    return p.text(), p.headings


def _find(el, tag: str):
    """Namespace-agnostic find."""
    for child in el.iter():
        if child.tag.rsplit("}", 1)[-1] == tag:
            return child
    return None


def _findall(el, tag: str):
    return [c for c in el.iter() if c.tag.rsplit("}", 1)[-1] == tag]


def load_epub(path: str) -> Book:
    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile as e:
        raise UnsupportedFormat("not a valid EPUB (zip) file") from e
    with zf:
        names = set(zf.namelist())
        if "META-INF/encryption.xml" in names:
            enc = zf.read("META-INF/encryption.xml").decode("utf-8", "replace")
            if "EncryptedData" in enc and "font" not in enc.lower():
                raise UnsupportedFormat("this EPUB appears to be DRM-protected")
        try:
            container = ET.fromstring(zf.read("META-INF/container.xml"))
        except (KeyError, ET.ParseError) as e:
            raise UnsupportedFormat("EPUB missing META-INF/container.xml") from e
        rootfile = _find(container, "rootfile")
        if rootfile is None:
            raise UnsupportedFormat("EPUB container has no rootfile")
        opf_path = rootfile.get("full-path")
        opf_dir = posixpath.dirname(opf_path)
        try:
            opf = ET.fromstring(zf.read(opf_path))
        except (KeyError, ET.ParseError) as e:
            raise UnsupportedFormat("EPUB has unreadable package (OPF) file") from e
        # metadata
        title = author = language = ""
        for el in opf.iter():
            t = el.tag.rsplit("}", 1)[-1]
            if t == "title" and not title:
                title = (el.text or "").strip()
            elif t == "creator" and not author:
                author = (el.text or "").strip()
            elif t == "language" and not language:
                language = (el.text or "").strip()
        manifest = {}
        for item in _findall(opf, "item"):
            manifest[item.get("id")] = (item.get("href"), item.get("media-type", ""), item.get("properties", ""))
        spine_ids = [ir.get("idref") for ir in _findall(opf, "itemref") if ir.get("linear", "yes") != "no"]
        chapters: list[Chapter] = []
        for idref in spine_ids:
            href, mtype, props = manifest.get(idref, (None, "", ""))
            if not href or "nav" in props:
                continue
            if mtype and "html" not in mtype and "xml" not in mtype:
                continue
            full = posixpath.normpath(posixpath.join(opf_dir, href)) if opf_dir else href
            full = full.split("#", 1)[0]
            try:
                raw = zf.read(full)
            except KeyError:
                try:
                    raw = zf.read(href)
                except KeyError:
                    continue
            html = raw.decode("utf-8", "replace")
            text, headings = html_to_text(html)
            if len(text.split()) < 20:
                continue
            ctitle = headings[0] if headings else ""
            chapters.append(Chapter(ctitle, text))
    from .txt import normalize, split_chapters, strip_gutenberg

    # Merge very short spine items (title pages, epigraphs) into following ones
    merged: list[Chapter] = []
    for ch in chapters:
        if merged and merged[-1].words < 120:
            merged[-1] = Chapter(merged[-1].title or ch.title, merged[-1].text + "\n\n" + ch.text)
        else:
            merged.append(ch)
    # Spine items often don't map to chapters (Gutenberg splits by size). Try heading heuristics
    # on the whole text and prefer them when they find at least as many chapters.
    joined, meta = strip_gutenberg("\n\n".join(c.text for c in merged))
    heuristic = split_chapters(normalize(joined))
    if len(heuristic) >= 2 and len(heuristic) * 1.5 >= len(merged):
        merged = heuristic
    else:  # keep spine items, but strip Gutenberg boilerplate from each
        stripped = []
        for c in merged:
            t, _ = strip_gutenberg(c.text)
            t = t.strip()
            if len(t.split()) >= 20 and not t.startswith("The Project Gutenberg eBook"):
                stripped.append(Chapter(c.title, normalize(t)))
        merged = stripped or merged
    return Book(title or meta.get("title", ""), author or meta.get("author", ""), merged, language=language)


def load_html_file(path: str) -> Book:
    from .txt import read_text, split_chapters
    text, headings = html_to_text(read_text(path))
    return Book(headings[0] if headings else "", "", split_chapters(text))
