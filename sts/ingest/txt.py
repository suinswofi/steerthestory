"""Plain-text ingestion with Gutenberg boilerplate stripping and chapter heuristics."""
from __future__ import annotations

import re

from . import Book, Chapter

_GUT_START = re.compile(r"^\*\*\*\s*START OF (?:THE|THIS) PROJECT GUTENBERG EBOOK.*?\*\*\*\s*$", re.I | re.M)
_GUT_END = re.compile(r"^\*\*\*\s*END OF (?:THE|THIS) PROJECT GUTENBERG EBOOK.*?\*\*\*\s*$", re.I | re.M)

# Chapter heading heuristics: "CHAPTER I", "Chapter 1.", "CHAPTER THE FIRST", "I.", "BOOK ONE", "PART II"...
_ROMAN = r"(?=[MDCLXVI])M{0,4}(?:CM|CD|D?C{0,3})(?:XC|XL|L?X{0,3})(?:IX|IV|V?I{0,3})"
_WORD_HEADING_RE = re.compile(
    r"^\s*(?:CHAPTER|Chapter|BOOK|Book|PART|Part|STAVE|Stave|LETTER|Letter|CANTO|Canto|SCENE|Scene|ACT|Act)"
    r"\s+(?:\d+|" + _ROMAN + r"|[A-Za-z-]+)\.?[^\n]{0,80}\s*$"
)
_ROMAN_HEADING_RE = re.compile(r"^\s*" + _ROMAN + r"(?:\.|:|\s*[—-]|\s+[A-Z][^\n]{0,80})?\.?\s*$")
_DIGIT_HEADING_RE = re.compile(r"^\s*\d{1,3}\.?(?:\s+[A-Z][^\n]{0,80})?\s*$")


def _heading_family(line: str) -> str:
    if _WORD_HEADING_RE.match(line):
        return "word"
    if _ROMAN_HEADING_RE.match(line):
        return "roman"
    if _DIGIT_HEADING_RE.match(line):
        return "digit"
    return ""


def read_text(path: str) -> str:
    raw = open(path, "rb").read()
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def strip_gutenberg(text: str) -> tuple[str, dict]:
    meta: dict = {}
    m = re.search(r"^Title:\s*(.+)$", text[:6000], re.M)
    if m:
        meta["title"] = m.group(1).strip()
    m = re.search(r"^Author:\s*(.+)$", text[:6000], re.M)
    if m:
        meta["author"] = m.group(1).strip()
    m = re.search(r"^Language:\s*(.+)$", text[:6000], re.M)
    if m:
        meta["language"] = m.group(1).strip()
    s = _GUT_START.search(text)
    if s:
        text = text[s.end():]
    e = _GUT_END.search(text)
    if e:
        text = text[:e.start()]
    return text, meta


def normalize(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\t", " ")
    text = re.sub(r"[  ]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip("\n") + "\n"


def _is_heading(lines: list[str], i: int) -> bool:
    """A heading is a short heading-like line preceded by a blank line and followed by a blank line,
    or followed by a single short subtitle line and then a blank line."""
    line = lines[i].strip()
    if not line or len(line) > 100 or not _heading_family(line):
        return False
    prev_blank = i == 0 or not lines[i - 1].strip()
    if not prev_blank:
        return False
    nxt = lines[i + 1].strip() if i + 1 < len(lines) else ""
    if not nxt:
        return True
    nxt2 = lines[i + 2].strip() if i + 2 < len(lines) else ""
    return len(nxt) <= 80 and not nxt2


def split_chapters(text: str) -> list[Chapter]:
    """Split on chapter-like headings. Falls back to a single chapter."""
    lines = text.split("\n")
    cands = [(i, _heading_family(lines[i].strip())) for i in range(len(lines)) if _is_heading(lines, i)]
    # Prefer the most explicit heading family present ("CHAPTER X" > "X." > "1"), to avoid
    # false positives like poem lines starting with "I" once real chapter headings exist.
    idxs: list[int] = []
    for fam in ("word", "roman", "digit"):
        fam_idxs = [i for i, f in cands if f == fam]
        if len(fam_idxs) >= 3:
            idxs = fam_idxs
            break
    if not idxs:
        idxs = [i for i, _ in cands]
    # Require a reasonable number of headings and reasonable spacing, else treat as one chapter
    if len(idxs) < 2:
        return [Chapter("", text)]
    chapters: list[Chapter] = []
    # Preamble before the first heading (dedication, contents) is dropped if short, else kept as chapter 0
    pre = "\n".join(lines[:idxs[0]]).strip()
    if len(pre.split()) > 400:
        chapters.append(Chapter("Front matter", pre))
    for n, start in enumerate(idxs):
        end = idxs[n + 1] if n + 1 < len(idxs) else len(lines)
        title = lines[start].strip()
        body_start = start + 1
        if body_start < end and lines[body_start].strip() and (body_start + 1 >= end or not lines[body_start + 1].strip()):
            title = title + " " + lines[body_start].strip()
            body_start += 1
        body = "\n".join(lines[body_start:end]).strip()
        # A table of contents produces many tiny "chapters": merge chapters shorter than ~40 words into next
        chapters.append(Chapter(title, body))
    # A table of contents yields headings that repeat later in the body: keep only the last
    # occurrence of each title. Anything substantial swallowed by a TOC entry becomes front matter.
    def _norm(t: str) -> str:
        return " ".join(re.sub(r"[^a-z0-9]+", " ", t.lower()).split()[:2])

    last_pos = {_norm(c.title): i for i, c in enumerate(chapters)}
    kept: list[Chapter] = []
    orphan_words = 0
    orphan_text: list[str] = []
    for i, ch in enumerate(chapters):
        if last_pos[_norm(ch.title)] == i:
            kept.append(ch)
        else:
            orphan_words += ch.words
            orphan_text.append(ch.text)
    if orphan_words > 400 and not (kept and kept[0].title == "Front matter"):
        kept.insert(0, Chapter("Front matter", "\n\n".join(orphan_text)))
    # Tiny "chapters" (< 40 words) are TOC entries or bare headings: drop them.
    merged: list[Chapter] = [ch for ch in kept if ch.words >= 40]
    if not merged:
        return [Chapter("", text)]
    # If chapters ended up absurdly many and tiny on average, the heuristic misfired -> single chapter
    avg = sum(c.words for c in merged) / len(merged)
    if avg < 150 and len(merged) > 20:
        return [Chapter("", text)]
    return merged


def load_txt(path: str) -> Book:
    text = read_text(path)
    text, meta = strip_gutenberg(text)
    text = normalize(text)
    chapters = split_chapters(text)
    return Book(meta.get("title", ""), meta.get("author", ""), chapters, language=meta.get("language", ""))
