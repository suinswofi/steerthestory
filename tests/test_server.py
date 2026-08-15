import json
import os
import threading
import time
import unittest
import urllib.request
from http.server import ThreadingHTTPServer

from sts import server as srv

FIX = os.path.join(os.path.dirname(__file__), "fixtures")


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import tempfile
        cls.tmp = tempfile.TemporaryDirectory()
        srv.Handler.state = srv.State(cls.tmp.name, play_only=False)
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
        cls.httpd.daemon_threads = True
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.tmp.cleanup()

    def req(self, path, data=None, headers=None, method=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read()
            return r.status, r.headers, body

    def test_flow(self):
        st, _, body = self.req("/")
        self.assertEqual(st, 200)
        self.assertIn(b"Steer", body)
        st, _, body = self.req("/api/status")
        self.assertEqual(json.loads(body)["play_only"], False)
        with open(os.path.join(FIX, "mini.epub"), "rb") as f:
            raw = f.read()
        st, _, body = self.req("/api/upload", data=raw, headers={"X-Filename": "mini.epub", "Content-Type": "application/octet-stream"})
        up = json.loads(body)
        self.assertEqual(up["chapters"], 4)
        cfg = {"scene_tokens": 500, "choice_every": 2, "branch_len": 2, "concurrency": 2, "llm": {"base_url": "fake://", "model": "fake"}}
        st, _, body = self.req("/api/dryrun", data=json.dumps({"upload": up["upload"], "filename": "mini.epub", "config": cfg}).encode())
        self.assertGreater(json.loads(body)["scenes"], 4)
        st, _, body = self.req("/api/compile", data=json.dumps({"upload": up["upload"], "filename": "mini.epub", "config": cfg}).encode())
        job = json.loads(body)["job"]
        # SSE stream ends when the job finishes
        st, hdrs, body = self.req(f"/api/jobs/{job}/events")
        self.assertIn("text/event-stream", hdrs["Content-Type"])
        events = [json.loads(l[6:]) for l in body.decode().splitlines() if l.startswith("data: ")]
        self.assertEqual(events[-1]["status"], "done")
        st, hdrs, body = self.req(f"/api/jobs/{job}/download")
        adv = json.loads(body)
        self.assertEqual(adv["format"], "sts/1")
        st, _, body = self.req("/api/library")
        lib = json.loads(body)["library"]
        self.assertEqual(len(lib), 1)
        st, _, body = self.req("/api/library/" + lib[0]["name"])
        self.assertEqual(json.loads(body)["meta"]["title"], "Mini Alice EPUB")
        st, _, _ = self.req("/api/library/" + lib[0]["name"], method="DELETE")
        self.assertEqual(json.loads(self.req("/api/library")[2])["library"], [])

    def test_bad_upload(self):
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.req("/api/upload", data=b"%PDF-1.4 nope", headers={"X-Filename": "x.pdf"})
        self.assertEqual(cm.exception.code, 400)


if __name__ == "__main__":
    unittest.main()
