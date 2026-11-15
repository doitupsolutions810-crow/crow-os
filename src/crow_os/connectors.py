"""Connectors (v0.16) — the Connect-apps catalog grows real wiring.

A connector is a configured external service with three honest
states, checked live:

- not configured: no endpoint/token on this home. The catalog keeps
  showing it as user-asserted only — never "connected".
- configured, unreachable: settings exist but the live check fails.
- live: the check just succeeded against the real endpoint.

Secrets never travel through chat, the API, CrowFS, or the ledger.
A token comes from the environment (CROW_OS_GITHUB_TOKEN) or a
0600 file in the home directory (secrets/github.token) that only
the operator places there. Tool calls are capability-gated and
attested exactly like the built-in tools; results are data excerpts,
and the token is never part of a result, a log, or an attestation.

The framework ships one real connector — GitHub (read-only:
whoami + repository listing) — against https://api.github.com by
default. The base URL is configurable per home, which is how the
test-suite proves the whole path against a local mock of the same
API: identical code, only the endpoint differs.
"""
from __future__ import annotations

import json
import os
import time
import urllib.request
from pathlib import Path
from typing import Any

from .fs import CrowFS, FSError

CONFIG_FILE = "/system/connectors.json"
CHECK_TTL = 30.0  # seconds to cache a live check

# connector id -> the tools it exposes to agents
CONNECTOR_TOOLS: dict[str, list[str]] = {
    "github": ["github.repos", "github.whoami"],
}


class ConnectorError(Exception):
    pass


class ConnectorRegistry:
    def __init__(self, fs: CrowFS, home: Path):
        self.fs = fs
        self.home = home
        self._checks: dict[str, tuple[float, bool, str]] = {}

    # -- configuration ---------------------------------------------------
    def _config(self) -> dict[str, Any]:
        try:
            data = json.loads(self.fs.read_file(CONFIG_FILE))
            return data if isinstance(data, dict) else {}
        except (FSError, json.JSONDecodeError):
            return {}

    def configure(self, connector_id: str, base_url: str) -> dict[str, Any]:
        if connector_id not in CONNECTOR_TOOLS:
            raise ConnectorError(f"unknown connector: {connector_id}")
        if not isinstance(base_url, str) or not base_url.startswith(("http://", "https://")):
            raise ConnectorError("base_url must be http(s)")
        config = self._config()
        config[connector_id] = {"base_url": base_url.rstrip("/")}
        self.fs.write_file(CONFIG_FILE, json.dumps(config, indent=2))
        self._checks.pop(connector_id, None)
        return self.status(connector_id)

    def base_url(self, connector_id: str) -> str | None:
        entry = self._config().get(connector_id) or {}
        return entry.get("base_url")

    def _token(self, connector_id: str) -> str | None:
        env = os.environ.get(f"CROW_OS_{connector_id.upper()}_TOKEN")
        if env:
            return env.strip()
        secret = self.home / "secrets" / f"{connector_id}.token"
        try:
            text = secret.read_text().strip()
            return text or None
        except OSError:
            return None

    # -- live state ---------------------------------------------------------
    def status(self, connector_id: str) -> dict[str, Any]:
        if connector_id not in CONNECTOR_TOOLS:
            raise ConnectorError(f"unknown connector: {connector_id}")
        base = self.base_url(connector_id)
        token = self._token(connector_id)
        if not base and not token:
            return {"id": connector_id, "state": "not_configured",
                    "tools": CONNECTOR_TOOLS[connector_id]}
        live, detail = self._check(connector_id, base or "https://api.github.com",
                                   token)
        return {"id": connector_id,
                "state": "live" if live else "unreachable",
                "detail": detail, "tools": CONNECTOR_TOOLS[connector_id]}

    def _check(self, connector_id: str, base: str,
               token: str | None) -> tuple[bool, str]:
        now = time.time()
        cached = self._checks.get(connector_id)
        if cached and now - cached[0] < CHECK_TTL:
            return cached[1], cached[2]
        try:
            data = self._get(connector_id, base, "/user", token)
            result = (True, f"@{data.get('login', '?')}")
        except ConnectorError as e:
            result = (False, str(e))
        self._checks[connector_id] = (now, result[0], result[1])
        return result

    def all_states(self) -> dict[str, str]:
        return {cid: self.status(cid)["state"] for cid in CONNECTOR_TOOLS}

    def tool_names(self) -> list[str]:
        return [t for tools in CONNECTOR_TOOLS.values() for t in tools]

    # -- tool execution -------------------------------------------------------
    def _get(self, connector_id: str, base: str, path: str,
             token: str | None) -> Any:
        req = urllib.request.Request(base + path, method="GET", headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "crow-os",
        })
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            raise ConnectorError(f"{connector_id} API refused ({e.code})") from None
        except Exception as e:  # noqa: BLE001
            raise ConnectorError(f"{connector_id} API unreachable: {type(e).__name__}") from None

    def call(self, tool: str) -> dict[str, Any]:
        """Execute one connector tool (the caller gates + attests)."""
        connector_id, _, _ = tool.partition(".")
        if tool not in self.tool_names():
            raise ConnectorError(f"unknown connector tool: {tool}")
        base = self.base_url(connector_id) or "https://api.github.com"
        token = self._token(connector_id)
        if tool == "github.whoami":
            data = self._get(connector_id, base, "/user", token)
            return {"tool": tool, "login": data.get("login"),
                    "name": data.get("name"), "public_repos": data.get("public_repos")}
        if tool == "github.repos":
            data = self._get(connector_id, base, "/user/repos?per_page=100", token)
            names = [r.get("full_name") for r in data if isinstance(r, dict)]
            return {"tool": tool, "count": len(names), "repos": names}
        raise ConnectorError(f"unimplemented connector tool: {tool}")
