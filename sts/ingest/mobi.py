"""MOBI/AZW3 ingestion via the optional `mobi` package (pip install mobi)."""
from __future__ import annotations

import os
import shutil
import tempfile

from . import Book, UnsupportedFormat


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
            book = load_epub(out)
        elif ext in (".html", ".htm", ".xhtml"):
            from .epub import load_html_file
            book = load_html_file(out)
        elif ext in (".txt", ".text"):
            from .txt import load_txt
            book = load_txt(out)
        else:
            raise UnsupportedFormat(f"mobi extraction produced unexpected file {out!r} (DRM-protected?)")
        # mobi lib leaves everything as one HTML file most of the time; keep whatever chapters we found
        return book
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
