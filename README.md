# Steer The Story (STS)

Turn any DRM‑free book into a **choose‑your‑own‑adventure** you can read in your browser — using
*your own* language model (anything with an OpenAI‑compatible API: Ollama, LM Studio, llama.cpp,
vLLM, OpenAI, OpenRouter, …).

Drop in a `.txt` / `.epub` (or `.mobi`), point STS at your model, and it compiles the book **once**
into a single self‑contained `.sts` file. Playing needs no model and no original book: open the
file in the built‑in reader, read the author's own prose, and at every fork decide what the
protagonist does. Take the book's path, or a detour that STS wrote in the author's voice — some
detours wind their way back to the plot, others end the story somewhere new.

- **No dependencies.** Pure Python 3.10+ standard library; the web UI is one HTML page.
- **Bring your own model.** Any OpenAI‑compatible chat endpoint. Designed so a ~7–8B model in 8 GB
  of VRAM works: no prompt is ever larger than ~4–5k tokens.
- **Compile once, read anywhere.** The `.sts` file is plain JSON. The reader even works when
  `index.html` is opened straight from disk.
- **Resumable.** Long compiles checkpoint after every model call; Ctrl‑C or a crash loses nothing.

## Quick start

```bash
git clone https://github.com/suinswofi/steerthestory
cd steerthestory
python3 -m sts serve          # opens http://127.0.0.1:8000
```

(Optional: `pip install .` gives you an `sts` command; `pip install .[mobi]` adds `.mobi`/`.azw3` support.)

1. **Make an adventure** tab → drop a book. STS shows the chapters it found and an estimate of the
   work (scenes, choice points, number of model calls, largest prompt).
2. Enter your endpoint URL and model name (see below), press **Test connection**.
3. Press **Make the adventure**. Watch progress; stop and restart any time — it resumes.
4. **Read it now** — or download the `.sts` and open it in the **Read** tab on any machine
   (`python3 -m sts play book.sts` serves the reader only).

Want to see the result before running a model? `examples/alice-in-wonderland.sts` is the whole of
*Alice's Adventures in Wonderland* compiled with default settings (7 forks, 7 endings, ~16k
generated words next to Carroll's 26k) — drop it on the **Read** tab, or
`python3 -m sts play examples/alice-in-wonderland.sts`.

Same thing from the command line:

```bash
python3 -m sts probe   --base-url http://localhost:11434/v1 --model qwen2.5:7b
python3 -m sts compile alice.epub --base-url http://localhost:11434/v1 --model qwen2.5:7b -o alice.sts
python3 -m sts info alice.sts -v      # stats, choice points, endings, graph validation
python3 -m sts play alice.sts
```

Endpoint settings can also come from the environment: `STS_BASE_URL` (or `OPENAI_BASE_URL`),
`STS_MODEL`, `STS_API_KEY` (or `OPENAI_API_KEY`).

### Pointing STS at your model

| Server | Endpoint URL | Model name |
|---|---|---|
| Ollama | `http://localhost:11434/v1` | e.g. `qwen2.5:7b`, `llama3.1:8b` |
| LM Studio | `http://localhost:1234/v1` | the id shown in LM Studio |
| llama.cpp `llama-server` | `http://localhost:8080/v1` | anything (server ignores it) |
| vLLM / TGI / others | `http://host:port/v1` | served model id |
| OpenAI | `https://api.openai.com/v1` + API key | e.g. `gpt-4o-mini` |
| OpenRouter | `https://openrouter.ai/api/v1` + API key | e.g. `anthropic/claude-sonnet-4` |

For a local 8 GB card, an 7–8B instruct model at Q4 with an **8k context** is enough
(`python3 -m sts probe --measure-context` estimates the usable context). Larger/better models give
noticeably better detours; the pipeline is the same.

## How it works (and why it works with small models)

A novel is 100–200k tokens; small models see 8k. So nothing in STS ever looks at the whole book.

```
book ──ingest──▶ chapters ──segment──▶ scenes (~1.8k tokens each)
                                          │
        pass 1  ┌──────────────────────────┘  one call per scene, sequential
                ▼
   style guide + rolling "story bible" (protagonist, POV, characters, setting,
   running summary ≤ 180 words) + a 2‑3 sentence summary per scene
                │
        pass 2  ▼  one call per choice point (every N scenes), parallel
   question, the "canon" option (what the book does next) and 2 alternatives with premises
                │
        pass 3  ▼  one call per generated scene, parallel per detour
   each detour = 2‑4 new scenes in the author's voice that either REJOIN the book a few
   scenes later (told the summary of what it must be consistent with and how the next
   original scene opens) or END the story
                │
                ▼
   .sts  = original scenes verbatim + generated detours, as a graph
```

Every prompt is `style guide + bible + running summary + one scene (+ target)`, i.e. 3–5k tokens.
Structural answers are requested as JSON (with tolerant parsing and one repair round‑trip); prose
is requested as plain text with a trailing `SUMMARY:` line, which small models handle far better
than JSON‑escaped prose.

**Branch‑and‑merge, not a tree.** A pure tree explodes exponentially and lets a small model drift
into nonsense. STS keeps the book as the spine; detours are short and are told where they must
land, so the whole adventure is roughly 2–4× the book and stays coherent. A configurable share of
detours end the story instead (alternate endings), so choices carry real weight.

**Cost.** For a 100k‑word novel with defaults: ~145 scenes, ~48 choice points, ~96 detours →
~480 model calls, ~200k output tokens. Minutes on a hosted API with `--concurrency 8`; a few
hours on a local 8B model. Use `--dry-run` (or the estimate in the UI) before you start, and
`--chapters 1-3` for a quick taste.

