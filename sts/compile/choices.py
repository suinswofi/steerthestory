"""Pass 2: decide where choice points go and design the options at each."""
from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass, field
from typing import Any

from ..config import CompileConfig
from ..llm import ChatClient, LLMBadJSON
from ..prompts import choice_prompt
from ..segment import Scene


@dataclass
class AltPlan:
    index: int              # alternative number (0-based)
    outcome: str            # "rejoin" | "ending"
    rejoin_index: int = -1  # canon scene index the branch rejoins (for rejoin)


@dataclass
class ChoicePlan:
    scene_index: int
    alts: list[AltPlan] = field(default_factory=list)

    @property
    def id(self) -> str:
        return f"{self.scene_index + 1:03d}"


def _coin(seed: int, *parts: int) -> float:
    h = hashlib.sha256(("|".join(str(p) for p in (seed,) + parts)).encode()).digest()
    return int.from_bytes(h[:4], "big") / 2**32


def plan_choice_points(n_scenes: int, cfg: CompileConfig) -> list[ChoicePlan]:
    plans: list[ChoicePlan] = []
    every = max(1, cfg.choice_every)
    for i in range(n_scenes - 1):  # never on the last scene
        if (i + 1) % every != 0:
            continue
        rejoin = i + 1 + max(1, cfg.rejoin_after)
        alts: list[AltPlan] = []
        for j in range(max(1, cfg.branches)):
            if rejoin > n_scenes - 1:
                alts.append(AltPlan(j, "ending"))
            elif _coin(cfg.seed, i, j) < cfg.ending_ratio:
                alts.append(AltPlan(j, "ending"))
            else:
                alts.append(AltPlan(j, "rejoin", rejoin))
        plans.append(ChoicePlan(i, alts))
    # Guarantee at least one alternate ending when the user asked for endings at all
    if plans and cfg.ending_ratio > 0 and not any(a.outcome == "ending" for p in plans for a in p.alts):
        p = plans[len(plans) // 2]
        p.alts[-1] = AltPlan(p.alts[-1].index, "ending")
    return plans


def design_choice(client: ChatClient, bible: dict[str, Any], running_before: str, scene: Scene,
                  next_summary: str, n_alts: int) -> dict[str, Any]:
    """Ask the model for the question, canon label and alternative premises. Raises LLMBadJSON."""
    data = client.chat_json(choice_prompt(bible, running_before, scene.text, next_summary, n_alts),
                            max_tokens=800, required=("question", "canon_label", "alternatives"))
    alts_raw = data.get("alternatives") or []
    alts: list[dict[str, str]] = []
    for a in alts_raw:
        if isinstance(a, dict) and a.get("label"):
            alts.append({"label": _clean_label(str(a["label"])), "premise": str(a.get("premise") or a["label"]).strip()})
        elif isinstance(a, str) and a.strip():
            alts.append({"label": _clean_label(a), "premise": a.strip()})
    if not alts:
        raise LLMBadJSON("no usable alternatives")
    return {
        "question": str(data.get("question") or f"What does {bible.get('protagonist', 'the protagonist')} do?").strip(),
        "canon_label": _clean_label(str(data.get("canon_label") or "Continue as in the book")),
        "alternatives": alts[:n_alts],
    }


def _clean_label(label: str) -> str:
    label = " ".join(label.split()).strip().strip("\"'“”.")
    if len(label) > 90:
        label = label[:87].rstrip() + "…"
    return label[:1].upper() + label[1:] if label else "Continue"


def shuffled_option_order(seed: int, scene_index: int, n: int) -> list[int]:
    """Deterministic order for options (index 0 = canon) so canon isn't always first."""
    order = list(range(n))
    random.Random(f"{seed}:{scene_index}").shuffle(order)
    return order
