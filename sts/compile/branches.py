"""Pass 3: generate the divergent arcs (branch scenes) for one alternative."""
from __future__ import annotations

from typing import Any, Callable

from ..config import CompileConfig
from ..llm import ChatClient, LLMError
from ..prompts import branch_scene_prompt, parse_scene_reply
from ..segment import Scene
from .choices import AltPlan, ChoicePlan


def _tail_words(text: str, n: int) -> str:
    words = text.split()
    return text if len(words) <= n else "…" + " ".join(words[-n:])


def _head_words(text: str, n: int) -> str:
    words = text.split()
    return " ".join(words[:n])


def branch_id(plan: ChoicePlan, alt: AltPlan) -> str:
    return f"b{plan.id}-{alt.index + 1}"


def generate_branch(client: ChatClient, cfg: CompileConfig, style_guide: str, scenes: list[Scene],
                    scene_records: dict[str, Any], plan: ChoicePlan, alt: AltPlan, premise: str,
                    existing: list[dict[str, Any]], *, on_scene: Callable[[dict[str, Any]], None],
                    should_stop: Callable[[], bool], log: Callable[[str], None]) -> list[dict[str, Any]]:
    """Generate the remaining scenes of one branch. `existing` = scenes already generated (resume).
    Returns list of node dicts: {"id","kind","text","summary","choices","ending_title","branch_id"}."""
    i = plan.scene_index
    rec = scene_records[str(i)]
    bible = rec["bible"]
    running = rec["running_summary"]
    tail = _tail_words(scenes[i].text, 220)
    bid = branch_id(plan, alt)
    total = max(1, cfg.branch_len)
    ending = alt.outcome == "ending"
    rejoin_summary = rejoin_opening = ""
    if not ending:
        between = [scene_records[str(k)]["summary"] for k in range(i + 1, alt.rejoin_index)
                   if str(k) in scene_records]
        rejoin_summary = " ".join(between) or scene_records[str(alt.rejoin_index)]["summary"]
        rejoin_opening = _head_words(scenes[alt.rejoin_index].text, 45)
    nodes = list(existing)
    for step in range(len(nodes) + 1, total + 1):
        if should_stop():
            break
        prev = nodes[-1]["text"] if nodes else ""
        msgs = branch_scene_prompt(style_guide, bible, running, tail, premise, prev, step, total,
                                   cfg.branch_scene_words, rejoin_summary=rejoin_summary,
                                   rejoin_opening=rejoin_opening, ending=ending)
        text = summary = title = ""
        for attempt in range(2):
            reply = client.chat(msgs, max_tokens=int(cfg.branch_scene_words * 2.2) + 200)
            text, summary, title = parse_scene_reply(reply)
            if len(text.split()) >= 60:
                break
            log(f"{bid} scene {step}: reply too short ({len(text.split())} words), retrying")
        if len(text.split()) < 20:
            raise LLMError(f"{bid} scene {step}: model returned no usable prose")
        node = {
            "id": f"{bid}-{step}",
            "kind": "branch",
            "text": text,
            "summary": summary or _head_words(text, 40),
            "choices": [],
            "ending_title": "",
            "branch_id": bid,
        }
        if step < total:
            node["choices"] = [{"label": "Continue", "to": f"{bid}-{step + 1}"}]
        elif ending:
            node["kind"] = "ending"
            node["ending_title"] = title or "An alternate ending"
        else:
            node["choices"] = [{"label": "Continue", "to": scenes[alt.rejoin_index].id}]
        nodes.append(node)
        on_scene(node)
    return nodes
