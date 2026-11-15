"""Ollama client (stdlib only).

Defaults follow the Crow convention from the audit: local Ollama at
127.0.0.1:11434. Model is configuration, never hard-coded trust:
CROW_OS_MODEL / --model. Default: qwen2.5-coder:32b (the largest
qwen2.5-coder tag); qwen3-coder:480b works by setting the model name.
"""
from __future__ import annotations

import ipaddress
import json
import math
import os
import time
import urllib.error
import urllib.parse
import urllib.request

def _base_from_env(raw: str | None) -> str:
    """OLLAMA_HOST as Ollama itself accepts it: "host:port", "0.0.0.0", or a
    full URL. A bare "127.0.0.1:11434" used to become an invalid URL."""
    raw = (raw or "").strip().strip('"').strip("'")
    if not raw:
        return "http://127.0.0.1:11434"
    if "://" not in raw:
        raw = "http://" + raw
    u = urllib.parse.urlsplit(raw)
    host = u.hostname or "127.0.0.1"
    if host in ("0.0.0.0", "::"):  # a bind-all address is not a place to connect to
        host = "127.0.0.1"
    try:
        port = u.port or 11434
    except ValueError:
        port = 11434
    if ":" in host:
        host = f"[{host}]"
    return f"{u.scheme or 'http'}://{host}:{port}"


DEFAULT_BASE = _base_from_env(os.environ.get("OLLAMA_HOST"))
MODEL_FROM_ENV = bool((os.environ.get("CROW_OS_MODEL") or "").strip())
DEFAULT_MODEL = (os.environ.get("CROW_OS_MODEL") or "").strip() or "qwen2.5-coder:32b"
# Preferred local models when the default is not pulled, best first (by family prefix).
PREFERRED = ("qwen2.5-coder", "qwen3-coder", "qwen2.5", "qwen3", "llama3.1", "llama3.2",
             "gemma3", "mistral", "phi")
_NOT_CHAT = ("embed", "bge-", "minilm", "rerank")
MAX_TAGS_BYTES = 512 * 1024
MAX_REPLY_BYTES = 2 * 1024 * 1024
MAX_STREAM_BYTES = 8 * 1024 * 1024
MAX_FRAME_BYTES = 1024 * 1024
MAX_PROMPT_BYTES = 2 * 1024 * 1024


def is_loopback(base_url: str) -> bool:
    host = (urllib.parse.urlsplit(base_url).hostname or "").lower()
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _opener(base_url: str) -> urllib.request.OpenerDirector:
    """Local Ollama never goes through HTTP_PROXY/HTTPS_PROXY. With a proxy in the
    environment and no NO_PROXY, every call to 127.0.0.1 was sent to the proxy and
    came back 404/403, so a working local Ollama looked 'unreachable'."""
    if is_loopback(base_url):
        return urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener()


def _http_error_text(e: urllib.error.HTTPError) -> str:
    """Ollama explains failures in the body ({"error": "model 'x' not found"})."""
    try:
        body = e.read(4096)
        msg = json.loads(body).get("error")
        if isinstance(msg, str) and msg.strip():
            return f"HTTP {e.code}: {msg.strip()[:300]}"
    except Exception:
        pass
    return f"HTTP {e.code}: {e.reason}"


def same_model(a: str, b: str) -> bool:
    """'qwen2.5' and 'qwen2.5:latest' name the same model."""
    norm = lambda m: m if ":" in m else m + ":latest"  # noqa: E731
    return norm(a.strip()) == norm(b.strip())


def pick_local_model(installed: list[str], wanted: str = DEFAULT_MODEL) -> str | None:
    """The installed tag for `wanted`, else the best installed chat model."""
    for name in installed:
        if same_model(name, wanted):
            return name
    chat = [m for m in installed if m and not any(t in m.lower() for t in _NOT_CHAT)]
    for fam in PREFERRED:
        hits = sorted(m for m in chat if m.lower().split(":")[0] == fam)
        if hits:
            return hits[0]
    return sorted(chat)[0] if chat else None


class LLMUnavailable(Exception):
    pass


