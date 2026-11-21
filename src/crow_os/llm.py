"""Ollama client (stdlib only) with local model discovery and bounded parsing."""
from __future__ import annotations

import ipaddress
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 11434
MODEL_FROM_ENV = bool(os.environ.get("CROW_OS_MODEL", "").strip())


def _base_from_env(value: str | None) -> str:
    raw = str(value or "").strip().strip("'").strip('"').strip()
    if not raw:
        return f"http://{DEFAULT_HOST}:{DEFAULT_PORT}"
    if "://" not in raw:
        raw = "http://" + raw
    try:
        parts = urllib.parse.urlsplit(raw)
        if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
            return f"http://{DEFAULT_HOST}:{DEFAULT_PORT}"
        host = parts.hostname.strip("[]")
        try:
            port = parts.port
        except ValueError:
            port = None
        if host in {"0.0.0.0", "::"}:
            host = DEFAULT_HOST if ":" not in host else "::1"
        rendered_host = f"[{host}]" if ":" in host else host
        rendered_port = port or DEFAULT_PORT
        path = parts.path.rstrip("/")
        return urllib.parse.urlunsplit(
            (parts.scheme.lower(), f"{rendered_host}:{rendered_port}", path, "", "")
        )
    except (TypeError, ValueError):
        return f"http://{DEFAULT_HOST}:{DEFAULT_PORT}"


def is_loopback(url: str) -> bool:
    try:
        host = urllib.parse.urlsplit(str(url)).hostname
        if not host:
            return False
        normalized = host.rstrip(".").lower()
        if normalized == "localhost" or normalized.endswith(".localhost"):
            return True
        return ipaddress.ip_address(normalized.split("%", 1)[0]).is_loopback
    except (ValueError, TypeError):
        return False


DEFAULT_BASE = _base_from_env(os.environ.get("OLLAMA_HOST"))
DEFAULT_MODEL = (os.environ.get("CROW_OS_MODEL") or "qwen2.5-coder:32b").strip()
DEFAULT_MODEL = DEFAULT_MODEL or "qwen2.5-coder:32b"


class LLMUnavailable(Exception):
    pass


def same_model(left: str, right: str) -> bool:
    def normalize(name: str) -> str:
        value = str(name or "").strip()
        if value.endswith(":latest"):
            value = value[:-7]
        return value
    return bool(normalize(left)) and normalize(left) == normalize(right)


def pick_local_model(models: list[str], preferred: str | None = None) -> str | None:
    names = []
    for item in models if isinstance(models, list) else []:
        if not isinstance(item, str):
            continue
        name = item.strip()
        if not name or "embed" in name.lower():
            continue
        if name not in names:
            names.append(name)
    if preferred:
        selected = next((name for name in names if same_model(name, preferred)), None)
        if selected:
            return selected
    # Prefer a model family that is generally capable at structured tool use,
    # then broad general-purpose local families, then any remaining model.
    for prefix in ("qwen", "llama", "mistral", "deepseek", "gemma", "phi"):
        selected = next((name for name in names if name.lower().startswith(prefix)), None)
        if selected:
            return selected
    return names[0] if names else None


def resolve_model(model: str | None = None, base_url: str | None = None) -> str:
    if model is not None and str(model).strip():
        candidate = str(model).strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}", candidate):
            raise LLMUnavailable("model name is invalid")
        return candidate
    if MODEL_FROM_ENV:
        return DEFAULT_MODEL
    try:
        health = OllamaClient(base_url=base_url).health()
        selected = pick_local_model(health.get("models") or [])
        return selected or DEFAULT_MODEL
    except LLMUnavailable:
        return DEFAULT_MODEL


