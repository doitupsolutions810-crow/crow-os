"""Ollama HTTP client and installed-model selection (stdlib only).

An explicit model stays explicit. Without one, generation-capable installed
models may be selected; unavailable health checks never invent a provider reply.
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request


class LLMUnavailable(Exception):
    pass


def _base_from_env(value: str | None) -> str:
    """Normalize Ollama's host:port/bind-address setting for client use."""
    raw = (value or "127.0.0.1:11434").strip().strip("\"'").strip() or "127.0.0.1:11434"
    if "://" not in raw:
        raw = "http://" + raw
    try:
        parsed = urllib.parse.urlsplit(raw)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError("HTTP(S) host required")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("credentials in the URL are not supported")
        if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
            raise ValueError("Ollama base must be an origin")
        host = parsed.hostname
        try:
            port = parsed.port or 11434
        except ValueError:
            port = 11434
        if host == "0.0.0.0":
            host = "127.0.0.1"
        elif host == "::":
            host = "::1"
        rendered = "[" + host + "]" if ":" in host else host
        return f"{parsed.scheme}://{rendered}:{port}"
    except (ValueError, AttributeError) as e:
        raise LLMUnavailable("invalid Ollama host configuration") from e


def is_loopback(url: str) -> bool:
    try:
        host = urllib.parse.urlsplit(url).hostname
        if host is None:
            return False
        if host.lower().rstrip(".") == "localhost":
            return True
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


DEFAULT_BASE = _base_from_env(os.environ.get("OLLAMA_HOST"))
MODEL_FROM_ENV = bool(os.environ.get("CROW_OS_MODEL", "").strip())
DEFAULT_MODEL = os.environ.get("CROW_OS_MODEL", "").strip() or "qwen2.5-coder:32b"
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
_MODEL = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._:/-]{0,255}\Z")


def same_model(left: str, right: str) -> bool:
    def canonical(name):
        if not isinstance(name, str):
            return None
        name = name.strip()
        if not name:
            return None
        # A registry port can contain ':'; only the final component is a tag.
        return name if ":" in name.rsplit("/", 1)[-1] else name + ":latest"
    a, b = canonical(left), canonical(right)
    return a is not None and a == b


def _valid_model(value) -> bool:
    return isinstance(value, str) and bool(_MODEL.fullmatch(value))


def pick_local_model(installed: list[str], preferred: str | None = None) -> str | None:
    """Choose a real generation model; embedding models cannot serve chat."""
    names = []
    for name in installed if isinstance(installed, list) else []:
        if not _valid_model(name) or name in names:
            continue
        family = name.rsplit("/", 1)[-1].split(":", 1)[0].lower()
        if "embed" in family or family.startswith(("bge", "e5-")):
            continue
        names.append(name)
    if preferred:
        for name in names:
            if same_model(name, preferred):
                return name
    families = ("qwen", "llama", "mistral", "gemma", "phi", "deepseek")
    def rank(name):
        family = name.rsplit("/", 1)[-1].lower()
        return next((i for i, prefix in enumerate(families) if family.startswith(prefix)), len(families))
    return min(names, key=rank) if names else None


def resolve_model(model: str | None = None, base_url: str | None = None) -> str:
    if isinstance(model, str) and model.strip():
        return model.strip()
    if model is not None and not isinstance(model, str):
        raise LLMUnavailable("model must be a name")
    if MODEL_FROM_ENV:
        return DEFAULT_MODEL
    try:
        installed = OllamaClient(base_url=base_url or DEFAULT_BASE).health()["models"]
        return pick_local_model(installed, DEFAULT_MODEL) or DEFAULT_MODEL
    except LLMUnavailable:
        # The worker must still record its durable failed job when a host is
        # offline. Returning configuration does not substitute any model output.
        return DEFAULT_MODEL


def _object(value, label: str) -> dict:
    if not isinstance(value, dict):
        raise LLMUnavailable(f"ollama returned a non-object {label}")
    if value.get("error"):
        raise LLMUnavailable("ollama: " + str(value["error"])[:4000])
    return value


def _content(value: dict, *, optional=False) -> str:
    message = value.get("message")
    if message is None and optional:
        return ""
    if not isinstance(message, dict):
        raise LLMUnavailable("ollama returned a non-object message")
    content = message.get("content", "")
    if not isinstance(content, str):
        raise LLMUnavailable("ollama returned a non-text response")
    return content


