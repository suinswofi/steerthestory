# CLAUDE.md — Steer The Story (STS)

Guidance for AI coding assistants working in this repo. The README is the user-facing source of
truth for behaviour and design; this file covers what you need to work on the code safely.

## What this is

A Python tool that compiles a DRM-free book (`.txt`/`.epub`/optional `.mobi`) into a
choose-your-own-adventure `.sts` file (plain JSON) using any OpenAI-compatible chat endpoint,
plus a small web app / reader (`python3 -m sts serve`). Reading needs no model.

## Hard constraints — do not break these

- **Standard library only.** No new runtime dependencies. `mobi` is the single *optional* extra.
  HTTP is `urllib`, EPUB is `zipfile` + `html.parser`, the server is `http.server`, the UI is one
  vanilla-JS HTML file (`sts/web/index.html` — markup, CSS and JS inline; keep it single-file).
- **Python 3.10+.** No 3.11+-only syntax/stdlib.
- **Small-model friendly.** No prompt may grow beyond ~4–5k tokens. Every prompt is
  `style guide + bible + running summary + one scene (+ target)`; never feed the whole book.
- **Author's prose stays verbatim.** Only detours are generated; canon nodes are never rewritten.
- **Checkpoint/resume must keep working.** `<out>.sts.partial.json` is rewritten atomically after
  every model result; the same book + same shape settings must resume. If you change what goes
  into the checkpoint or the shape config, make sure stale checkpoints are detected, not misread.
- **Deterministic structure.** Where forks fall, which detours end, and option order come from
  `--seed`; only text comes from the model. Keep it that way.
- **`.sts` format is `sts/1`.** Additive changes are fine; changing existing fields means bumping
  the format and keeping the reader/`info`/validation backwards-compatible.

## Layout

```
sts/cli.py           argparse entry (compile / probe / info / serve / play / shim); __main__.py calls it
sts/config.py        endpoint + shape settings, env var handling
sts/ingest/          txt.py / epub.py / mobi.py -> Book(chapters); Gutenberg header/footer stripping
sts/segment.py       chapters -> scenes (~scene-tokens each)
sts/compile/         bible.py (pass 1: style guide + rolling bible, sequential)
                     choices.py (pass 2: choice points, parallel)
                     branches.py (pass 3: detours, parallel)
                     checkpoint.py (atomic partial-file resume)
sts/prompts.py       every prompt template lives here — edit prompts here only
sts/llm.py           OpenAI-compatible client, tolerant JSON extraction/repair, probe
sts/adventure.py     .sts schema, validation, load/save (.sts and .sts.gz)
sts/server.py        web backend (ThreadingHTTPServer), upload/compile/progress/read endpoints
sts/web/index.html   the whole UI (Make / Read tabs), also works opened from disk
sts/testing/         fake_llm.py (deterministic model), claude_shim.py (OpenAI shim over `claude -p`)
tests/               unittest suite; tests/fixtures/mini.txt is the tiny sample book
examples/            compiled Alice (.sts) and Dracula (.sts.gz)
```

## Commands

```bash
python3 -m unittest                                   # full suite, ~1–2 s, no network/model needed
python3 -m sts compile tests/fixtures/mini.txt --fake -o /tmp/mini.sts   # whole pipeline, fake model
python3 -m sts compile book.txt --dry-run             # scene/choice counts, prompt sizes, no calls
python3 -m sts info out.sts -v                        # stats + graph validation
python3 -m sts serve                                  # web UI at http://127.0.0.1:8000
python3 -m sts play file.sts                          # reader only
```

Endpoint via flags or env: `STS_BASE_URL`/`OPENAI_BASE_URL`, `STS_MODEL`, `STS_API_KEY`/`OPENAI_API_KEY`.
Debug logging: `STS_LLM_LOG=llm.jsonl` records every prompt/reply; `STS_HTTP_LOG=1` turns on the web server's per-request log.
The UI accepts endpoint URL `fake://` for the fake model.

## Testing with a real model but no GPU

`python3 -m sts shim --port 8765 --model sonnet` starts an OpenAI-compatible server backed by the
`claude` CLI (`STS_SHIM_MODEL` sets the default). Then `--base-url http://localhost:8765/v1`.
Expect ~10–20 s per call. Use `--chapters 1-3` / `--max-scenes` for trials; full books take many
hundreds of calls (see README cost numbers). Public-domain Gutenberg texts are the test material.

## Conventions

- Add a test for behaviour changes; the fake model makes end-to-end tests cheap — prefer them for
  pipeline changes over mocking individual calls.
- Prompt changes: keep the `SUMMARY:` trailing-line convention for prose and JSON for structural
  answers; re-run a `--fake` compile plus at least a short real-model slice before committing.
- Keep the README tables (flags, layout, cost) in sync when you change flags or module layout.
- Do not commit anything under `library/` (uploads/compiled output) or `*.partial.json`.
- License is PolyForm Noncommercial 1.0.0 (`LICENSE.md`); don't add MIT/Apache headers.