class OllamaClient:
    def __init__(self, base_url: str | None = None, model: str | None = None,
                 timeout: float = 120.0):
        self.base_url = _base_from_env(base_url if base_url is not None else DEFAULT_BASE)
        self.model = str(model if model is not None else DEFAULT_MODEL).strip() or DEFAULT_MODEL
        self.timeout = timeout
        self.calls = 0

    def _open(self, request: urllib.request.Request, timeout: float | None = None):
        # Local runtimes must never be sent through HTTP_PROXY / HTTPS_PROXY.
        if is_loopback(self.base_url):
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            return opener.open(request, timeout=timeout or self.timeout)
        return urllib.request.urlopen(request, timeout=timeout or self.timeout)

    @staticmethod
    def _body_error(raw: bytes, status: int | None = None) -> str:
        detail = ""
        try:
            value = json.loads(raw or b"{}")
            if isinstance(value, dict):
                error = value.get("error")
                if isinstance(error, str):
                    detail = error.strip()
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
            detail = ""
        if detail:
            return detail[:1000]
        if status == 404:
            return "model or endpoint not found (HTTP 404)"
        return (raw or b"").decode("utf-8", "replace").strip()[:500] or (
            f"HTTP {status}" if status else "empty error response"
        )

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        req = urllib.request.Request(
            self.base_url + path,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        try:
            with self._open(req) as response:
                raw = response.read(1_048_577)
            if len(raw) > 1_048_576:
                raise LLMUnavailable("ollama response exceeded 1 MiB")
            result = json.loads(raw)
        except urllib.error.HTTPError as error:
            detail = self._body_error(error.read(8192), error.code)
            raise LLMUnavailable(f"ollama request failed: {detail}") from None
        except urllib.error.URLError as error:
            raise LLMUnavailable(f"ollama unreachable at {self.base_url}: {error.reason}") from None
        except (OSError, json.JSONDecodeError, UnicodeDecodeError, ValueError) as error:
            raise LLMUnavailable(f"invalid ollama response: {type(error).__name__}") from None
        if not isinstance(result, dict):
            raise LLMUnavailable("ollama returned a non-object JSON response")
        if isinstance(result.get("error"), str) and result["error"].strip():
            raise LLMUnavailable(result["error"].strip()[:1000])
        return result

    def health(self) -> dict[str, Any]:
        req = urllib.request.Request(self.base_url + "/api/tags")
        try:
            with self._open(req, timeout=min(self.timeout, 5.0)) as response:
                raw = response.read(1_048_577)
            if len(raw) > 1_048_576:
                raise LLMUnavailable("ollama tags response exceeded 1 MiB")
            tags = json.loads(raw)
        except urllib.error.HTTPError as error:
            detail = self._body_error(error.read(8192), error.code)
            raise LLMUnavailable(f"ollama health check failed: {detail}") from None
        except urllib.error.URLError as error:
            raise LLMUnavailable(f"ollama unreachable at {self.base_url}: {error.reason}") from None
        except (OSError, json.JSONDecodeError, UnicodeDecodeError, ValueError) as error:
            raise LLMUnavailable(f"invalid ollama tags response: {type(error).__name__}") from None
        if not isinstance(tags, dict):
            return {"ok": True, "model_present": False, "models": []}
        raw_models = tags.get("models")
        names = []
        if isinstance(raw_models, list):
            for item in raw_models:
                if isinstance(item, str):
                    name = item.strip()
                elif isinstance(item, dict) and isinstance(item.get("name"), str):
                    name = item["name"].strip()
                else:
                    continue
                if name and name not in names:
                    names.append(name)
        present = any(same_model(self.model, name) for name in names)
        return {"ok": True, "model_present": present, "models": names}

    def chat_stream(self, system: str, user: str, on_token=None) -> str:
        """Read bounded NDJSON; reject malformed frames and stop at done=true."""
        self.calls += 1
        req = urllib.request.Request(
            self.base_url + "/api/chat",
            data=json.dumps({
                "model": self.model,
                "stream": True,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            }).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        parts: list[str] = []
        try:
            with self._open(req) as response:
                total = 0
                for line in response:
                    total += len(line)
                    if total > 1_048_576:
                        raise LLMUnavailable("ollama stream exceeded 1 MiB")
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        raise LLMUnavailable("ollama returned a malformed stream frame") from None
                    if not isinstance(obj, dict):
                        raise LLMUnavailable("ollama stream frame must be a JSON object")
                    if isinstance(obj.get("error"), str) and obj["error"].strip():
                        raise LLMUnavailable(obj["error"].strip()[:1000])
                    message = obj.get("message") or {}
                    if not isinstance(message, dict):
                        raise LLMUnavailable("ollama stream message must be an object")
                    piece = message.get("content", "")
                    if piece is not None and not isinstance(piece, str):
                        raise LLMUnavailable("ollama stream content must be text")
                    if piece:
                        parts.append(piece)
                        if on_token is not None:
                            on_token(piece)
                    if obj.get("done") is True:
                        break
        except urllib.error.HTTPError as error:
            detail = self._body_error(error.read(8192), error.code)
            raise LLMUnavailable(f"ollama stream failed: {detail}") from None
        except urllib.error.URLError as error:
            raise LLMUnavailable(f"ollama unreachable at {self.base_url}: {error.reason}") from None
        except LLMUnavailable:
            raise
        except (OSError, ValueError, TypeError) as error:
            raise LLMUnavailable(f"invalid ollama stream: {type(error).__name__}") from None
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
        message = out.get("message")
        if not isinstance(message, dict):
            raise LLMUnavailable("ollama returned a non-object message")
        content = message.get("content", "")
        if not isinstance(content, str) or not content.strip():
            raise LLMUnavailable(f"ollama returned a non-text response: {type(content).__name__}")
        return content
