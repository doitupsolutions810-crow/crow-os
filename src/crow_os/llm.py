"""Bounded, fail-closed stdlib Ollama client for Crow OS local inference.

The model is never an authority: caller-owned capability and governance checks
remain outside this transport. A mockable /api/chat and /api/tags implementation
makes failure paths testable without a real model or outbound network.
"""
from __future__ import annotations

import json
import math
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

DEFAULT_BASE = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")
DEFAULT_MODEL = os.environ.get("CROW_OS_MODEL", "qwen2.5-coder:32b")
MAX_TAGS_BYTES = 512 * 1024
MAX_REPLY_BYTES = 2 * 1024 * 1024
MAX_STREAM_BYTES = 8 * 1024 * 1024
MAX_FRAME_BYTES = 1024 * 1024
MAX_PROMPT_BYTES = 2 * 1024 * 1024


class LLMUnavailable(Exception):
    """Local model transport, protocol, model, or resource-budget failure."""


def _endpoint(base: str) -> str:
    if not isinstance(base, str) or not base.strip() or len(base) > 2048:
        raise ValueError("Ollama endpoint must be a nonempty URL")
    raw = base.strip()
    if "://" not in raw:
        raw = "http://" + raw
    u = urllib.parse.urlsplit(raw)
    try:
        port = u.port
    except ValueError as e:
        raise ValueError("invalid Ollama endpoint port") from e
    if (u.scheme not in ("http", "https") or not u.hostname or
            u.username is not None or u.password is not None or
            u.path not in ("", "/") or u.query or u.fragment or port == 0):
        raise ValueError("Ollama endpoint must be a bare http(s) host and optional port")
    if any(ord(c) < 33 or ord(c) == 127 for c in raw):
        raise ValueError("Ollama endpoint contains invalid characters")
    return raw.rstrip("/")


def _body(response: Any, limit: int) -> dict[str, Any]:
    raw = response.read(limit + 1)
    if len(raw) > limit:
        raise LLMUnavailable("Ollama response exceeds byte budget")
    try:
        obj = json.loads(raw)
    except (ValueError, UnicodeError, RecursionError) as e:
        raise LLMUnavailable("Ollama returned invalid JSON") from e
    if not isinstance(obj, dict):
        raise LLMUnavailable("Ollama response must be a JSON object")
    if obj.get("error"):
        raise LLMUnavailable("Ollama returned an error")
    return obj


def _text(value: Any) -> str:
    if not isinstance(value, str):
        raise LLMUnavailable("Ollama returned non-text message content")
    try:
        value.encode("utf-8")
    except UnicodeError as e:
        raise LLMUnavailable("Ollama returned invalid Unicode") from e
    if "\x00" in value:
        raise LLMUnavailable("Ollama returned a NUL byte")
    return value


