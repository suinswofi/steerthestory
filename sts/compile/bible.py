"""Pass 1: rolling story bible, per-scene summaries and the style guide."""
from __future__ import annotations

import copy
import re
from typing import Any, Callable

from ..llm import ChatClient, LLMBadJSON, LLMError, estimate_tokens
from ..prompts import compact_prompt, scene_prompt, setup_prompt, shorten_summary_prompt
from ..segment import Scene
from .checkpoint import Checkpoint

BIBLE_TOKEN_BUDGET = 700
SUMMARY_WORD_BUDGET = 220


def _clip_words(text: str, n: int) -> str:
    words = text.split()
    return text if len(words) <= n else " ".join(words[:n]) + "…"


def pick_sample_paragraphs(scenes: list[Scene], k: int = 2) -> list[str]:
    """Pick k medium-length paragraphs (with some dialogue if possible) from the opening scenes."""
    paras: list[str] = []
    for sc in scenes[:4]:
        for p in re.split(r"\n\s*\n", sc.text):
            p = " ".join(p.split())
            w = len(p.split())
            if 50 <= w <= 140:
                paras.append(p)
    if not paras:
        for sc in scenes[:2]:
            paras.extend(" ".join(p.split()) for p in re.split(r"\n\s*\n", sc.text) if len(p.split()) >= 20)
    with_dialogue = [p for p in paras if any(q in p for q in "\"“”'")]
    without = [p for p in paras if p not in with_dialogue]
    out: list[str] = []
    for src in (without, with_dialogue):
        if src:
            out.append(src[len(src) // 2])
    while len(out) < k and paras:
        cand = paras[len(out) % len(paras)]
        if cand not in out:
            out.append(cand)
        else:
            break
    return out[:k]


def build_style_guide(setup: dict[str, Any], samples: list[str]) -> str:
    notes = setup.get("style_notes")
    if isinstance(notes, list):
        notes = "; ".join(str(n) for n in notes)
    lines = [f"Point of view & tense: {setup.get('pov', 'unknown')}",
             f"Voice: {notes or 'match the samples'}"]
    if samples:
        lines.append("Sample passages from the book (imitate this voice):")
        for s in samples:
            lines.append(f"> {s}")
    return "\n".join(lines)


def run_setup(client: ChatClient, scenes: list[Scene], title: str, author: str) -> dict[str, Any]:
    opening = scenes[0].text
    if len(scenes) > 1 and estimate_tokens(opening) < 1200:
        opening = opening + "\n\n" + scenes[1].text
    opening = _clip_words(opening, 1500)
    data = client.chat_json(setup_prompt(title, author, opening), max_tokens=700,
                            required=("protagonist", "pov"))
    setup = {
        "protagonist": str(data.get("protagonist") or "the protagonist"),
        "pov": str(data.get("pov") or ""),
        "style_notes": data.get("style_notes") or "",
        "setting": str(data.get("setting") or ""),
        "themes": [str(t) for t in (data.get("themes") or [])][:6],
        "characters": _norm_chars(data.get("characters") or []),
    }
    setup["samples"] = pick_sample_paragraphs(scenes)
    setup["style_guide"] = build_style_guide(setup, setup["samples"])
    return setup


def _norm_chars(chars: Any) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    if isinstance(chars, dict):
        chars = [{"name": k, "role": v} for k, v in chars.items()]
    if not isinstance(chars, list):
        return out
    for c in chars:
        if isinstance(c, dict) and c.get("name"):
            out.append({"name": str(c["name"])[:60], "role": str(c.get("role") or c.get("description") or "")[:160]})
        elif isinstance(c, str) and c.strip():
            name, _, role = c.partition(":")
            out.append({"name": name.strip()[:60], "role": role.strip()[:160]})
    return out


def merge_characters(existing: list[dict[str, str]], new: list[dict[str, str]]) -> list[dict[str, str]]:
    by_name = {c["name"].lower(): c for c in existing}
    for c in new:
        key = c["name"].lower()
        if key in by_name:
            if c["role"] and c["role"] != by_name[key]["role"]:
                by_name[key]["role"] = c["role"]
        else:
            # avoid near-duplicates like "Mr. Darcy" vs "Darcy"
            dup = next((k for k in by_name if k in key or key in k), None)
            if dup and len(key) > 3:
                continue
            existing.append(c)
            by_name[key] = c
    return existing


def initial_bible(setup: dict[str, Any]) -> dict[str, Any]:
    return {
        "protagonist": setup["protagonist"],
        "setting": setup.get("setting", ""),
        "themes": setup.get("themes", []),
        "characters": copy.deepcopy(setup.get("characters", [])),
    }


def run_bible_pass(client: ChatClient, scenes: list[Scene], setup: dict[str, Any], cp: Checkpoint,
                   *, progress: Callable[[int, int, str], None], log: Callable[[str], None],
                   should_stop: Callable[[], bool]) -> None:
    """Sequentially analyse scenes; results stored in cp.state['scenes'][str(index)]."""
    done = cp.state["scenes"]
    # rebuild rolling state from the last completed scene
    bible = initial_bible(setup)
    running = ""
    start = 0
    for i in range(len(scenes)):
        rec = done.get(str(i))
        if not rec:
            break
        bible = rec["bible"]
        running = rec["running_summary"]
        start = i + 1
    for i in range(start, len(scenes)):
        if should_stop():
            return
        sc = scenes[i]
        progress(i, len(scenes), f"analysing scene {i + 1}/{len(scenes)} (chapter {sc.chapter})")
        try:
            data = client.chat_json(scene_prompt(bible, running, sc.text, i + 1, len(scenes)),
                                    max_tokens=700, required=("scene_summary", "running_summary"))
        except LLMBadJSON as e:
            log(f"scene {i + 1}: model failed to produce JSON twice ({e}); using fallback summary")
            data = {"scene_summary": _clip_words(sc.text, 60), "running_summary": running}
        scene_summary = str(data.get("scene_summary") or "").strip() or _clip_words(sc.text, 60)
        new_running = str(data.get("running_summary") or "").strip() or (running + " " + scene_summary)
        if len(new_running.split()) > SUMMARY_WORD_BUDGET:
            try:
                fixed = client.chat_json(shorten_summary_prompt(new_running), max_tokens=400,
                                         required=("running_summary",))
                new_running = str(fixed.get("running_summary") or new_running)
            except (LLMError, LLMBadJSON):
                pass
            new_running = _clip_words(new_running, SUMMARY_WORD_BUDGET + 40)
        bible = copy.deepcopy(bible)
        bible["characters"] = merge_characters(bible.get("characters", []), _norm_chars(data.get("new_characters") or []))
        if data.get("setting") and isinstance(data["setting"], str) and len(data["setting"]) > 8:
            bible["setting"] = data["setting"][:300]
        if estimate_tokens(str(bible.get("characters"))) > BIBLE_TOKEN_BUDGET:
            try:
                fixed = client.chat_json(compact_prompt(bible), max_tokens=700, required=("characters",))
                chars = _norm_chars(fixed.get("characters") or [])
                if chars:
                    bible["characters"] = chars[:12]
            except (LLMError, LLMBadJSON):
                bible["characters"] = bible["characters"][:12]
        running = new_running
        done[str(i)] = {"summary": scene_summary, "running_summary": running, "bible": bible}
        cp.state["usage"] = client.usage.as_dict()
        cp.save()
    progress(len(scenes), len(scenes), "scene analysis complete")
