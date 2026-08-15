""".sts adventure file: schema, validation, load/save."""
from __future__ import annotations

import gzip
import json
import os
from dataclasses import asdict, dataclass, field
from typing import Any, Iterator, Optional

FORMAT = "sts/1"


@dataclass
class Choice:
    label: str
    to: str
    canon: bool = False

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"label": self.label, "to": self.to}
        if self.canon:
            d["canon"] = True
        return d


@dataclass
class Node:
    id: str
    kind: str                     # canon | branch | ending
    text: str
    summary: str = ""
    choices: list[Choice] = field(default_factory=list)
    chapter: Optional[int] = None
    chapter_title: str = ""
    question: str = ""            # prompt shown above the choices, e.g. "What does Alice do?"
    ending_title: str = ""
    branch_id: str = ""           # which divergent arc this belongs to
    generated_at_play: bool = False

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"kind": self.kind, "text": self.text}
        if self.summary:
            d["summary"] = self.summary
        if self.choices:
            d["choices"] = [c.to_dict() for c in self.choices]
        for k in ("chapter", "chapter_title", "question", "ending_title", "branch_id"):
            v = getattr(self, k)
            if v not in (None, "", 0):
                d[k] = v
        if self.generated_at_play:
            d["generated_at_play"] = True
        return d

    @classmethod
    def from_dict(cls, nid: str, d: dict[str, Any]) -> "Node":
        return cls(
            id=nid,
            kind=d.get("kind", "canon"),
            text=d.get("text", ""),
            summary=d.get("summary", ""),
            choices=[Choice(c.get("label", "Continue"), c["to"], bool(c.get("canon"))) for c in d.get("choices", [])],
            chapter=d.get("chapter"),
            chapter_title=d.get("chapter_title", ""),
            question=d.get("question", ""),
            ending_title=d.get("ending_title", ""),
            branch_id=d.get("branch_id", ""),
            generated_at_play=bool(d.get("generated_at_play")),
        )


@dataclass
class Adventure:
    meta: dict[str, Any]
    style_guide: str
    bible: dict[str, Any]
    start: str
    nodes: dict[str, Node] = field(default_factory=dict)

    # ---- serialization ----------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "format": FORMAT,
            "meta": self.meta,
            "style_guide": self.style_guide,
            "bible": self.bible,
            "start": self.start,
            "nodes": {nid: n.to_dict() for nid, n in self.nodes.items()},
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Adventure":
        if d.get("format") != FORMAT:
            raise ValueError(f"not an STS adventure file (format={d.get('format')!r})")
        return cls(
            meta=d.get("meta", {}),
            style_guide=d.get("style_guide", ""),
            bible=d.get("bible", {}),
            start=d.get("start", ""),
            nodes={nid: Node.from_dict(nid, nd) for nid, nd in d.get("nodes", {}).items()},
        )

    def save(self, path: str) -> None:
        data = json.dumps(self.to_dict(), ensure_ascii=False, indent=1).encode("utf-8")
        tmp = path + ".tmp"
        if path.endswith(".gz"):
            with gzip.open(tmp, "wb") as f:
                f.write(data)
        else:
            with open(tmp, "wb") as f:
                f.write(data)
        os.replace(tmp, path)

    @classmethod
    def load(cls, path: str) -> "Adventure":
        with open(path, "rb") as f:
            raw = f.read()
        if raw[:2] == b"\x1f\x8b":
            raw = gzip.decompress(raw)
        return cls.from_dict(json.loads(raw.decode("utf-8")))

    # ---- graph helpers ----------------------------------------------------------
    def reachable(self) -> set[str]:
        seen: set[str] = set()
        stack = [self.start]
        while stack:
            nid = stack.pop()
            if nid in seen or nid not in self.nodes:
                continue
            seen.add(nid)
            stack.extend(c.to for c in self.nodes[nid].choices)
        return seen

    def endings(self) -> list[Node]:
        return [n for n in self.nodes.values() if n.kind == "ending" or not n.choices]

    def validate(self) -> list[str]:
        """Return a list of problems (empty == valid)."""
        problems: list[str] = []
        if not self.nodes:
            return ["no nodes"]
        if self.start not in self.nodes:
            problems.append(f"start node {self.start!r} missing")
        for nid, n in self.nodes.items():
            if n.kind not in ("canon", "branch", "ending"):
                problems.append(f"{nid}: bad kind {n.kind!r}")
            if not n.text.strip():
                problems.append(f"{nid}: empty text")
            for c in n.choices:
                if c.to not in self.nodes:
                    problems.append(f"{nid}: choice -> missing node {c.to!r}")
            if n.kind == "ending" and n.choices:
                problems.append(f"{nid}: ending has choices")
            if n.kind != "ending" and not n.choices:
                problems.append(f"{nid}: dead end (non-ending node without choices)")
        unreachable = set(self.nodes) - self.reachable()
        if unreachable:
            problems.append(f"{len(unreachable)} unreachable node(s): {sorted(unreachable)[:5]}")
        return problems

    def stats(self) -> dict[str, Any]:
        kinds: dict[str, int] = {}
        for n in self.nodes.values():
            kinds[n.kind] = kinds.get(n.kind, 0) + 1
        choice_points = sum(1 for n in self.nodes.values() if len(n.choices) > 1)
        words = sum(len(n.text.split()) for n in self.nodes.values())
        canon_words = sum(len(n.text.split()) for n in self.nodes.values() if not n.branch_id and not n.generated_at_play)
        return {
            "nodes": len(self.nodes),
            "kinds": kinds,
            "choice_points": choice_points,
            "endings": len(self.endings()),
            "words": words,
            "canon_words": canon_words,
            "generated_words": words - canon_words,
        }

    def walk_canon(self) -> Iterator[Node]:
        nid = self.start
        seen = set()
        while nid in self.nodes and nid not in seen:
            seen.add(nid)
            n = self.nodes[nid]
            yield n
            nxt = [c for c in n.choices if c.canon] or n.choices[:1]
            if not nxt:
                return
            nid = nxt[0].to
