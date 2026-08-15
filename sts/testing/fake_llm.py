"""Deterministic in-process stand-in for an LLM, used by the test-suite and `--fake` trials."""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from ..llm import Usage, _check_required, extract_json


@dataclass
class FakeLLM:
    usage: Usage = field(default_factory=Usage)
    delay: float = 0.0
    calls: list[list[dict[str, str]]] = field(default_factory=list)
    supports_json_mode: Optional[bool] = True

    def chat(self, messages: list[dict[str, str]], *, json_mode: bool = False,
             max_tokens: int = 1024, temperature: Optional[float] = None) -> str:
        self.calls.append(messages)
        if self.delay:
            time.sleep(self.delay)
        system = messages[0]["content"] if messages and messages[0]["role"] == "system" else ""
        user = messages[-1]["content"]
        self.usage.add(Usage(calls=1, prompt_tokens=len(user) // 4, completion_tokens=100))
        if '{"ok": true}' in user:
            return '{"ok": true}'
        if "ghost-writing" in system:
            return self._branch(user)
        if '"protagonist"' in user and '"pov"' in user:
            return json.dumps({
                "protagonist": "Alice", "pov": "third person limited, past tense",
                "style_notes": ["whimsical", "long playful sentences", "Victorian diction"],
                "setting": "A dreamlike Wonderland, mid-19th century England.",
                "themes": ["curiosity", "absurdity"],
                "characters": [{"name": "Alice", "role": "curious girl"}, {"name": "White Rabbit", "role": "hurried rabbit"}],
            })
        if '"scene_summary"' in user:
            m = re.search(r"This is scene (\d+) of (\d+)", user)
            n = m.group(1) if m else "?"
            return json.dumps({
                "scene_summary": f"In scene {n}, Alice encounters something curious and reacts with wonder.",
                "new_characters": [{"name": f"Creature {n}", "role": "odd inhabitant"}] if int(n or 0) % 4 == 0 else [],
                "setting": "",
                "running_summary": f"Alice has fallen into Wonderland and, through scene {n}, met a series of odd creatures.",
            })
        if "has grown too long" in user:
            return json.dumps({"characters": [{"name": "Alice", "role": "curious girl"}]})
        if "Shorten this story summary" in user:
            return json.dumps({"running_summary": "Alice wanders Wonderland meeting odd creatures."})
        if '"canon_label"' in user:
            m = re.search(r"list of (\d+) objects", user)
            k = int(m.group(1)) if m else 2
            return json.dumps({
                "question": "What does Alice do?",
                "canon_label": "Follow the White Rabbit",
                "alternatives": [{"label": f"Alternative {j + 1}", "premise": f"Alice instead tries alternative {j + 1}."}
                                 for j in range(k)],
            })
        return "OK"

    def _branch(self, user: str) -> str:
        m = re.search(r"Write scene (\d+) of (\d+)", user)
        step, total = (int(m.group(1)), int(m.group(2))) if m else (1, 1)
        ending = "FINAL scene" in user
        words = " ".join(["Alice wandered further into the strange garden, wondering what would happen next."] * 12)
        out = f"{words}\n\nSUMMARY: Alice explores in scene {step} of {total}."
        if ending:
            out += "\nTITLE: A Curious End"
        return out

    def chat_json(self, messages: list[dict[str, str]], *, max_tokens: int = 1024,
                  temperature: Optional[float] = None, required: tuple[str, ...] = ()) -> dict[str, Any]:
        obj = extract_json(self.chat(messages, json_mode=True, max_tokens=max_tokens, temperature=temperature))
        _check_required(obj, required)
        return obj

    def probe(self, *, measure_context: bool = False) -> dict[str, Any]:
        return {"ok": True, "model": "fake", "base_url": "fake://", "json_mode": True}
