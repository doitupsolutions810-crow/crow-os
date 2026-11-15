"""Ollama client (stdlib only).

Defaults follow the Crow convention from the audit: local Ollama at
127.0.0.1:11434. Model is configuration, never hard-coded trust:
CROW_OS_MODEL / --model. Default: qwen2.5-coder:32b (the largest
qwen2.5-coder tag); qwen3-coder:480b works by setting the model name.
"""
from __future__ import annotations

import ipaddress
import json
import os
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
            with _opener(self.base_url).open(req, timeout=self.timeout) as r:
                out = json.loads(r.read())
        except urllib.error.HTTPError as e:
            raise LLMUnavailable(f"ollama at {self.base_url} refused model {self.model!r}: "
                                 f"{_http_error_text(e)}") from e
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise LLMUnavailable(f"ollama unreachable at {self.base_url}: {e}") from e
        if not isinstance(out, dict):
            raise LLMUnavailable(f"ollama returned {type(out).__name__}, not an object")
        if isinstance(out.get("error"), str):
            raise LLMUnavailable(f"ollama error for model {self.model!r}: {out['error'][:300]}")
        return out

    def health(self) -> dict:
        try:
            req = urllib.request.Request(self.base_url + "/api/tags")
            with _opener(self.base_url).open(req, timeout=5) as r:
                tags = json.loads(r.read())
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise LLMUnavailable(f"ollama unreachable at {self.base_url}: {e}") from e
        models = tags.get("models") if isinstance(tags, dict) else None
        names = [m["name"] for m in models if isinstance(m, dict) and isinstance(m.get("name"), str)] \
            if isinstance(models, list) else []
        return {"ok": True, "model_present": any(same_model(n, self.model) for n in names),
                "models": names}

    def chat_stream(self, system: str, user: str, on_token=None) -> str:
        """Streamed chat: Ollama NDJSON tokens are passed to on_token as
        they arrive (the Home Screen renders them live); the full text
        is returned and held to the same fail-closed rules as chat()."""
        self.calls += 1
        req = urllib.request.Request(
            self.base_url + "/api/chat",
            data=json.dumps({
                "model": self.model, "stream": True,
                "messages": [{"role": "system", "content": system},
                             {"role": "user", "content": user}],
            }).encode(),
            headers={"Content-Type": "application/json"})
        parts: list[str] = []
        try:
            with _opener(self.base_url).open(req, timeout=self.timeout) as r:
                for line in r:
                    line = line.strip()
                    if not line:
                        continue
                    obj = json.loads(line)
                    if not isinstance(obj, dict):
                        raise LLMUnavailable(f"ollama stream sent {type(obj).__name__}, not an object")
                    if isinstance(obj.get("error"), str):  # mid-stream failure (OOM, model unloaded)
                        raise LLMUnavailable(f"ollama error for model {self.model!r}: {obj['error'][:300]}")
                    msg = obj.get("message")
                    piece = msg.get("content", "") if isinstance(msg, dict) else ""
                    if isinstance(piece, str) and piece:
                        parts.append(piece)
                        if on_token is not None:
                            on_token(piece)
                    if obj.get("done") is True:
                        break
        except urllib.error.HTTPError as e:
            raise LLMUnavailable(f"ollama at {self.base_url} refused model {self.model!r}: "
                                 f"{_http_error_text(e)}") from e
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise LLMUnavailable(f"ollama unreachable at {self.base_url}: {e}") from e
        content = "".join(parts)
        if not content.strip():
            raise LLMUnavailable("ollama returned an empty streamed response")
        return content

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
        msg = out.get("message")
        content = msg.get("content", "") if isinstance(msg, dict) else ""
        if not isinstance(content, str) or not content.strip():
            raise LLMUnavailable(f"ollama returned a non-text response: {type(content).__name__}")
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
