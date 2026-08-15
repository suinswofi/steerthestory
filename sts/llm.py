"""Minimal OpenAI-compatible chat client using only the standard library."""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Protocol

from .config import LLMConfig


class LLMError(Exception):
    pass


class LLMBadJSON(LLMError):
    pass


def estimate_tokens(text: str) -> int:
    """Cheap token estimate (~4 chars/token for English prose)."""
    return max(1, len(text) // 4)


@dataclass
class Usage:
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    seconds: float = 0.0

    def add(self, other: "Usage") -> None:
        self.calls += other.calls
        self.prompt_tokens += other.prompt_tokens
        self.completion_tokens += other.completion_tokens
        self.seconds += other.seconds

    def as_dict(self) -> dict[str, Any]:
        return {
            "calls": self.calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "seconds": round(self.seconds, 1),
        }


class ChatClient(Protocol):
    usage: Usage

    def chat(self, messages: list[dict[str, str]], *, json_mode: bool = False,
             max_tokens: int = 1024, temperature: Optional[float] = None) -> str: ...

    def chat_json(self, messages: list[dict[str, str]], *, max_tokens: int = 1024,
                  temperature: Optional[float] = None, required: tuple[str, ...] = ()) -> dict[str, Any]: ...


_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def extract_json(text: str) -> Any:
    """Pull the first JSON object/array out of a model reply, tolerating fences and chatter."""
    text = text.strip()
    if not text:
        raise LLMBadJSON("empty reply")
    candidates = [text]
    m = _FENCE_RE.search(text)
    if m:
        candidates.insert(0, m.group(1).strip())
    for cand in candidates:
        try:
            return json.loads(cand)
        except json.JSONDecodeError:
            pass
    # Find the outermost {...} or [...]
    for open_c, close_c in (("{", "}"), ("[", "]")):
        start = text.find(open_c)
        end = text.rfind(close_c)
        if start != -1 and end > start:
            chunk = text[start:end + 1]
            try:
                return json.loads(chunk)
            except json.JSONDecodeError:
                # Try trimming trailing commas which small models love to emit
                fixed = re.sub(r",\s*([}\]])", r"\1", chunk)
                try:
                    return json.loads(fixed)
                except json.JSONDecodeError:
                    continue
    raise LLMBadJSON("no JSON object found in reply: " + text[:200].replace("\n", " "))


@dataclass
class OpenAIClient:
    cfg: LLMConfig
    usage: Usage = field(default_factory=Usage)
    log: Optional[Callable[[str], None]] = None
    supports_json_mode: Optional[bool] = None  # learned on first failure

    # ---- low level -------------------------------------------------------------
    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = self.cfg.base_url.rstrip("/") + path
        data = json.dumps(payload).encode()
        headers = {"Content-Type": "application/json", "User-Agent": "steerthestory/0.1"}
        if self.cfg.api_key:
            headers["Authorization"] = f"Bearer {self.cfg.api_key}"
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=self.cfg.timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
        try:
            return json.loads(body)
        except json.JSONDecodeError as e:
            raise LLMError(f"non-JSON response from {url}: {body[:200]}") from e

    def _get(self, path: str) -> dict[str, Any]:
        url = self.cfg.base_url.rstrip("/") + path
        headers = {"User-Agent": "steerthestory/0.1"}
        if self.cfg.api_key:
            headers["Authorization"] = f"Bearer {self.cfg.api_key}"
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=min(self.cfg.timeout, 30)) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))

    def list_models(self) -> list[str]:
        data = self._get("/models")
        return [m.get("id", "") for m in data.get("data", []) if isinstance(m, dict)]

    # ---- chat ------------------------------------------------------------------
    def chat(self, messages: list[dict[str, str]], *, json_mode: bool = False,
             max_tokens: int = 1024, temperature: Optional[float] = None) -> str:
        payload: dict[str, Any] = {
            "model": self.cfg.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": self.cfg.temperature if temperature is None else temperature,
            "stream": False,
        }
        use_json = json_mode and self.supports_json_mode is not False
        if use_json:
            payload["response_format"] = {"type": "json_object"}

        last_err: Optional[Exception] = None
        for attempt in range(self.cfg.max_retries + 1):
            t0 = time.time()
            try:
                data = self._post("/chat/completions", payload)
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", "replace") if e.fp else ""
                if use_json and e.code in (400, 422) and "response_format" in body + str(payload) and attempt == 0:
                    # Server rejects JSON mode -> disable and retry immediately.
                    self.supports_json_mode = False
                    payload.pop("response_format", None)
                    use_json = False
                    self._log("server rejected response_format; falling back to plain text JSON")
                    continue
                last_err = LLMError(f"HTTP {e.code} from {self.cfg.base_url}: {body[:300]}")
                if e.code in (401, 403, 404):
                    raise last_err
                if e.code < 500 and e.code != 429:
                    raise last_err
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
                last_err = LLMError(f"connection error to {self.cfg.base_url}: {e}")
            else:
                dt = time.time() - t0
                try:
                    content = data["choices"][0]["message"]["content"] or ""
                except (KeyError, IndexError, TypeError) as e:
                    if "error" in data:
                        raise LLMError(f"API error: {data['error']}") from e
                    raise LLMError(f"unexpected response shape: {str(data)[:300]}") from e
                u = data.get("usage") or {}
                self.usage.add(Usage(
                    calls=1,
                    prompt_tokens=int(u.get("prompt_tokens") or sum(estimate_tokens(m["content"]) for m in messages)),
                    completion_tokens=int(u.get("completion_tokens") or estimate_tokens(content)),
                    seconds=dt,
                ))
                return content
            # backoff
            wait = min(60.0, 2.0 ** attempt)
            self._log(f"LLM call failed ({last_err}); retrying in {wait:.0f}s")
            time.sleep(wait)
        raise last_err or LLMError("LLM call failed")

    def chat_json(self, messages: list[dict[str, str]], *, max_tokens: int = 1024,
                  temperature: Optional[float] = None, required: tuple[str, ...] = ()) -> dict[str, Any]:
        """Chat and parse a JSON object; re-asks once with the parse error if needed."""
        reply = self.chat(messages, json_mode=True, max_tokens=max_tokens, temperature=temperature)
        try:
            obj = extract_json(reply)
            _check_required(obj, required)
            return obj
        except LLMBadJSON as e:
            self._log(f"bad JSON ({e}); asking model to fix")
            fix_msgs = messages + [
                {"role": "assistant", "content": reply},
                {"role": "user", "content": f"That was not valid JSON ({e}). Reply again with ONLY the JSON object, "
                                             f"no commentary, containing the keys: {', '.join(required) or 'as specified'}."},
            ]
            reply2 = self.chat(fix_msgs, json_mode=True, max_tokens=max_tokens, temperature=0.2)
            obj = extract_json(reply2)
            _check_required(obj, required)
            return obj

    # ---- probe -----------------------------------------------------------------
    def probe(self, *, measure_context: bool = False) -> dict[str, Any]:
        """Check reachability, model, JSON mode; optionally estimate usable context length."""
        result: dict[str, Any] = {"base_url": self.cfg.base_url, "model": self.cfg.model, "ok": False}
        try:
            models = self.list_models()
            result["models"] = models[:50]
            if models and self.cfg.model not in models:
                result["warning"] = f"model '{self.cfg.model}' not in server's model list"
        except Exception as e:  # /models is optional on many servers
            result["models_error"] = str(e)[:200]
        try:
            t0 = time.time()
            reply = self.chat([{"role": "user", "content": 'Reply with exactly this JSON and nothing else: {"ok": true}'}],
                              json_mode=True, max_tokens=32, temperature=0)
            result["latency_s"] = round(time.time() - t0, 2)
            obj = extract_json(reply)
            result["ok"] = bool(obj.get("ok")) if isinstance(obj, dict) else False
            result["json_mode"] = self.supports_json_mode is not False
            result["reply"] = reply[:100]
        except Exception as e:
            result["error"] = str(e)[:400]
            return result
        if measure_context and result["ok"]:
            result["approx_context_tokens"] = self._measure_context()
        return result

    def _measure_context(self) -> int:
        """Binary-search how large a prompt the server accepts (in ~tokens). Coarse but useful."""
        filler = "The quick brown fox jumps over the lazy dog. "  # ~10 tokens
        lo, hi = 1000, 65000
        best = 0
        while lo <= hi:
            mid = (lo + hi) // 2
            text = filler * (mid // 10)
            try:
                self.chat([{"role": "user", "content": text + "\nReply with the single word OK."}],
                          max_tokens=4, temperature=0)
                best = mid
                lo = mid + 2000
            except LLMError:
                hi = mid - 2000
        return best

    def _log(self, msg: str) -> None:
        if self.log:
            self.log(msg)


def _check_required(obj: Any, required: tuple[str, ...]) -> None:
    if not isinstance(obj, dict):
        raise LLMBadJSON("expected a JSON object")
    missing = [k for k in required if k not in obj]
    if missing:
        raise LLMBadJSON("missing keys: " + ", ".join(missing))
