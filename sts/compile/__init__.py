"""Compile orchestrator: book -> Adventure (.sts)."""
from __future__ import annotations

import datetime as _dt
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Optional

from .. import __version__
from ..adventure import Adventure, Choice, Node
from ..config import CompileConfig
from ..ingest import Book
from ..llm import ChatClient, LLMBadJSON, LLMError, estimate_tokens
from ..prompts import branch_scene_prompt, choice_prompt, scene_prompt
from ..segment import Scene, segment_book
from .bible import run_bible_pass, run_setup
from .branches import branch_id, generate_branch
from .checkpoint import Checkpoint
from .choices import ChoicePlan, design_choice, plan_choice_points, shuffled_option_order

ProgressFn = Callable[[str, int, int, str], None]   # (phase, done, total, message)
LogFn = Callable[[str], None]


class CompileCancelled(Exception):
    pass


def _noop_progress(phase: str, done: int, total: int, msg: str) -> None:
    pass


def _noop_log(msg: str) -> None:
    pass


def prepare(book: Book, cfg: CompileConfig) -> tuple[Book, list[Scene], list[ChoicePlan]]:
    if cfg.chapters:
        book = book.slice_chapters(cfg.chapters)
    scenes = segment_book(book, scene_tokens=cfg.scene_tokens, max_scenes=cfg.max_scenes)
    if len(scenes) < 2:
        raise ValueError("book is too short to make an adventure (need at least 2 scenes)")
    plans = plan_choice_points(len(scenes), cfg)
    return book, scenes, plans


def dry_run_report(book: Book, cfg: CompileConfig) -> dict[str, Any]:
    """Estimate calls and prompt sizes without touching the LLM."""
    book, scenes, plans = prepare(book, cfg)
    n_alts = sum(len(p.alts) for p in plans)
    calls = len(scenes) + 1 + len(plans) + n_alts * cfg.branch_len
    biggest_scene = max(scenes, key=lambda s: s.tokens)
    fake_bible = {"protagonist": "X", "setting": "s" * 200, "themes": ["a"] * 4,
                  "characters": [{"name": "Name %d" % i, "role": "r" * 80} for i in range(12)]}
    fake_summary = "word " * 220
    p1 = scene_prompt(fake_bible, fake_summary, biggest_scene.text, 1, len(scenes))
    p2 = choice_prompt(fake_bible, fake_summary, biggest_scene.text, "word " * 60, cfg.branches)
    p3 = branch_scene_prompt("word " * 300, fake_bible, fake_summary, "word " * 220, "word " * 40, "word " * cfg.branch_scene_words,
                             2, cfg.branch_len, cfg.branch_scene_words, rejoin_summary="word " * 200, rejoin_opening="word " * 45)
    tok = lambda msgs: sum(estimate_tokens(m["content"]) for m in msgs)
    return {
        "title": book.title, "author": book.author, "chapters": len(book.chapters), "words": book.words,
        "scenes": len(scenes), "choice_points": len(plans), "branches": n_alts,
        "endings": sum(1 for p in plans for a in p.alts if a.outcome == "ending") + 1,
        "generated_scenes": n_alts * cfg.branch_len,
        "llm_calls": calls,
        "est_output_tokens": len(scenes) * 350 + len(plans) * 250 + n_alts * cfg.branch_len * int(cfg.branch_scene_words * 1.4),
        "max_prompt_tokens": {"scene_analysis": tok(p1), "choice_design": tok(p2), "branch_scene": tok(p3)},
        "context_needed": max(tok(p1), tok(p2), tok(p3)) + 900,
    }