## Design decisions

- **The author's prose is kept verbatim.** The original scenes are not rewritten or condensed;
  only the detours are generated, and they are written in the book's own POV/tense/voice
  (the choice question is phrased in‑world: *"What does Alice do?"*). This is cheaper, keeps the
  best prose, and asks the least of small models. Full second‑person rewrites are a possible later
  option, not the default.
- **Precomputed, offline reading.** Everything the reader needs is in the `.sts`; no model at
  play time. The format has room for a future "live mode" (nodes may carry `generated_at_play`).
- **Standard library only.** `urllib` talks to the API, `zipfile` + `html.parser` read EPUB,
  `http.server` serves the UI, vanilla JS renders it. `mobi` is the one optional dependency
  (`pip install mobi`; or convert with Calibre).
- **Checkpoint everything.** `<output>.sts.partial.json` is rewritten atomically after each model
  result; a compile with the same book and the same shape settings resumes automatically.
- **Deterministic structure, creative content.** Where choice points fall, which detours are
  endings, and the order of options are all derived from a seed; only the text comes from the model.

## The `.sts` file

Plain JSON (`.sts.gz` also accepted):

```jsonc
{
  "format": "sts/1",
  "meta":  { "title", "author", "source_sha256", "model", "config", "usage", ... },
  "style_guide": "Point of view & tense: ...",
  "bible": { "protagonist": "Alice", "characters": [...], "setting": "...", "themes": [...] },
  "start": "c001",
  "nodes": {
    "c001":     { "kind": "canon",  "text": "...", "summary": "...", "chapter": 1, "chapter_title": "CHAPTER I.",
                  "choices": [ { "label": "Continue", "to": "c002", "canon": true } ] },
    "c003":     { "kind": "canon",  "text": "...", "question": "What does Alice do?",
                  "choices": [ { "label": "Try to speak politely to the Mouse", "to": "c004", "canon": true },
                               { "label": "Swim past the Mouse toward the far shore", "to": "b003-1-1" } ] },
    "b003-1-1": { "kind": "branch", "text": "...", "branch_id": "b003-1", "choices": [ { "label": "Continue", "to": "b003-1-2" } ] },
    "b003-1-3": { "kind": "branch", "text": "...", "branch_id": "b003-1", "choices": [ { "label": "Continue", "to": "c007" } ] },
    "b006-2-3": { "kind": "ending", "text": "...", "ending_title": "Alice Walks Away From the Quarrel" }
  }
}
```

`kind` is `canon` (original text), `branch` (generated) or `ending`. Node ids: `cNNN` for the
book's scenes, `bNNN-k-s` for scene *s* of detour *k* leaving scene *NNN*.

## Shaping the adventure

| Setting | CLI flag | Default | Meaning |
|---|---|---|---|
| Choice every N scenes | `--choice-every` | 3 | how often the reader gets a fork |
| Alternatives | `--branches` | 2 | detours per fork (plus the book's own path) |
| Scenes per detour | `--branch-len` | 3 | how long a detour is |
| Rejoin after | `--rejoin-after` | 3 | detours rejoin the book this many scenes later |
| Ending ratio | `--ending-ratio` | 0.25 | share of detours that end the story instead |
| Words per new scene | `--branch-scene-words` | 350 | length of generated scenes |
| Scene size | `--scene-tokens` | 1800 | size of an original scene (lower for 4k contexts) |
| Chapters | `--chapters 1-3` | all | compile a slice, for trials |
| Concurrency | `--concurrency` | 1 | parallel model calls in passes 2–3 |

## Testing without a GPU

- `python3 -m unittest` — 19 tests (ingest, segmentation, graph validation, full compile with a
  deterministic fake model, cancel/resume, web server flow).
- `python3 -m sts compile book.txt --fake` (or endpoint URL `fake://` in the UI) exercises the
  whole pipeline with the fake model in under a second.
- If you use Claude Code, `python3 -m sts shim --port 8765` starts a tiny OpenAI‑compatible server
  that answers via the `claude` CLI, so `--base-url http://localhost:8765/v1` lets you try STS with
  a strong model before you have a local one running.

- `STS_LLM_LOG=llm.jsonl python3 -m sts compile …` records every prompt and reply (JSON lines) —
  handy when a small model misbehaves.

Public‑domain books from [Project Gutenberg](https://www.gutenberg.org/) are perfect test material.

## Limitations & notes

- Detour quality is bounded by your model. A 7B model will be recognisably weaker than the
  author; the reader marks new passages by default so you always know which is which.
- Chapter detection is heuristic (works well on Gutenberg‑style texts and most EPUBs); check the
  chapter list the UI shows, and use `--chapters` if front matter sneaks in.
- Rejoins are "soft": the detour is asked to land in a state consistent with the skipped original
  scenes, and small continuity wrinkles can remain — part of the charm of gamebooks.
- Only DRM‑free files are supported. What you compile and share is your responsibility.

## Layout

```
sts/ingest/     txt / epub / mobi -> Book(chapters)
sts/segment.py  chapters -> scenes
sts/compile/    bible.py (pass 1), choices.py (pass 2), branches.py (pass 3), checkpoint.py
sts/prompts.py  every prompt template
sts/llm.py      OpenAI-compatible client (urllib), JSON parsing, probe
sts/adventure.py .sts schema, validation, load/save
sts/server.py   web app backend      sts/web/  index.html, app.js, style.css
sts/testing/    fake_llm.py, claude_shim.py     tests/  unittest suite
```

MIT licensed.
