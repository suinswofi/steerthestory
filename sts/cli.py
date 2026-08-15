"""Command-line interface: sts compile | serve | play | probe | info | shim."""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from typing import Optional

from . import __version__
from .config import CompileConfig, LLMConfig


def _add_llm_args(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("LLM endpoint (OpenAI-compatible)")
    g.add_argument("--base-url", help="e.g. http://localhost:11434/v1 (env STS_BASE_URL / OPENAI_BASE_URL)")
    g.add_argument("--model", help="model name as the server knows it (env STS_MODEL)")
    g.add_argument("--api-key", help="API key if the server needs one (env STS_API_KEY / OPENAI_API_KEY)")
    g.add_argument("--timeout", type=float, help="seconds to wait for one completion (default 300)")
    g.add_argument("--temperature", type=float, help="sampling temperature for generation (default 0.8)")


def _add_compile_args(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("adventure shape")
    g.add_argument("--choice-every", type=int, help="a choice point every N scenes (default 3)")
    g.add_argument("--branches", type=int, help="divergent options per choice point (default 2)")
    g.add_argument("--branch-len", type=int, help="generated scenes per divergent arc (default 3)")
    g.add_argument("--rejoin-after", type=int, help="rejoin the book this many scenes after the choice (default 3)")
    g.add_argument("--ending-ratio", type=float, help="share of arcs that end the story instead of rejoining (default 0.25)")
    g.add_argument("--branch-scene-words", type=int, help="target words per generated scene (default 350)")
    g.add_argument("--scene-tokens", type=int, help="target size of a canon scene in tokens (default 1800)")
    g.add_argument("--chapters", help="only use these chapters, e.g. 1-3 (for quick trials)")
    g.add_argument("--max-scenes", type=int, help="cap the number of canon scenes (for quick trials)")
    g.add_argument("--concurrency", type=int, help="parallel LLM calls for branch generation (default 1)")
    g.add_argument("--seed", type=int, help="seed for option order / ending placement (default 0)")
    g.add_argument("--no-resume", action="store_true", help="ignore an existing .partial.json checkpoint")


def build_config(args: argparse.Namespace) -> CompileConfig:
    cfg = CompileConfig(llm=LLMConfig.from_env())
    for name in ("base_url", "model", "api_key", "timeout", "temperature"):
        v = getattr(args, name, None)
        if v not in (None, ""):
            setattr(cfg.llm, name, v)
    for name in ("choice_every", "branches", "branch_len", "rejoin_after", "ending_ratio", "branch_scene_words",
                 "scene_tokens", "chapters", "max_scenes", "concurrency", "seed"):
        v = getattr(args, name, None)
        if v not in (None, ""):
            setattr(cfg, name, v)
    if getattr(args, "no_resume", False):
        cfg.resume = False
    return cfg


def make_client(cfg: CompileConfig, *, fake: bool = False, log=None):
    if fake:
        from .testing.fake_llm import FakeLLM
        return FakeLLM()
    from .llm import OpenAIClient
    return OpenAIClient(cfg.llm, log=log)


# ---------------------------------------------------------------------------------------------
def cmd_compile(args: argparse.Namespace) -> int:
    from .compile import CompileCancelled, compile_book, dry_run_report
    from .ingest import UnsupportedFormat, load_book

    cfg = build_config(args)
    try:
        book = load_book(args.book)
    except UnsupportedFormat as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    out = args.output or os.path.splitext(os.path.basename(args.book))[0] + ".sts"
    if args.dry_run:
        rep = dry_run_report(book, cfg)
        print(json.dumps(rep, indent=1))
        if rep["context_needed"] > cfg.llm.context_tokens:
            print(f"warning: largest prompt (~{rep['context_needed']} tokens) may exceed a {cfg.llm.context_tokens}-token context; "
                  f"consider --scene-tokens {int(cfg.scene_tokens * 0.7)}", file=sys.stderr)
        return 0

    log = lambda m: print(f"  · {m}", file=sys.stderr)
    client = make_client(cfg, fake=args.fake, log=log)
    if not args.fake:
        print(f"endpoint {cfg.llm.base_url}  model {cfg.llm.model}", file=sys.stderr)
        pr = client.probe()
        if not pr.get("ok"):
            print(f"error: cannot talk to the model: {pr.get('error') or pr}", file=sys.stderr)
            print("hint: run `sts probe` to diagnose, or set --base-url/--model", file=sys.stderr)
            return 3
        if pr.get("warning"):
            print("warning: " + pr["warning"], file=sys.stderr)

    stop = threading.Event()
    last = {"line": "", "t": 0.0}
    t0 = time.time()

    def progress(phase: str, done: int, total: int, msg: str) -> None:
        now = time.time()
        if msg == last["line"] and now - last["t"] < 1:
            return
        last["line"], last["t"] = msg, now
        pct = f"{100 * done // max(1, total):3d}%"
        elapsed = now - t0
        eta = ""
        if done and phase in ("analyse", "branch") and total > done:
            eta = f"  ETA {int(elapsed / done * (total - done))//60}m"
        print(f"[{phase:7s} {pct}] {msg}{eta}", file=sys.stderr)

    checkpoint = out + ".partial.json"
    try:
        adv = compile_book(book, cfg, client, checkpoint_path=checkpoint, progress=progress, log=log, stop_event=stop)
    except KeyboardInterrupt:
        stop.set()
        print(f"\ninterrupted — progress saved to {checkpoint}; run the same command again to resume", file=sys.stderr)
        return 130
    except CompileCancelled:
        return 130
    adv.save(out)
    if os.path.exists(checkpoint):
        os.remove(checkpoint)
    st = adv.stats()
    print(f"\nwrote {out}: {st['nodes']} nodes, {st['choice_points']} choice points, {st['endings']} endings, "
          f"{st['generated_words']} generated words alongside {st['canon_words']} original words "
          f"({client.usage.calls} LLM calls, {int(time.time() - t0)}s)", file=sys.stderr)
    problems = adv.validate()
    if problems:
        print("validation warnings: " + "; ".join(problems), file=sys.stderr)
    return 0


def cmd_probe(args: argparse.Namespace) -> int:
    cfg = build_config(args)
    client = make_client(cfg)
    print(f"probing {cfg.llm.base_url} (model {cfg.llm.model}) ...")
    res = client.probe(measure_context=args.measure_context)
    print(json.dumps(res, indent=1))
    if res.get("ok"):
        print("OK — the endpoint answers and produces JSON.")
        if res.get("approx_context_tokens"):
            print(f"approximate usable context: ~{res['approx_context_tokens']} tokens "
                  f"({'fine' if res['approx_context_tokens'] >= 6000 else 'too small: use --scene-tokens 1000 or a bigger context'})")
        return 0
    return 1


def cmd_info(args: argparse.Namespace) -> int:
    from .adventure import Adventure
    adv = Adventure.load(args.file)
    st = adv.stats()
    print(f"{adv.meta.get('title')} — {adv.meta.get('author')}")
    print(f"generated by {adv.meta.get('generator')} with {adv.meta.get('model')} on {adv.meta.get('created')}")
    print(json.dumps(st, indent=1))
    problems = adv.validate()
    print("valid" if not problems else "PROBLEMS: " + "; ".join(problems))
    if args.verbose:
        for n in adv.walk_canon():
            if len(n.choices) > 1:
                print(f"\n{n.id} ({n.chapter_title}): {n.question}")
                for c in n.choices:
                    print(f"   {'*' if c.canon else '-'} {c.label} -> {c.to}")
        print("\nendings:")
        for e in adv.endings():
            print(f"   {e.id}: {e.ending_title}")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    from .server import serve
    serve(port=args.port, host=args.host, library=args.library, open_browser=not args.no_browser,
          play_only=getattr(args, "play_only", False), initial_file=getattr(args, "file", None))
    return 0


def cmd_shim(args: argparse.Namespace) -> int:
    from .testing.claude_shim import serve
    serve(port=args.port, model=args.model, verbose=args.verbose)
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="sts", description="Steer The Story — turn a book into a choose-your-own-adventure.")
    ap.add_argument("--version", action="version", version=f"steerthestory {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("compile", help="turn a book into an .sts adventure file")
    p.add_argument("book", help="path to a .txt / .epub / .mobi file (DRM-free)")
    p.add_argument("-o", "--output", help="output .sts path (default: <book>.sts)")
    p.add_argument("--dry-run", action="store_true", help="only report scene/choice counts and prompt sizes")
    p.add_argument("--fake", action="store_true", help="use the built-in fake LLM (plumbing test, no model needed)")
    _add_llm_args(p)
    _add_compile_args(p)
    p.set_defaults(func=cmd_compile)

    p = sub.add_parser("probe", help="check that the LLM endpoint works")
    p.add_argument("--measure-context", action="store_true", help="also estimate the usable context length (slow)")
    _add_llm_args(p)
    p.set_defaults(func=cmd_probe)

    p = sub.add_parser("info", help="show statistics for an .sts file")
    p.add_argument("file")
    p.add_argument("-v", "--verbose", action="store_true", help="list choice points and endings")
    p.set_defaults(func=cmd_info)

    for name, help_ in (("serve", "run the web app (compile + play)"), ("play", "run the web app in play-only mode")):
        p = sub.add_parser(name, help=help_)
        if name == "play":
            p.add_argument("file", nargs="?", help=".sts file to open")
        p.add_argument("--port", type=int, default=8000)
        p.add_argument("--host", default="127.0.0.1")
        p.add_argument("--library", default="library", help="folder for uploads and compiled adventures")
        p.add_argument("--no-browser", action="store_true")
        p.set_defaults(func=cmd_serve, play_only=(name == "play"))

    p = sub.add_parser("shim", help="OpenAI-compatible test server backed by the `claude` CLI")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--model", default=os.environ.get("STS_SHIM_MODEL", "sonnet"))
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(func=cmd_shim)

    args = ap.parse_args(argv)
    return args.func(args)
