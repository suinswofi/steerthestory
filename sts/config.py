"""Compile configuration: endpoint settings + adventure-shaping knobs."""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass, field, fields
from typing import Any, Optional


def _env(*names: str, default: Optional[str] = None) -> Optional[str]:
    for n in names:
        v = os.environ.get(n)
        if v:
            return v
    return default


@dataclass
class LLMConfig:
    base_url: str = "http://localhost:11434/v1"
    model: str = "qwen2.5:7b"
    api_key: str = ""
    timeout: float = 300.0
    max_retries: int = 4
    temperature: float = 0.8
    # Rough budget for a single prompt (input) in tokens; used for guards/warnings.
    context_tokens: int = 8192

    @classmethod
    def from_env(cls) -> "LLMConfig":
        return cls(
            base_url=_env("STS_BASE_URL", "OPENAI_BASE_URL", default=cls.base_url) or cls.base_url,
            model=_env("STS_MODEL", "OPENAI_MODEL", default=cls.model) or cls.model,
            api_key=_env("STS_API_KEY", "OPENAI_API_KEY", default="") or "",
        )


@dataclass
class CompileConfig:
    """Knobs that shape the adventure. Defaults tuned for ~7-8B local models."""

    llm: LLMConfig = field(default_factory=LLMConfig)
    # Segmentation
    scene_tokens: int = 1800          # target size of a canon scene
    max_scenes: int = 0               # 0 = no cap
    chapters: str = ""                # e.g. "1-3" to slice the book for trials
    # Choice structure
    choice_every: int = 3             # a choice point every N canon scenes
    branches: int = 2                 # divergent options per choice point
    branch_len: int = 3               # generated scenes per divergent arc
    rejoin_after: int = 3             # rejoin canon this many scenes after the choice
    ending_ratio: float = 0.25        # share of divergent arcs that end the story instead of rejoining
    branch_scene_words: int = 350     # target words per generated scene
    # Execution
    concurrency: int = 1
    resume: bool = True
    seed: int = 0

    def config_hash(self) -> str:
        """Hash of the adventure-shaping knobs (not endpoint/model), used to validate resume."""
        d = asdict(self)
        d.pop("llm", None)
        d.pop("concurrency", None)
        d.pop("resume", None)
        raw = json.dumps(d, sort_keys=True).encode()
        return hashlib.sha256(raw).hexdigest()[:16]

    def to_public_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["llm"].pop("api_key", None)
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CompileConfig":
        """Build a config from a loose dict (e.g. JSON from the web UI); unknown keys ignored."""
        data = dict(data or {})
        llm_data = data.pop("llm", {}) or {}
        cfg = cls(llm=LLMConfig.from_env())
        _apply(cfg.llm, llm_data)
        _apply(cfg, data)
        return cfg


def _apply(obj: Any, data: dict[str, Any]) -> None:
    for f in fields(obj):
        if f.name not in data or data[f.name] is None or data[f.name] == "":
            continue
        val = data[f.name]
        cur = getattr(obj, f.name)
        try:
            if isinstance(cur, bool):
                val = val if isinstance(val, bool) else str(val).lower() in ("1", "true", "yes", "on")
            elif isinstance(cur, int):
                val = int(val)
            elif isinstance(cur, float):
                val = float(val)
            else:
                val = str(val)
        except (TypeError, ValueError):
            continue
        setattr(obj, f.name, val)
