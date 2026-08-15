"""Book ingestion: txt / epub / (optional) mobi -> Book(title, author, chapters)."""
from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Chapter:
    title: str
    text: str

    @property
    def words(self) -> int:
        return len(self.text.split())


@dataclass
class Book:
    title: str
    author: str
    chapters: list[Chapter]
    source_path: str = ""
    source_sha256: str = ""
    language: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def words(self) -> int:
        return sum(c.words for c in self.chapters)

    def slice_chapters(self, spec: str) -> "Book":
        """Return a copy containing only chapters in a spec like '1-3', '2', '4-'."""
        spec = (spec or "").strip()
        if not spec:
            return self
        m = re.fullmatch(r"(\d+)?\s*(?:-\s*(\d+)?)?", spec)
        if not m:
            raise ValueError(f"bad chapter spec: {spec!r} (use e.g. 1-3)")
        lo = int(m.group(1)) if m.group(1) else 1
        if "-" in spec:
            hi = int(m.group(2)) if m.group(2) else len(self.chapters)
        else:
            hi = lo
        lo = max(1, lo)
        hi = min(len(self.chapters), hi)
        if lo > hi:
            raise ValueError(f"chapter range {spec!r} selects nothing (book has {len(self.chapters)} chapters)")
        return Book(self.title, self.author, self.chapters[lo - 1:hi], self.source_path,
                    self.source_sha256, self.language, dict(self.extra, chapter_slice=spec))


class UnsupportedFormat(Exception):
    pass


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_book(path: str, *, filename: Optional[str] = None) -> Book:
    """Load a book from disk. `filename` overrides the extension sniff (for uploads)."""
    name = (filename or path).lower()
    ext = os.path.splitext(name)[1]
    if ext in (".txt", ".text", ".md", ""):
        from .txt import load_txt
        book = load_txt(path)
    elif ext == ".epub":
        from .epub import load_epub
        book = load_epub(path)
    elif ext in (".mobi", ".azw", ".azw3", ".prc", ".kf8"):
        from .mobi import load_mobi
        book = load_mobi(path)
    elif ext in (".html", ".htm", ".xhtml"):
        from .epub import load_html_file
        book = load_html_file(path)
    else:
        raise UnsupportedFormat(f"unsupported file type {ext!r}; use .txt, .epub or .mobi (DRM-free)")
    book.source_path = path
    book.source_sha256 = sha256_file(path)
    if not book.title:
        book.title = os.path.splitext(os.path.basename(filename or path))[0]
    book.chapters = [c for c in book.chapters if c.text.strip()]
    if not book.chapters:
        raise UnsupportedFormat("no readable text found in this file (is it DRM-protected or image-only?)")
    return book