def _message_text(value):
    if not isinstance(value, str):
        raise LLMUnavailable("ollama returned non-text message content")
    try:
        value.encode("utf-8")
    except UnicodeError as e:
        raise LLMUnavailable("ollama returned invalid Unicode") from e
    if "\x00" in value:
        raise LLMUnavailable("ollama returned a NUL byte")
    return value


class OllamaClient:
    def __init__(self, base_url: str = DEFAULT_BASE, model: str = DEFAULT_MODEL,
                 timeout: float = 120.0):
        if not isinstance(base_url, str) or not base_url.strip() or len(base_url) > 2048:
            raise ValueError("invalid Ollama endpoint")
        raw = base_url.strip()
        if "://" not in raw:
            raw = _base_from_env(raw)
        u = urllib.parse.urlsplit(raw)
        try:
            port = u.port
        except ValueError as e:
            raise ValueError("invalid Ollama endpoint port") from e
        if (u.scheme not in ("http", "https") or not u.hostname or
                u.username is not None or u.password is not None or
                u.path not in ("", "/") or u.query or u.fragment or port == 0 or
                any(ord(c) < 33 or ord(c) == 127 for c in raw)):
            raise ValueError("Ollama endpoint must be a bare http(s) host and optional port")
        if not isinstance(model, str) or not model.strip() or len(model) > 200:
            raise ValueError("invalid Ollama model name")
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool):
            raise ValueError("Ollama timeout must be finite and positive")
        t = float(timeout)
        if not math.isfinite(t) or t <= 0:
            raise ValueError("Ollama timeout must be finite and positive")
        self.base_url = raw.rstrip("/")
        self.model = model.strip()
        self.timeout = min(t, 900.0)
        self.calls = 0

    def _post(self, path: str, payload: dict) -> dict:
        try:
            data = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
            if len(data) > MAX_PROMPT_BYTES:
                raise LLMUnavailable("ollama request exceeds byte budget")
            req = urllib.request.Request(
                self.base_url + path, data=data,
                headers={"Content-Type": "application/json"})
            with _opener(self.base_url).open(req, timeout=self.timeout) as r:
                raw = r.read(MAX_REPLY_BYTES + 1)
            if len(raw) > MAX_REPLY_BYTES:
                raise LLMUnavailable("ollama response exceeds byte budget")
            out = json.loads(raw)
        except LLMUnavailable:
            raise
        except urllib.error.HTTPError as e:
            raise LLMUnavailable(f"ollama at {self.base_url} refused model {self.model!r}: "
                                 f"{_http_error_text(e)}") from e
        except (urllib.error.URLError, OSError, ValueError, TypeError,
                UnicodeError, RecursionError) as e:
            raise LLMUnavailable(f"ollama unreachable at {self.base_url}: {e}") from e
        if not isinstance(out, dict):
            raise LLMUnavailable(f"ollama returned {type(out).__name__}, not an object")
        if isinstance(out.get("error"), str):
            raise LLMUnavailable(f"ollama error for model {self.model!r}: {out['error'][:300]}")
        return out

    def health(self) -> dict:
        try:
            req = urllib.request.Request(self.base_url + "/api/tags")
            with _opener(self.base_url).open(req, timeout=min(5, self.timeout)) as r:
                raw = r.read(MAX_TAGS_BYTES + 1)
            if len(raw) > MAX_TAGS_BYTES:
                raise LLMUnavailable("ollama model listing exceeds byte budget")
            tags = json.loads(raw)
        except LLMUnavailable:
            raise
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise LLMUnavailable(f"ollama unreachable at {self.base_url}: {e}") from e
        models = tags.get("models") if isinstance(tags, dict) else None
        names = [m["name"] for m in models if isinstance(m, dict) and isinstance(m.get("name"), str)] \
            if isinstance(models, list) else []
        return {"ok": True, "model_present": any(same_model(n, self.model) for n in names),
                "models": names}

    def chat_stream(self, system: str, user: str, on_token=None) -> str:
        """Read bounded Ollama NDJSON; incomplete explicit streams fail closed.

        Legacy Crow OS stubs with one JSON message and no done field remain
        compatible. Ignore trailing wire data after a valid done frame, as
        required by existing Crow OS v0.9 streaming fixtures.
        """
        self.calls += 1
        try:
            data = json.dumps({"model": self.model, "stream": True,
                "messages": [{"role": "system", "content": system},
                             {"role": "user", "content": user}]},
                ensure_ascii=False, allow_nan=False).encode("utf-8")
            if len(data) > MAX_PROMPT_BYTES:
                raise LLMUnavailable("ollama request exceeds byte budget")
            req = urllib.request.Request(
                self.base_url + "/api/chat", data=data,
                headers={"Content-Type": "application/json"})
            parts = []
            total = frames = 0
            explicit_stream = saw_done = False
            started = time.monotonic()
            with _opener(self.base_url).open(req, timeout=self.timeout) as r:
                while True:
                    line = r.readline(MAX_FRAME_BYTES + 1)
                    if not line:
                        break
                    total += len(line)
                    if len(line) > MAX_FRAME_BYTES or total > MAX_STREAM_BYTES:
                        raise LLMUnavailable("ollama stream exceeds byte budget")
                    if time.monotonic() - started > self.timeout:
                        raise LLMUnavailable("ollama stream exceeded deadline")
                    if not line.strip():
                        continue
                    frames += 1
                    try:
                        obj = json.loads(line)
                    except (ValueError, UnicodeError, RecursionError) as e:
                        raise LLMUnavailable("ollama stream returned invalid JSON") from e
                    if not isinstance(obj, dict):
                        raise LLMUnavailable("ollama stream frame must be an object")
                    if isinstance(obj.get("error"), str):
                        raise LLMUnavailable(f"ollama error for model {self.model!r}: {obj['error'][:300]}")
                    msg = obj.get("message")
                    if msg is not None and not isinstance(msg, dict):
                        raise LLMUnavailable("ollama stream message must be an object")
                    if msg is not None:
                        piece = _message_text(msg.get("content", ""))
                        if piece:
                            parts.append(piece)
                            if on_token is not None:
                                try:
                                    on_token(piece)
                                except Exception as e:
                                    raise LLMUnavailable("ollama stream consumer failed") from e
                    done = obj.get("done")
                    if done is True:
                        if obj.get("done_reason") in ("length", "error"):
                            raise LLMUnavailable("ollama stream stopped before completion")
                        saw_done = True
                        break
                    if done is False:
                        explicit_stream = True
                    elif done is not None:
                        raise LLMUnavailable("ollama stream done flag must be boolean")
            if not saw_done and (explicit_stream or frames != 1):
                raise LLMUnavailable("ollama stream ended without terminal frame")
            content = "".join(parts)
            if not content.strip():
                raise LLMUnavailable("ollama returned an empty streamed response")
            return content
        except LLMUnavailable:
            raise
        except urllib.error.HTTPError as e:
            raise LLMUnavailable(f"ollama at {self.base_url} refused model {self.model!r}: "
                                 f"{_http_error_text(e)}") from e
        except (urllib.error.URLError, OSError, ValueError, TypeError, UnicodeError) as e:
            raise LLMUnavailable(f"ollama unreachable at {self.base_url}: {e}") from e

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
        if out.get("done") is False or out.get("done_reason") in ("length", "error"):
            raise LLMUnavailable("ollama did not finish its response")
        msg = out.get("message")
        content = _message_text(msg.get("content", "") if isinstance(msg, dict) else "")
        if not content.strip():
            raise LLMUnavailable("ollama returned an empty response")
        return content


def resolve_model(explicit: str | None, base_url: str = DEFAULT_BASE) -> str:
    """The model a run should use. An explicit choice (argument, agent setting or
    CROW_OS_MODEL) is honored as-is and fails closed if missing. Only the built-in
    default is swapped for an installed local model, so a fresh box with, say,
    qwen2.5:3b pulled runs instead of failing every job on qwen2.5-coder:32b."""
    if explicit and explicit.strip():
        return explicit.strip()
    if MODEL_FROM_ENV:
        return DEFAULT_MODEL
    try:
        names = OllamaClient(base_url=base_url, model=DEFAULT_MODEL).health()["models"]
    except LLMUnavailable:
        return DEFAULT_MODEL  # the run fails closed with the real reason
    return pick_local_model(names) or DEFAULT_MODEL