def compile_book(book: Book, cfg: CompileConfig, client: ChatClient, *,
                 checkpoint_path: Optional[str] = None,
                 progress: ProgressFn = _noop_progress, log: LogFn = _noop_log,
                 stop_event: Optional[threading.Event] = None) -> Adventure:
    should_stop = (lambda: stop_event.is_set()) if stop_event else (lambda: False)
    t_start = time.time()
    book, scenes, plans = prepare(book, cfg)
    cp, resumed = Checkpoint.load_or_new(checkpoint_path, book.source_sha256, cfg.config_hash(), resume=cfg.resume)
    if resumed:
        log(f"resuming from checkpoint: {cp.progress_summary()}")
    log(f"{book.title!r}: {len(book.chapters)} chapters, {book.words} words -> {len(scenes)} scenes, "
        f"{len(plans)} choice points, {sum(len(p.alts) for p in plans)} branches")

    # ---- Pass 1a: setup / style guide -------------------------------------------------------
    if not cp.state.get("setup"):
        progress("setup", 0, 1, "reading the opening to learn the author's voice")
        cp.state["setup"] = run_setup(client, scenes, book.title, book.author)
        cp.save()
    setup = cp.state["setup"]
    log(f"protagonist: {setup['protagonist']}; POV: {setup['pov']}")
    progress("setup", 1, 1, "style guide ready")

    # ---- Pass 1b: rolling bible ---------------------------------------------------------------
    run_bible_pass(client, scenes, setup, cp,
                   progress=lambda d, t, m: progress("analyse", d, t, m), log=log, should_stop=should_stop)
    if should_stop():
        raise CompileCancelled()
    scene_records = cp.state["scenes"]

    # ---- Pass 2+3: choices and branches (parallel per choice point) ---------------------------
    units_total = sum(1 + len(p.alts) * cfg.branch_len for p in plans)
    units_done = _count_done_units(cp, plans, cfg)
    lock = threading.Lock()

    def bump(msg: str, n: int = 1) -> None:
        nonlocal units_done
        with lock:
            units_done += n
            progress("branch", units_done, units_total, msg)

    progress("branch", units_done, units_total, "designing choices and writing branches")

    def design(plan: ChoicePlan) -> None:
        if should_stop():
            return
        i = plan.scene_index
        key = str(i)
        if cp.state["choices"].get(key) is not None:
            return
        running_before = scene_records[str(i - 1)]["running_summary"] if i > 0 else ""
        try:
            d = design_choice(client, scene_records[key]["bible"], running_before, scenes[i],
                              scene_records[str(i + 1)]["summary"], len(plan.alts))
        except LLMBadJSON as e:
            log(f"choice at scene {i + 1}: model could not design options ({e}); skipping this choice point")
            d = {"skipped": True}
        with cp.lock:
            cp.state["choices"][key] = d
        cp.save()
        bump(f"choice at scene {i + 1} designed" + (f": {d.get('question', '')}" if not d.get("skipped") else ""))

    def write_branch(plan: ChoicePlan, alt) -> None:
        if should_stop():
            return
        design_ = cp.state["choices"].get(str(plan.scene_index))
        if not design_ or design_.get("skipped"):
            return
        bid = branch_id(plan, alt)
        with cp.lock:
            brec = cp.state["branches"].setdefault(bid, {"nodes": [], "done": False})
        if brec.get("done"):
            return
        premise = design_["alternatives"][alt.index]["premise"]

        def on_scene(node: dict[str, Any]) -> None:
            with cp.lock:
                cp.state["branches"][bid]["nodes"].append(node)
                cp.state["usage"] = client.usage.as_dict()
                n_done = len(cp.state["branches"][bid]["nodes"])
            cp.save()
            bump(f"{bid}: scene {n_done}/{cfg.branch_len} written ({alt.outcome})")

        try:
            generate_branch(client, cfg, setup["style_guide"], scenes, scene_records, plan, alt, premise,
                            list(brec["nodes"]), on_scene=on_scene, should_stop=should_stop, log=log)
        except LLMError as e:
            log(f"{bid}: generation failed ({e}); branch will be truncated")
            bump("", n=max(0, cfg.branch_len - len(cp.state["branches"][bid]["nodes"])))
        if not should_stop():
            with cp.lock:
                cp.state["branches"][bid]["done"] = True
            cp.save()

    workers = max(1, cfg.concurrency)
    branch_jobs = [(p, a) for p in plans for a in p.alts]
    if workers == 1:
        for plan in plans:
            design(plan)
        for plan, alt in branch_jobs:
            write_branch(plan, alt)
    else:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for f in as_completed([ex.submit(design, p) for p in plans]):
                f.result()
            for f in as_completed([ex.submit(write_branch, p, a) for p, a in branch_jobs]):
                f.result()
    if should_stop():
        raise CompileCancelled()

    # ---- Assemble ---------------------------------------------------------------------------
    adv = assemble(book, cfg, scenes, plans, cp, client, elapsed=time.time() - t_start)
    problems = adv.validate()
    if problems:
        log("validation problems: " + "; ".join(problems[:5]))
    progress("done", 1, 1, "adventure complete")
    return adv


