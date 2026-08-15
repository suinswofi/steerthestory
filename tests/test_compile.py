import json
import os
import tempfile
import threading
import unittest

from sts.adventure import Adventure, Choice, Node
from sts.compile import CompileCancelled, compile_book, dry_run_report
from sts.compile.checkpoint import Checkpoint
from sts.compile.choices import plan_choice_points
from sts.config import CompileConfig
from sts.ingest import load_book
from sts.llm import extract_json, LLMBadJSON
from sts.prompts import parse_scene_reply
from sts.testing.fake_llm import FakeLLM

FIX = os.path.join(os.path.dirname(__file__), "fixtures")


class UnitTests(unittest.TestCase):
    def test_extract_json_tolerant(self):
        self.assertEqual(extract_json('```json\n{"a": 1,}\n```'), {"a": 1})
        self.assertEqual(extract_json('Sure: {"a": [1,2,]} ok'), {"a": [1, 2]})
        with self.assertRaises(LLMBadJSON):
            extract_json("no json here")

    def test_parse_scene_reply(self):
        text, summary, title = parse_scene_reply("Once upon a time.\n\nMore prose.\n\nSUMMARY: it happened.\nTITLE: The End of It")
        self.assertEqual(text, "Once upon a time.\n\nMore prose.")
        self.assertEqual(summary, "it happened.")
        self.assertEqual(title, "The End of It")

    def test_plan_choice_points(self):
        cfg = CompileConfig(choice_every=3, branches=2, rejoin_after=3, ending_ratio=0.0)
        plans = plan_choice_points(20, cfg)
        self.assertEqual([p.scene_index for p in plans], [2, 5, 8, 11, 14, 17])
        # near the end, rejoin impossible -> endings
        self.assertTrue(all(a.outcome == "ending" for a in plans[-1].alts))
        self.assertTrue(all(a.outcome == "rejoin" and a.rejoin_index == 6 for a in plans[0].alts))
        cfg2 = CompileConfig(choice_every=3, branches=2, rejoin_after=3, ending_ratio=0.5)
        outs = [a.outcome for p in plan_choice_points(40, cfg2) for a in p.alts]
        self.assertIn("ending", outs)
        self.assertIn("rejoin", outs)

    def test_config_hash_ignores_endpoint(self):
        a = CompileConfig(); b = CompileConfig()
        b.llm.model = "other"; b.concurrency = 8
        self.assertEqual(a.config_hash(), b.config_hash())
        b.choice_every = 5
        self.assertNotEqual(a.config_hash(), b.config_hash())

    def test_adventure_validate(self):
        adv = Adventure({}, "", {}, "c001", {
            "c001": Node("c001", "canon", "x", choices=[Choice("go", "c002", True), Choice("nope", "missing")]),
            "c002": Node("c002", "ending", "y"),
            "zz": Node("zz", "branch", "orphan", choices=[Choice("c", "c002")]),
        })
        problems = adv.validate()
        self.assertTrue(any("missing" in p for p in problems))
        self.assertTrue(any("unreachable" in p for p in problems))


class CompileTests(unittest.TestCase):
    def setUp(self):
        self.book = load_book(os.path.join(FIX, "mini.txt"))
        self.tmp = tempfile.TemporaryDirectory()
        self.cp = os.path.join(self.tmp.name, "x.partial.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_dry_run(self):
        rep = dry_run_report(self.book, CompileConfig(scene_tokens=500))
        self.assertGreater(rep["scenes"], 4)
        self.assertGreater(rep["llm_calls"], rep["scenes"])
        self.assertLess(rep["context_needed"], 8000)

    def test_full_compile_valid_graph(self):
        cfg = CompileConfig(scene_tokens=500, choice_every=2, branches=2, branch_len=2, rejoin_after=2, ending_ratio=0.3, concurrency=3)
        llm = FakeLLM()
        adv = compile_book(self.book, cfg, llm, checkpoint_path=self.cp)
        self.assertEqual(adv.validate(), [])
        st = adv.stats()
        self.assertGreater(st["choice_points"], 1)
        self.assertGreater(st["endings"], 1)
        # every canon scene present verbatim and in order
        canon = list(adv.walk_canon())
        self.assertEqual(canon[0].id, "c001")
        self.assertEqual(canon[-1].kind, "ending")
        self.assertEqual(sum(len(n.text.split()) for n in canon), self.book.words)
        # a rejoin branch actually points back at a canon node
        rejoins = [n for n in adv.nodes.values() if n.branch_id and n.choices and n.choices[0].to.startswith("c")]
        self.assertTrue(rejoins)
        # choice nodes: exactly one canon option
        for n in adv.nodes.values():
            if len(n.choices) > 1:
                self.assertEqual(sum(1 for c in n.choices if c.canon), 1)
                self.assertTrue(n.question)
        # round trip
        p = os.path.join(self.tmp.name, "a.sts.gz")
        adv.save(p)
        adv2 = Adventure.load(p)
        self.assertEqual(adv2.to_dict(), adv.to_dict())
        self.assertEqual(rep_calls(llm), llm.usage.calls)

    def test_cancel_and_resume_wastes_no_calls(self):
        cfg = CompileConfig(scene_tokens=500, choice_every=2, branches=2, branch_len=2, concurrency=2)
        total = FakeLLM()
        compile_book(self.book, cfg, total)  # reference count without checkpoint
        stop = threading.Event()
        llm1 = FakeLLM(delay=0.01)
        threading.Timer(0.25, stop.set).start()
        with self.assertRaises(CompileCancelled):
            compile_book(self.book, cfg, llm1, checkpoint_path=self.cp, stop_event=stop)
        self.assertTrue(os.path.exists(self.cp))
        cp, ok = Checkpoint.load_or_new(self.cp, self.book.source_sha256, cfg.config_hash())
        self.assertTrue(ok)
        llm2 = FakeLLM()
        adv = compile_book(self.book, cfg, llm2, checkpoint_path=self.cp)
        self.assertEqual(adv.validate(), [])
        self.assertLessEqual(llm1.usage.calls + llm2.usage.calls, total.usage.calls + cfg.concurrency)

    def test_checkpoint_rejected_on_config_change(self):
        cfg = CompileConfig(scene_tokens=500)
        compile_book(self.book, cfg, FakeLLM(), checkpoint_path=self.cp)
        # compile_book leaves the checkpoint in place; the CLI removes it. Change config -> ignored.
        cfg2 = CompileConfig(scene_tokens=500, choice_every=5)
        cp, ok = Checkpoint.load_or_new(self.cp, self.book.source_sha256, cfg2.config_hash())
        self.assertFalse(ok)


def rep_calls(llm):
    return len(llm.calls)


if __name__ == "__main__":
    unittest.main()
