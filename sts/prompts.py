"""Prompt templates. Kept short and concrete so ~7B models can follow them; every prompt is
built from bounded pieces (style guide, bible, running summary, one scene) so it fits ~8k context."""
from __future__ import annotations

import json
from typing import Any

SYSTEM_ANALYST = (
    "You are a meticulous literary analyst helping build an interactive edition of a novel. "
    "You answer ONLY with a single valid JSON object, no prose before or after it."
)

SYSTEM_AUTHOR = (
    "You are ghost-writing new scenes for an interactive edition of a novel, imitating the original "
    "author's voice so closely that readers cannot tell the difference. Match the point of view, "
    "tense, vocabulary, sentence rhythm, dialogue style and tone described in the STYLE GUIDE. "
    "Never break the fourth wall, never mention choices, games or the reader. Write only story prose."
)


def bible_block(bible: dict[str, Any]) -> str:
    chars = bible.get("characters") or []
    lines = []
    if bible.get("protagonist"):
        lines.append(f"Protagonist: {bible['protagonist']}")
    if bible.get("setting"):
        lines.append(f"Setting: {bible['setting']}")
    if bible.get("themes"):
        lines.append("Themes: " + ", ".join(str(t) for t in bible["themes"][:8]))
    if chars:
        lines.append("Characters:")
        for c in chars[:16]:
            if isinstance(c, dict):
                name = c.get("name", "?")
                desc = c.get("role") or c.get("description") or ""
                lines.append(f"- {name}: {desc}"[:200])
            else:
                lines.append(f"- {c}"[:200])
    return "\n".join(lines) if lines else "(nothing known yet)"


# ---------------------------------------------------------------------------------------------
# Pass 1a: setup (protagonist, POV, style) from the first scenes
# ---------------------------------------------------------------------------------------------
def setup_prompt(title: str, author: str, opening_text: str) -> list[dict[str, str]]:
    user = f"""Book: "{title}" by {author or 'unknown author'}.
Below is the opening of the book. Identify how it is written so we can imitate it later.

Return JSON with exactly these keys:
- "protagonist": the main character's name (or "the narrator" if unnamed)
- "pov": point of view and tense, e.g. "third person limited, past tense" or "first person, present tense"
- "style_notes": 3-5 short bullet-like phrases describing voice, diction, sentence rhythm, dialogue conventions, tone
- "setting": one sentence on time and place
- "themes": list of 2-5 short theme words/phrases
- "characters": list of objects {{"name": ..., "role": ...}} for characters introduced so far (max 8)

OPENING:
\"\"\"
{opening_text}
\"\"\""""
    return [{"role": "system", "content": SYSTEM_ANALYST}, {"role": "user", "content": user}]


# ---------------------------------------------------------------------------------------------
# Pass 1b: per-scene summary + bible update (rolling)
# ---------------------------------------------------------------------------------------------
def scene_prompt(bible: dict[str, Any], running_summary: str, scene_text: str, scene_no: int,
                 total: int) -> list[dict[str, str]]:
    user = f"""We are reading a novel scene by scene. This is scene {scene_no} of {total}.

WHAT WE KNOW SO FAR:
{bible_block(bible)}

STORY SO FAR (running summary):
{running_summary or '(this is the beginning)'}

NEW SCENE:
\"\"\"
{scene_text}
\"\"\"

Return JSON with exactly these keys:
- "scene_summary": 2-3 sentences summarizing what happens in THIS scene (concrete events, names).
- "new_characters": list of {{"name": ..., "role": ...}} for characters who are NEW in this scene or whose role changed (empty list if none, max 5).
- "setting": one sentence if the setting changed notably, else "".
- "running_summary": an updated summary of the WHOLE story so far including this scene, at most 180 words, plain prose, most recent events last."""
    return [{"role": "system", "content": SYSTEM_ANALYST}, {"role": "user", "content": user}]


def compact_prompt(bible: dict[str, Any]) -> list[dict[str, str]]:
    user = f"""This character list for a novel has grown too long. Merge duplicates, drop minor walk-on characters, and keep at most 12 entries, each role at most 15 words.

{json.dumps(bible.get('characters', []), ensure_ascii=False)}

Return JSON: {{"characters": [{{"name": ..., "role": ...}}, ...]}}"""
    return [{"role": "system", "content": SYSTEM_ANALYST}, {"role": "user", "content": user}]


def shorten_summary_prompt(summary: str) -> list[dict[str, str]]:
    user = f"""Shorten this story summary to at most 150 words, keeping names and the most recent events:

{summary}

Return JSON: {{"running_summary": "..."}}"""
    return [{"role": "system", "content": SYSTEM_ANALYST}, {"role": "user", "content": user}]