def _count_done_units(cp: Checkpoint, plans: list[ChoicePlan], cfg: CompileConfig) -> int:
    n = 0
    for p in plans:
        d = cp.state["choices"].get(str(p.scene_index))
        if d is None:
            continue
        n += 1
        if d.get("skipped"):
            n += len(p.alts) * cfg.branch_len
            continue
        for alt in p.alts:
            b = cp.state["branches"].get(branch_id(p, alt))
            if b:
                n += cfg.branch_len if b.get("done") else len(b["nodes"])
    return n


def assemble(book: Book, cfg: CompileConfig, scenes: list[Scene], plans: list[ChoicePlan], cp: Checkpoint,
             client: ChatClient, *, elapsed: float = 0.0) -> Adventure:
    setup = cp.state["setup"]
    records = cp.state["scenes"]
    plan_by_scene = {p.scene_index: p for p in plans}
    nodes: dict[str, Node] = {}
    truncated = bool(cfg.max_scenes or cfg.chapters)
    for sc in scenes:
        rec = records.get(str(sc.index), {})
        n = Node(id=sc.id, kind="canon", text=sc.text, summary=rec.get("summary", ""),
                 chapter=sc.chapter, chapter_title=sc.chapter_title)
        last = sc.index == len(scenes) - 1
        if last:
            n.kind = "ending"
            n.ending_title = "To be continued…" if truncated else "The original ending"
        else:
            nxt = scenes[sc.index + 1].id
            plan = plan_by_scene.get(sc.index)
            design = cp.state["choices"].get(str(sc.index)) if plan else None
            if design and not design.get("skipped"):
                options: list[Choice] = [Choice(design["canon_label"], nxt, canon=True)]
                for alt in plan.alts:
                    bid = branch_id(plan, alt)
                    brec = cp.state["branches"].get(bid, {"nodes": []})
                    bnodes = brec["nodes"]
                    if not bnodes:
                        continue
                    # repair truncated branches: make sure the last node terminates properly
                    for k, bn in enumerate(bnodes):
                        node = Node(id=bn["id"], kind=bn["kind"], text=bn["text"], summary=bn.get("summary", ""),
                                    choices=[Choice(c["label"], c["to"]) for c in bn.get("choices", [])],
                                    ending_title=bn.get("ending_title", ""), branch_id=bid)
                        if k == len(bnodes) - 1 and node.kind != "ending" and (
                                not node.choices or node.choices[0].to.startswith(bid)):
                            if alt.outcome == "rejoin":
                                node.choices = [Choice("Continue", scenes[alt.rejoin_index].id)]
                            else:
                                node.kind, node.choices = "ending", []
                                node.ending_title = node.ending_title or "An abrupt ending"
                        nodes[node.id] = node
                    options.append(Choice(design["alternatives"][alt.index]["label"], bnodes[0]["id"]))
                if len(options) > 1:
                    order = shuffled_option_order(cfg.seed, sc.index, len(options))
                    n.choices = [options[k] for k in order]
                    n.question = design.get("question", "")
                else:
                    n.choices = [Choice("Continue", nxt, canon=True)]
            else:
                n.choices = [Choice("Continue", nxt, canon=True)]
        nodes[n.id] = n
    meta = {
        "title": book.title,
        "author": book.author,
        "source_sha256": book.source_sha256,
        "created": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        "generator": f"steerthestory {__version__}",
        "model": cfg.llm.model,
        "endpoint_hint": cfg.llm.base_url,
        "config": cfg.to_public_dict(),
        "usage": client.usage.as_dict(),
        "compile_seconds": round(elapsed, 1),
        "protagonist": setup.get("protagonist", ""),
        "pov": setup.get("pov", ""),
        "chapters": len(book.chapters),
        "book_words": book.words,
    }
    bible = dict(records[str(len(scenes) - 1)]["bible"]) if records else {}
    bible["themes"] = setup.get("themes", [])
    return Adventure(meta=meta, style_guide=setup.get("style_guide", ""), bible=bible,
                     start=scenes[0].id, nodes=nodes)
