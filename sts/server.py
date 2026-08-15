"""Local web app: compile books in the browser (drag & drop) and play .sts adventures.
Standard library only (ThreadingHTTPServer)."""
from __future__ import annotations

import json
import mimetypes
import os
import re
import threading
import time
import traceback
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional
from urllib.parse import unquote, urlparse

from . import __version__
from .config import CompileConfig, LLMConfig

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
MAX_UPLOAD = 200 * 1024 * 1024
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._ -]+")


def safe_name(name: str) -> str:
    name = os.path.basename(name or "book")
    name = _SAFE_NAME.sub("_", name).strip(" .") or "book"
    return name[:120]


class Job:
    def __init__(self, job_id: str, upload_path: str, filename: str, cfg: CompileConfig, out_path: str):
        self.id = job_id
        self.upload_path = upload_path
        self.filename = filename
        self.cfg = cfg
        self.out_path = out_path
        self.status = "queued"          # queued | running | done | error | cancelled
        self.phase = ""
        self.done = 0
        self.total = 0
        self.message = ""
        self.log: list[str] = []
        self.error = ""
        self.started = time.time()
        self.finished: Optional[float] = None
        self.usage: dict[str, Any] = {}
        self.stats: dict[str, Any] = {}
        self.title = ""
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.version = 0
        self.thread: Optional[threading.Thread] = None

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                "id": self.id, "status": self.status, "phase": self.phase, "done": self.done, "total": self.total,
                "message": self.message, "log": self.log[-60:], "error": self.error, "title": self.title,
                "started": self.started, "elapsed": round((self.finished or time.time()) - self.started, 1),
                "usage": self.usage, "stats": self.stats, "output": os.path.basename(self.out_path),
                "filename": self.filename, "version": self.version,
                "config": self.cfg.to_public_dict(),
            }

    def _bump(self) -> None:
        self.version += 1

    def add_log(self, msg: str) -> None:
        with self.lock:
            self.log.append(f"{time.strftime('%H:%M:%S')} {msg}")
            if len(self.log) > 400:
                del self.log[:100]
            self._bump()

    def progress(self, phase: str, done: int, total: int, msg: str) -> None:
        with self.lock:
            self.phase, self.done, self.total = phase, done, total
            if msg:
                self.message = msg
            self._bump()

    def run(self) -> None:
        from .compile import CompileCancelled, compile_book
        from .ingest import load_book
        from .llm import OpenAIClient
        try:
            with self.lock:
                self.status = "running"
                self._bump()
            book = load_book(self.upload_path, filename=self.filename)
            with self.lock:
                self.title = book.title
            if self.cfg.llm.base_url.startswith("fake://"):  # plumbing tests without a model
                from .testing.fake_llm import FakeLLM
                client = FakeLLM(delay=0.01)
            else:
                client = OpenAIClient(self.cfg.llm, log=self.add_log)
            probe = client.probe()
            if not probe.get("ok"):
                raise RuntimeError(f"cannot reach the model: {probe.get('error') or probe}")

            def usage_tick() -> None:
                with self.lock:
                    self.usage = client.usage.as_dict()

            def prog(phase: str, done: int, total: int, msg: str) -> None:
                self.progress(phase, done, total, msg)
                usage_tick()

            adv = compile_book(book, self.cfg, client, checkpoint_path=self.out_path + ".partial.json",
                               progress=prog, log=self.add_log, stop_event=self.stop)
            adv.save(self.out_path)
            cpp = self.out_path + ".partial.json"
            if os.path.exists(cpp):
                os.remove(cpp)
            with self.lock:
                self.status = "done"
                self.stats = adv.stats()
                self.usage = client.usage.as_dict()
                self.finished = time.time()
                self.message = "done"
                self._bump()
        except CompileCancelled:
            with self.lock:
                self.status = "cancelled"
                self.finished = time.time()
                self.message = "cancelled — progress kept in checkpoint; start again with the same file and settings to resume"
                self._bump()
        except Exception as e:  # noqa: BLE001
            self.add_log("ERROR: " + "".join(traceback.format_exception_only(type(e), e)).strip())
            with self.lock:
                self.status = "error"
                self.error = str(e)
                self.finished = time.time()
                self._bump()


