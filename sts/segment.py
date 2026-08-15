"""Split chapters into scenes of roughly `scene_tokens` tokens, never mid-paragraph."""
from __future__ import annotations

import re
from dataclasses import dataclass

from .ingest import Book
from .llm import estimate_tokens

_SCENE_BREAK_RE = re.compile(r"^\s*(?:\*\s*){3,}\s*$|^\s*(?:#|~|—|-|_){3,}\s*$|^\s*\*\s*$")


@dataclass
class Scene:
    index: int          # 0-based position in the canon spine
    chapter: int        # 1-based chapter number
    chapter_title: str
    text: str

    @property
    def id(self) -> str:
        return f"c{self.index + 1:03d}"

    @property
    def tokens(self) -> int:
        return estimate_tokens(self.text)


def _paragraphs(text: str) -> list[str]:
    paras = [p.strip() for p in re.split(r"\n\s*\n", text)]
    return [p for p in paras if p]


def segment_book(book: Book, *, scene_tokens: int = 1800, max_scenes: int = 0,
                 skip_front_matter: bool = True) -> list[Scene]:
    """Greedy paragraph packing. Chapter boundaries and explicit scene breaks always split.
    Very long paragraphs are split on sentence boundaries."""
    scenes: list[Scene] = []
    hard_cap = int(scene_tokens * 1.35)
    soft_min = int(scene_tokens * 0.6)
    for ci, ch in enumerate(book.chapters, start=1):
        if skip_front_matter and ch.title == "Front matter":
            continue
        # Pack paragraphs greedily. Explicit scene breaks are preferred split points (taken when
        # the current scene is at least ~half the target), size limits are the fallback.
        chapter_scenes: list[list[str]] = []
        cur: list[str] = []
        cur_tok = 0

        def flush() -> None:
            nonlocal cur, cur_tok
            if cur:
                chapter_scenes.append(cur)
            cur, cur_tok = [], 0

        for para in _paragraphs(ch.text):
            if _SCENE_BREAK_RE.match(para):
                if cur_tok >= soft_min * 0.75:
                    flush()
                continue
            ptok = estimate_tokens(para)
            pieces = _split_long_paragraph(para, scene_tokens) if ptok > hard_cap else [para]
            for piece in pieces:
                pt = estimate_tokens(piece)
                if cur and cur_tok + pt > scene_tokens and cur_tok >= soft_min:
                    flush()
                cur.append(piece)
                cur_tok += pt
        flush()
        # Merge a trailing tiny scene into the previous one (same chapter)
        if len(chapter_scenes) >= 2 and estimate_tokens("\n\n".join(chapter_scenes[-1])) < soft_min // 2:
            chapter_scenes[-2].extend(chapter_scenes.pop())
        for paras in chapter_scenes:
            scenes.append(Scene(len(scenes), ci, ch.title, "\n\n".join(paras)))
            if max_scenes and len(scenes) >= max_scenes:
                return scenes
    return scenes


_SENT_RE = re.compile(r"(?<=[.!?])\s+|(?<=[.!?][\"'”’)\]])\s+")


def _split_long_paragraph(para: str, target_tokens: int) -> list[str]:
    sents = _SENT_RE.split(para)
    out: list[str] = []
    cur: list[str] = []
    cur_tok = 0
    for s in sents:
        st = estimate_tokens(s)
        if cur and cur_tok + st > target_tokens:
            out.append(" ".join(cur))
            cur, cur_tok = [], 0
        cur.append(s)
        cur_tok += st
    if cur:
        out.append(" ".join(cur))
    return out