class OllamaClient:
    def __init__(self, base_url: str = DEFAULT_BASE, model: str = DEFAULT_MODEL,
                 timeout: float = 120.0):
        self.base_url = _endpoint(base_url)
        if not isinstance(model, str) or not model.strip() or len(model) > 200:
            raise ValueError("Ollama model must be a nonempty model name")
        self.model = model.strip()
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool):
            raise ValueError("Ollama timeout must be finite and positive")
        t = float(timeout)
        if not math.isfinite(t) or t <= 0:
            raise ValueError("Ollama timeout must be finite and positive")
        self.timeout = min(t, 900.0)
        self.calls = 0

    def _request(self, path: str, data: bytes | None = None) -> urllib.request.Request:
        return urllib.request.Request(
            self.base_url + path, data=data,
            headers={"Content-Type": "application/json", "Accept": "application/json"})

    def _post(self, path: str, payload: dict) -> dict:
        try:
            data = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
            if len(data) > MAX_PROMPT_BYTES:
                raise LLMUnavailable("Ollama request exceeds byte budget")
            with urllib.request.urlopen(self._request(path, data), timeout=self.timeout) as r:
                return _body(r, MAX_REPLY_BYTES)
        except LLMUnavailable:
            raise
        except urllib.error.HTTPError as e:
            raise LLMUnavailable(f"Ollama HTTP status {e.code}") from e
        except (urllib.error.URLError, OSError, ValueError, UnicodeError, TypeError) as e:
            raise LLMUnavailable(f"Ollama request failed at {self.base_url}") from e

    def health(self) -> dict:
        try:
            with urllib.request.urlopen(self._request("/api/tags"), timeout=min(5, self.timeout)) as r:
                tags = _body(r, MAX_TAGS_BYTES)
        except LLMUnavailable:
            raise
        except urllib.error.HTTPError as e:
            raise LLMUnavailable(f"Ollama HTTP status {e.code}") from e
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise LLMUnavailable(f"Ollama unreachable at {self.base_url}") from e
        models = tags.get("models")
        if not isinstance(models, list):
            raise LLMUnavailable("Ollama model listing is malformed")
        names = []
        for record in models[:10000]:
            if not isinstance(record, dict):
                continue
            name = record.get("name", record.get("model"))
            if isinstance(name, str) and len(name) <= 200:
                names.append(name)
        return {"ok": True, "model_present": self.model in names, "models": names}

    def chat(self, system: str, user: str) -> str:
        self.calls += 1
        out = self._post("/api/chat", {"model": self.model, "stream": False,
                 "messages": [{"role": "system", "content": system},
                              {"role": "user", "content": user}]})
        if out.get("done") is False or out.get("done_reason") in ("length", "error"):
            raise LLMUnavailable("Ollama did not finish its response")
        message = out.get("message")
        if not isinstance(message, dict):
            raise LLMUnavailable("Ollama response missing message")
        content = _text(message.get("content"))
        if not content.strip():
            raise LLMUnavailable("Ollama returned empty content")
        return content

    def chat_stream(self, system: str, user: str,
                    on_token: Callable[[str], Any] | None = None) -> str:
        """Consume NDJSON with bounded frames and explicit finish enforcement.

        A single legacy JSON chat response without done remains accepted
        for backward compatibility with earlier Crow OS stub fixtures. Any
        streaming response with explicit done: false requires done: true.
        Partial content cannot turn a failed model call into a successful job.
        """
        self.calls += 1
        try:
            data = json.dumps({"model": self.model, "stream": True,
                "messages": [{"role": "system", "content": system},
                             {"role": "user", "content": user}]},
                ensure_ascii=False, allow_nan=False).encode("utf-8")
            if len(data) > MAX_PROMPT_BYTES:
                raise LLMUnavailable("Ollama request exceeds byte budget")
            request = self._request("/api/chat", data)
            parts: list[str] = []
            total, frames = 0, 0
            saw_done, explicit_stream = False, False
            start = time.monotonic()
            with urllib.request.urlopen(request, timeout=self.timeout) as r:
                while True:
                    line = r.readline(MAX_FRAME_BYTES + 1)
                    if not line:
                        break
                    total += len(line)
                    if len(line) > MAX_FRAME_BYTES or total > MAX_STREAM_BYTES:
                        raise LLMUnavailable("Ollama stream exceeds byte budget")
                    if time.monotonic() - start > self.timeout:
                        raise LLMUnavailable("Ollama stream exceeded deadline")
                    if not line.strip():
                        continue
                    frames += 1
                    if saw_done:
                        raise LLMUnavailable("Ollama returned data after terminal frame")
                    try:
                        frame = json.loads(line)
                    except (ValueError, UnicodeError, RecursionError) as e:
                        raise LLMUnavailable("Ollama returned malformed stream JSON") from e
                    if not isinstance(frame, dict):
                        raise LLMUnavailable("Ollama stream frame must be an object")
                    if frame.get("error"):
                        raise LLMUnavailable("Ollama stream reported an error")
                    done = frame.get("done")
                    if done is not None:
                        if not isinstance(done, bool):
                            raise LLMUnavailable("Ollama stream done flag must be boolean")
                        explicit_stream = True
                    message = frame.get("message")
                    if message is not None:
                        if not isinstance(message, dict):
                            raise LLMUnavailable("Ollama stream message must be an object")
                        piece = _text(message.get("content", ""))
                        if piece:
                            parts.append(piece)
                            if on_token is not None:
                                try:
                                    on_token(piece)
                                except Exception as e:
                                    raise LLMUnavailable("Ollama stream consumer failed") from e
                    if done is True:
                        if frame.get("done_reason") in ("length", "error"):
                            raise LLMUnavailable("Ollama stream stopped before completion")
                        saw_done = True
                if not saw_done and (explicit_stream or frames != 1):
                    raise LLMUnavailable("Ollama stream ended without terminal frame")
            result = "".join(parts)
            if not result.strip():
                raise LLMUnavailable("Ollama returned an empty streamed response")
            return result
        except LLMUnavailable:
            raise
        except urllib.error.HTTPError as e:
            raise LLMUnavailable(f"Ollama HTTP status {e.code}") from e
        except (urllib.error.URLError, OSError, ValueError, UnicodeError, TypeError) as e:
            raise LLMUnavailable(f"Ollama stream failed at {self.base_url}") from e
