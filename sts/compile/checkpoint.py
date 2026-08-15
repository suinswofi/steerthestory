"""Atomic JSON checkpoint so long compiles can resume after a crash or Ctrl-C."""
from __future__ import annotations

import json
import os
import threading
from typing import Any, Optional


class Checkpoint:
    def __init__(self, path: Optional[str], source_sha256: str, config_hash: str):
        self.path = path
        self.lock = threading.Lock()
        self.state: dict[str, Any] = {
            "version": 1,
            "source_sha256": source_sha256,
            "config_hash": config_hash,
            "setup": None,          # style/protagonist info
            "scenes": {},           # index -> {"summary", "running_summary", "bible"}
            "choices": {},          # index -> choice design
            "branches": {},         # branch_id -> {"nodes": [...], "done": bool}
            "usage": {},
        }

    @classmethod
    def load_or_new(cls, path: Optional[str], source_sha256: str, config_hash: str,
                    *, resume: bool = True) -> tuple["Checkpoint", bool]:
        cp = cls(path, source_sha256, config_hash)
        if not path or not resume or not os.path.exists(path):
            return cp, False
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            return cp, False
        if data.get("source_sha256") != source_sha256 or data.get("config_hash") != config_hash:
            return cp, False
        cp.state = data
        return cp, True

    def save(self) -> None:
        if not self.path:
            return
        with self.lock:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.state, f, ensure_ascii=False)
            os.replace(tmp, self.path)

    def remove(self) -> None:
        if self.path and os.path.exists(self.path):
            try:
                os.remove(self.path)
            except OSError:
                pass

    # convenience -------------------------------------------------------------
    def progress_summary(self) -> str:
        s = self.state
        return (f"{len(s['scenes'])} scenes analysed, {len(s['choices'])} choice points, "
                f"{sum(1 for b in s['branches'].values() if b.get('done'))} branches done")