class OllamaClient:
    def __init__(self, base_url: str = DEFAULT_BASE, model: str = DEFAULT_MODEL,
                 timeout: float = 120.0):
        self.base_url = _base_from_env(base_url)
        self.model = model
        self.timeout = timeout
        self.calls = 0
        # Proxy routing of the OS's own loopback model endpoint both breaks local
        # availability and can disclose prompts. Remote providers retain the
        # environment's normal proxy/TLS handling.
        self._opener = (urllib.request.build_opener(urllib.request.ProxyHandler({}))
                        if is_loopback(self.base_url) else urllib.request.build_opener())

    def _open(self, request, timeout=None):
        try:
            return self._opener.open(request, timeout=self.timeout if timeout is None else timeout)
        except urllib.error.HTTPError as e:
            try:
                raw = e.read(32768)
                data = json.loads(raw)
                error = data.get("error") if isinstance(data, dict) else None
            except (OSError, ValueError):
                error = None
            finally:
                e.close()
            detail = str(error)[:4000] if error else f"HTTP {e.code} {e.reason}"
            raise LLMUnavailable("ollama: " + detail) from e
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise LLMUnavailable(f"ollama unreachable at {self.base_url}: {e}") from e

    def _json_response(self, response, label: str) -> dict:
        try:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise LLMUnavailable("ollama response exceeded the size limit")
            return _object(json.loads(raw), label)
        except (OSError, ValueError) as e:
            raise LLMUnavailable("ollama returned invalid JSON") from e

    def _post(self, path: str, payload: dict) -> dict:
        req = urllib.request.Request(
            self.base_url + path, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"})
        with self._open(req) as response:
            return self._json_response(response, "response")

    def health(self) -> dict:
        req = urllib.request.Request(self.base_url + "/api/tags")
        with self._open(req, timeout=5) as response:
            tags = self._json_response(response, "tags")
        models = tags.get("models", [])
        names = []
        for item in models if isinstance(models, list) else []:
            name = item.get("name") if isinstance(item, dict) else None
            if _valid_model(name) and name not in names:
                names.append(name)
        return {"ok": True, "model_present": any(same_model(self.model, name) for name in names),
                "models": names}

    def chat_stream(self, system: str, user: str, on_token=None) -> str:
        self.calls += 1
        req = urllib.request.Request(
            self.base_url + "/api/chat", data=json.dumps({
                "model": self.model, "stream": True,
                "messages": [{"role": "system", "content": system},
                             {"role": "user", "content": user}],
            }).encode(), headers={"Content-Type": "application/json"})
        parts, total, frames, done = [], 0, 0, False
        try:
            with self._open(req) as response:
                native_stream = "ndjson" in response.headers.get("Content-Type", "").lower()
                legacy_complete = False
                while True:
                    line = response.readline(MAX_RESPONSE_BYTES + 1)
                    if not line:
                        break
                    total += len(line)
                    if total > MAX_RESPONSE_BYTES:
                        raise LLMUnavailable("ollama response exceeded the size limit")
                    if not line.strip():
                        continue
                    obj = _object(json.loads(line), "stream frame")
                    frames += 1
                    if "done" in obj and not isinstance(obj["done"], bool):
                        raise LLMUnavailable("ollama returned an invalid completion marker")
                    legacy_complete = "done" not in obj
                    piece = _content(obj, optional=obj.get("done") is True)
                    if piece:
                        parts.append(piece)
                        if on_token is not None:
                            on_token(piece)
                    if obj.get("done") is True:
                        done = True
                        break
                # Native Ollama NDJSON has an explicit terminal frame. Retain
                # support for a finite legacy JSON response (one complete object).
                if not done and (native_stream or frames != 1 or not legacy_complete):
                    raise LLMUnavailable("ollama stream ended before completion")
        except (OSError, ValueError) as e:
            raise LLMUnavailable("ollama stream failed: " + str(e)) from e
        content = "".join(parts)
        if not content.strip():
            raise LLMUnavailable("ollama returned an empty streamed response")
        return content

    def chat(self, system: str, user: str) -> str:
        self.calls += 1
        out = self._post("/api/chat", {
            "model": self.model, "stream": False,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
        })
        if "done" in out and not isinstance(out["done"], bool):
            raise LLMUnavailable("ollama returned an invalid completion marker")
        if out.get("done") is False:
            raise LLMUnavailable("ollama response is incomplete")
        content = _content(out)
        if not content.strip():
            raise LLMUnavailable("ollama returned an empty response")
        return content