class State:
    def __init__(self, library: str, play_only: bool):
        self.library = os.path.abspath(library)
        self.uploads = os.path.join(self.library, "uploads")
        os.makedirs(self.uploads, exist_ok=True)
        self.play_only = play_only
        self.jobs: dict[str, Job] = {}
        self.lock = threading.Lock()

    def list_library(self) -> list[dict[str, Any]]:
        out = []
        for name in sorted(os.listdir(self.library)):
            if not (name.endswith(".sts") or name.endswith(".sts.gz")):
                continue
            path = os.path.join(self.library, name)
            info: dict[str, Any] = {"name": name, "size": os.path.getsize(path),
                                    "mtime": int(os.path.getmtime(path))}
            try:
                # cheap peek at meta without loading everything
                from .adventure import Adventure
                adv = Adventure.load(path)
                info.update({"title": adv.meta.get("title"), "author": adv.meta.get("author"),
                             "nodes": len(adv.nodes), "endings": len(adv.endings()),
                             "model": adv.meta.get("model")})
            except Exception:  # noqa: BLE001
                info["title"] = name
            out.append(info)
        return out


class Handler(BaseHTTPRequestHandler):
    server_version = f"steerthestory/{__version__}"
    state: State
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        if os.environ.get("STS_HTTP_LOG"):
            super().log_message(fmt, *args)

    # ---- helpers ----------------------------------------------------------------------------
    def _send(self, code: int, body: bytes, ctype: str = "application/json; charset=utf-8",
              extra: Optional[dict[str, str]] = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code: int, obj: Any) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def _error(self, code: int, msg: str) -> None:
        self._json(code, {"error": msg})

    def _read_body(self, limit: int = MAX_UPLOAD) -> bytes:
        n = int(self.headers.get("Content-Length") or 0)
        if n > limit:
            raise ValueError(f"request too large ({n} bytes)")
        return self.rfile.read(n) if n else b""

    def _read_json(self) -> dict[str, Any]:
        raw = self._read_body(8 * 1024 * 1024)
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))

    def _job(self, job_id: str) -> Optional[Job]:
        return self.state.jobs.get(job_id)

    # ---- routing -----------------------------------------------------------------------------
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path = urlparse(self.path).path
        try:
            if path in ("/", "/index.html"):
                return self._static("index.html")
            if path.startswith("/api/"):
                return self._api_get(path)
            return self._static(path.lstrip("/"))
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            self._error(500, str(e))

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            return self._api_post(path)
        except json.JSONDecodeError:
            self._error(400, "invalid JSON")
        except ValueError as e:
            self._error(400, str(e))
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            self._error(500, str(e))

    def do_DELETE(self):
        path = urlparse(self.path).path
        m = re.fullmatch(r"/api/library/([^/]+)", path)
        if m:
            name = safe_name(unquote(m.group(1)))
            p = os.path.join(self.state.library, name)
            if os.path.isfile(p) and (name.endswith(".sts") or name.endswith(".sts.gz")):
                os.remove(p)
                return self._json(200, {"ok": True})
            return self._error(404, "no such file")
        self._error(404, "not found")

    def _static(self, rel: str) -> None:
        rel = os.path.normpath(rel).lstrip("./")
        full = os.path.join(WEB_DIR, rel)
        if not full.startswith(WEB_DIR) or not os.path.isfile(full):
            return self._error(404, "not found")
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype == "application/javascript":
            ctype += "; charset=utf-8"
        with open(full, "rb") as f:
            self._send(200, f.read(), ctype)

    def _api_get(self, path: str) -> None:
        st = self.state
        if path == "/api/status":
            env = LLMConfig.from_env()
            return self._json(200, {
                "version": __version__, "play_only": st.play_only,
                "defaults": {"base_url": env.base_url, "model": env.model, "has_api_key": bool(env.api_key)},
                "config_defaults": CompileConfig().to_public_dict(),
                "library": st.list_library(),
                "jobs": [j.snapshot() for j in st.jobs.values()],
            })
        if path == "/api/library":
            return self._json(200, {"library": st.list_library()})
        m = re.fullmatch(r"/api/library/([^/]+)", path)
        if m:
            name = safe_name(unquote(m.group(1)))
            p = os.path.join(st.library, name)
            if not os.path.isfile(p):
                return self._error(404, "no such adventure")
            with open(p, "rb") as f:
                data = f.read()
            ctype = "application/gzip" if name.endswith(".gz") else "application/json; charset=utf-8"
            return self._send(200, data, ctype, {"Content-Disposition": f'attachment; filename="{name}"'})
        if path == "/api/jobs":
            return self._json(200, {"jobs": [j.snapshot() for j in st.jobs.values()]})
        m = re.fullmatch(r"/api/jobs/([^/]+)(?:/(events|download))?", path)
        if m:
            job = self._job(m.group(1))
            if not job:
                return self._error(404, "no such job")
            if m.group(2) == "events":
                return self._sse(job)
            if m.group(2) == "download":
                if job.status != "done" or not os.path.exists(job.out_path):
                    return self._error(409, "job not finished")
                with open(job.out_path, "rb") as f:
                    data = f.read()
                return self._send(200, data, "application/json; charset=utf-8",
                                  {"Content-Disposition": f'attachment; filename="{os.path.basename(job.out_path)}"'})
            return self._json(200, job.snapshot())
        self._error(404, "not found")

    def _sse(self, job: Job) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        last_version = -1
        try:
            while True:
                snap = job.snapshot()
                if snap["version"] != last_version:
                    last_version = snap["version"]
                    self.wfile.write(f"data: {json.dumps(snap, ensure_ascii=False)}\n\n".encode("utf-8"))
                    self.wfile.flush()
                if snap["status"] in ("done", "error", "cancelled"):
                    break
                time.sleep(0.5)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _api_post(self, path: str) -> None:
        st = self.state
        if path == "/api/probe":
            body = self._read_json()
            cfg = CompileConfig.from_dict({"llm": body})
            from .llm import OpenAIClient
            client = OpenAIClient(cfg.llm)
            return self._json(200, client.probe(measure_context=bool(body.get("measure_context"))))
        if st.play_only and path in ("/api/upload", "/api/compile", "/api/dryrun"):
            return self._error(403, "server is running in play-only mode")
        if path == "/api/upload":
            filename = safe_name(unquote(self.headers.get("X-Filename") or "book.txt"))
            data = self._read_body()
            if not data:
                return self._error(400, "empty upload")
            upload_id = uuid.uuid4().hex[:10]
            p = os.path.join(st.uploads, f"{upload_id}-{filename}")
            with open(p, "wb") as f:
                f.write(data)
            from .compile import dry_run_report
            from .ingest import UnsupportedFormat, load_book
            try:
                book = load_book(p, filename=filename)
            except UnsupportedFormat as e:
                os.remove(p)
                return self._error(400, str(e))
            rep = dry_run_report(book, CompileConfig())
            return self._json(200, {"upload": upload_id, "filename": filename, "title": book.title,
                                    "author": book.author, "chapters": len(book.chapters), "words": book.words,
                                    "chapter_titles": [c.title for c in book.chapters][:400], "estimate": rep})
        if path == "/api/dryrun":
            body = self._read_json()
            p = self._upload_path(body.get("upload", ""))
            cfg = CompileConfig.from_dict(body.get("config") or {})
            from .compile import dry_run_report
            from .ingest import load_book
            book = load_book(p, filename=body.get("filename") or os.path.basename(p))
            return self._json(200, dry_run_report(book, cfg))
        if path == "/api/compile":
            body = self._read_json()
            p = self._upload_path(body.get("upload", ""))
            cfg = CompileConfig.from_dict(body.get("config") or {})
            filename = safe_name(body.get("filename") or os.path.basename(p))
            base = os.path.splitext(filename)[0]
            if cfg.chapters:
                base += f"-ch{cfg.chapters.replace('-', 'to')}"
            out = os.path.join(st.library, base + ".sts")
            job = Job(uuid.uuid4().hex[:8], p, filename, cfg, out)
            with st.lock:
                st.jobs[job.id] = job
            job.thread = threading.Thread(target=job.run, name=f"compile-{job.id}", daemon=True)
            job.thread.start()
            return self._json(200, {"job": job.id, "output": os.path.basename(out)})
        m = re.fullmatch(r"/api/jobs/([^/]+)/cancel", path)
        if m:
            job = self._job(m.group(1))
            if not job:
                return self._error(404, "no such job")
            job.stop.set()
            job.add_log("cancel requested — finishing the current LLM call…")
            return self._json(200, {"ok": True})
        self._error(404, "not found")

    def _upload_path(self, upload_id: str) -> str:
        if not re.fullmatch(r"[0-9a-f]{10}", upload_id or ""):
            raise ValueError("bad upload id")
        for name in os.listdir(self.state.uploads):
            if name.startswith(upload_id + "-"):
                return os.path.join(self.state.uploads, name)
        raise ValueError("upload not found (server restarted?) — drop the book again")


def serve(port: int = 8000, host: str = "127.0.0.1", library: str = "library", open_browser: bool = True,
          play_only: bool = False, initial_file: Optional[str] = None) -> None:
    Handler.state = State(library, play_only)
    if initial_file:
        # copy the given .sts into the library so the UI can list it
        import shutil
        dst = os.path.join(Handler.state.library, safe_name(os.path.basename(initial_file)))
        if os.path.abspath(initial_file) != os.path.abspath(dst):
            shutil.copyfile(initial_file, dst)
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    url = f"http://{host}:{port}/"
    print(f"Steer The Story {__version__} — {url}  (library: {Handler.state.library}){'  [play only]' if play_only else ''}")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
    finally:
        httpd.server_close()
