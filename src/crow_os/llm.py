"""Ollama client (stdlib only).

Defaults follow the Crow convention from the audit: local Ollama at
127.0.0.1:11434. Model is configuration, never hard-coded trust:
CROW_OS_MODEL / --model. Default: qwen2.5-coder:32b (the largest
qwen2.5-coder tag); qwen3-coder:480b works by setting the model name.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

DEFAULT_BASE = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")
DEFAULT_MODEL = os.environ.get("CROW_OS_MODEL", "qwen2.5-coder:32b")


class LLMUnavailable(Exception):
    pass


class OllamaClient:
    def __init__(self, base_url: str = DEFAULT_BASE, model: str = DEFAULT_MODEL,
                 timeout: float = 120.0):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.calls = 0

    def _post(self, path: str, payload: dict) -> dict:
        req = urllib.request.Request(
            self.base_url + path,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read())
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
            raise LLMUnavailable(f"ollama unreachable at {self.base_url}: {e}") from e

    def health(self) -> dict:
        try:
            req = urllib.request.Request(self.base_url + "/api/tags")
            with urllib.request.urlopen(req, timeout=5) as r:
                tags = json.loads(r.read())
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
            raise LLMUnavailable(f"ollama unreachable at {self.base_url}: {e}") from e
        names = [m.get("name", "") for m in tags.get("models", [])]
        return {"ok": True, "model_present": self.model in names, "models": names}

    def chat(self, system: str, user: str) -> str:
        self.calls += 1
        out = self._post("/api/chat", {
            "model": self.model,
            "stream": False,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        })
        content = (out.get("message") or {}).get("content", "")
        if not content.strip():
            raise LLMUnavailable("ollama returned an empty response")
        return content