# ---------------------------------------------------------------------------------------------
# Pass 2: choice points
# ---------------------------------------------------------------------------------------------
def choice_prompt(bible: dict[str, Any], running_summary_before: str, scene_text: str,
                  next_scene_summary: str, n_alternatives: int) -> list[dict[str, str]]:
    prot = bible.get("protagonist") or "the protagonist"
    user = f"""We are turning a novel into an interactive story where the reader steers the viewpoint character
(usually {prot}; if the current scene follows someone else — a different narrator, letter-writer or
point-of-view character — the choice is about THAT character instead).

WHAT WE KNOW:
{bible_block(bible)}

STORY SO FAR:
{running_summary_before or '(beginning)'}

CURRENT SCENE (the reader has just finished reading this):
\"\"\"
{scene_text}
\"\"\"

WHAT ACTUALLY HAPPENS NEXT IN THE BOOK:
{next_scene_summary}

At the end of the current scene, the viewpoint character faces a decision. Design a choice with {n_alternatives + 1} options:
option 1 leads to what actually happens next in the book; the other {n_alternatives} are tempting, in-character
alternatives that would send the story in a different direction. Alternatives must be plausible for
this character and setting (no anachronisms, no magic unless the book has it). Labels must be short
imperative-style phrases about the character's next action, all in the same grammatical form, and
must not reveal what happens afterwards.

Return JSON with exactly these keys:
- "question": one short question shown to the reader naming the character, e.g. "What does {prot} do?"
- "canon_label": label for the option that follows the book (max 12 words)
- "alternatives": list of {n_alternatives} objects {{"label": <max 12 words>, "premise": <1-2 sentences describing what happens if the character does this>}}"""
    return [{"role": "system", "content": SYSTEM_ANALYST}, {"role": "user", "content": user}]


# ---------------------------------------------------------------------------------------------
# Pass 3: branch scenes (plain text output; more robust than JSON for long prose)
# ---------------------------------------------------------------------------------------------
def branch_scene_prompt(style_guide: str, bible: dict[str, Any], running_summary: str,
                        divergence_tail: str, premise: str, prev_scene: str, step: int,
                        total_steps: int, target_words: int, *, rejoin_summary: str = "",
                        rejoin_opening: str = "", ending: bool = False) -> list[dict[str, str]]:
    prot = bible.get("protagonist") or "the protagonist"
    parts = [f"STYLE GUIDE:\n{style_guide}", f"WHAT WE KNOW:\n{bible_block(bible)}",
             f"STORY SO FAR:\n{running_summary}"]
    if step == 1:
        parts.append(f"THE LAST SCENE ENDED LIKE THIS:\n\"\"\"\n{divergence_tail}\n\"\"\"")
        parts.append(f"WHAT HAPPENS INSTEAD (the reader chose this): {premise}")
    else:
        parts.append(f"THE READER'S DIVERGENCE FROM THE ORIGINAL PLOT: {premise}")
        parts.append(f"PREVIOUS SCENE OF THIS NEW STORYLINE:\n\"\"\"\n{prev_scene}\n\"\"\"")
    task = [f"Write scene {step} of {total_steps} of this new storyline, about {target_words} words."]
    if step == 1:
        task.append("Begin right where the last scene ended and show the viewpoint character of that scene "
                    f"(usually {prot}) taking the chosen course of action.")
    if step == total_steps and ending:
        task.append("This is the FINAL scene: bring the story to a definite, satisfying or tragic conclusion "
                    "in keeping with the book's tone. Do not leave the plot open.")
    elif step == total_steps and rejoin_summary:
        task.append("This is the LAST scene of the detour, so it must steer events back so that the story can "
                    "rejoin the original plot. By the end of this scene the situation must be consistent with "
                    "the following, which the reader will not see but must have effectively happened:\n"
                    f"\"\"\"\n{rejoin_summary}\n\"\"\"")
        if rejoin_opening:
            task.append(f"The very next scene the reader will read begins: \"{rejoin_opening}...\" — end your scene "
                        "so that this follows naturally.")
    else:
        task.append("Advance the storyline meaningfully; end at a natural pause, not a cliffhanger question to the reader.")
    task.append("Write ONLY the prose of the scene (dialogue and narration in the book's style). "
                "Then, on the very last line, write `SUMMARY:` followed by a 1-2 sentence summary of the scene."
                + (" Also add a final line `TITLE:` with a short title for this ending (max 6 words)." if (step == total_steps and ending) else ""))
    parts.append("\n".join(task))
    return [{"role": "system", "content": SYSTEM_AUTHOR}, {"role": "user", "content": "\n\n".join(parts)}]


def parse_scene_reply(reply: str) -> tuple[str, str, str]:
    """Split a branch-scene reply into (text, summary, title)."""
    text = reply.strip()
    summary = ""
    title = ""
    lines = text.split("\n")
    # scan the last few lines for SUMMARY:/TITLE: markers
    body_end = len(lines)
    for i in range(len(lines) - 1, max(-1, len(lines) - 8), -1):
        s = lines[i].strip().lstrip("*#` ").rstrip("*` ")
        low = s.lower()
        if low.startswith("summary:"):
            summary = s[len("summary:"):].strip()
            body_end = min(body_end, i)
        elif low.startswith("title:"):
            title = s[len("title:"):].strip().strip("\"'*")
            body_end = min(body_end, i)
    body = "\n".join(lines[:body_end]).strip()
    # strip markdown headings / code fences the model may add
    body = body.strip("`").strip()
    if body.lower().startswith("scene "):
        first, _, rest = body.partition("\n")
        if len(first) < 40:
            body = rest.strip()
    return body, summary, title
