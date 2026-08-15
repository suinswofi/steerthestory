"""A tiny OpenAI-compatible server that answers /v1/chat/completions by shelling out to the
`claude` CLI (`claude -p`). Only for testing STS on machines without a local model.

    python -m sts shim --port 8765 [--model sonnet]
    python -m sts compile book.txt --base-url http://localhost:8765/v1
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional

DEFAULT_MODEL = os.environ.get("STS_SHIM_MODEL", "sonnet")


def _run_claude(system: str, prompt: str, model: Optional[str], timeout: float) -> str:
    cmd = ["claude", "-p", "--tools", "", "--no-session-persistence",
           "--output-format", "text", "--system-prompt", system or "You are a helpful assistant."]
    if model:
        cmd += ["--model", model]
    # Run in a neutral directory so no project CLAUDE.md leaks into the prompt.
    cwd = tempfile.gettempdir()
    proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=timeout, cwd=cwd)
    if proc.returncode != 0:
        raise RuntimeError(f"claude exited {proc.returncode}: {proc.stderr.strip()[:500]}")
    return proc.stdout.strip()


def _flatten(messages: list[dict[str, Any]]) -> tuple[str, str]:
    system_parts, convo = [], []
    for m in messages:
        role = m.get("role")
        content = m.get("content")
        if isinstance(content, list):  # OpenAI content-part arrays
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        content = content or ""
        if role == "system":
            system_parts.append(content)
        elif role == "assistant":
            convo.append(f"[Your previous reply]\n{content}")
        else:
            convo.append(content)
    return "\n\n".join(system_parts), "\n\n".join(convo)


class Handler(BaseHTTPRequestHandler):
    server_version = "sts-claude-shim/0.1"
    model: str = DEFAULT_MODEL
    timeout_s: float = 600.0
    verbose: bool = False

    def log_message(self, fmt, *args):  # quieter
        if self.verbose:
            super().log_message(fmt, *args)

    def _json(self, code: int, obj: Any) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.rstrip("/").endswith("/models"):
            return self._json(200, {"object": "list", "data": [{"id": self.model, "object": "model"}]})
        self._json(404, {"error": "not found"})

    def do_POST(self):
        if not self.path.rstrip("/").endswith("/chat/completions"):
            return self._json(404, {"error": "not found"})
        n = int(self.headers.get("Content-Length") or 0)
        try:
            req = json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            return self._json(400, {"error": {"message": "bad json"}})
        system, prompt = _flatten(req.get("messages") or [])
        if req.get("response_format", {}).get("type") == "json_object":
            system += "\n\nRespond with a single valid JSON object only."
        model = req.get("model") or self.model
        if model and not any(k in model for k in ("claude", "sonnet", "opus", "haiku", "fable")):
            model = self.model
        t0 = time.time()
        try:
            text = _run_claude(system, prompt, model, self.timeout_s)
        except Exception as e:  # noqa: BLE001
            return self._json(500, {"error": {"message": str(e)}})
        if self.verbose:
            print(f"[shim] {len(prompt)//4}+{len(text)//4} tok in {time.time()-t0:.1f}s")
        self._json(200, {
            "id": "chatcmpl-" + uuid.uuid4().hex[:12],
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": (len(system) + len(prompt)) // 4, "completion_tokens": len(text) // 4,
                      "total_tokens": (len(system) + len(prompt) + len(text)) // 4},
        })


def serve(port: int = 8765, model: str = DEFAULT_MODEL, verbose: bool = False, timeout: float = 600.0) -> None:
    Handler.model = model
    Handler.verbose = verbose
    Handler.timeout_s = timeout
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"claude shim listening on http://127.0.0.1:{port}/v1  (model={model})")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
