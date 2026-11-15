"""Connect apps (v0.11) — the plugin catalog, modeled on the Grok Bot
app's Connect apps page: category sections, one-line descriptions,
Connect buttons, a connected count.

Honesty rules, same as devices: a plugin's state is either
*verified* (Crow OS can check it itself — the built-in governed
tools are always available; Ollama is checked live) or *asserted*
(the user toggled it; Crow OS records the claim in
/system/plugins.json and labels it as the user's word, never as a
verified connection).
"""
from __future__ import annotations

import json
from typing import Any

from .fs import CrowFS, FSError

PLUGINS_FILE = "/system/plugins.json"

# id, name, category, description, kind: builtin | verified | asserted
CATALOG: list[dict[str, str]] = [
    {"id": "ollama", "name": "Ollama", "category": "Models",
     "description": "Your OS-installed local models — the chat backend.",
     "kind": "verified"},
    {"id": "net-tools", "name": "Network Tools", "category": "Built-in tools",
     "description": "net.fetch and net.resolve — capability-gated web access for agents.",
     "kind": "builtin"},
    {"id": "fs-tools", "name": "File System Tools", "category": "Built-in tools",
     "description": "fs.list, fs.read and fs.write inside the CrowFS sandbox.",
     "kind": "builtin"},
    {"id": "agent-spawn", "name": "Sub-agent Spawning", "category": "Built-in tools",
     "description": "Agents delegate to sub-agents; every spawn is attested in the ledger.",
     "kind": "builtin"},
    {"id": "github", "name": "GitHub", "category": "Code",
     "description": "Repositories, issues and pull requests for your account.",
     "kind": "asserted"},
    {"id": "gmail", "name": "Gmail", "category": "Communication",
     "description": "Search, read and send mail from your mailbox.",
     "kind": "asserted"},
    {"id": "google-calendar", "name": "Google Calendar", "category": "Productivity",
     "description": "Your calendars, events and free/busy times.",
     "kind": "asserted"},
    {"id": "google-drive", "name": "Google Drive", "category": "Productivity",
     "description": "Find, upload and share files in Drive.",
     "kind": "asserted"},
    {"id": "spotify", "name": "Spotify", "category": "Media",
     "description": "Search and play music, manage playlists.",
     "kind": "asserted"},
    {"id": "stripe", "name": "Stripe", "category": "Finance",
     "description": "Customers, payments, invoices and subscriptions.",
     "kind": "asserted"},
]


class PluginError(Exception):
    pass


class PluginRegistry:
    def __init__(self, fs: CrowFS):
        self.fs = fs

    def _asserted(self) -> dict[str, bool]:
        try:
            return json.loads(self.fs.read_file(PLUGINS_FILE))
        except (FSError, json.JSONDecodeError):
            return {}

    def set_asserted(self, plugin_id: str, connected: bool) -> None:
        if not any(p["id"] == plugin_id and p["kind"] == "asserted"
                   for p in CATALOG):
            raise PluginError(f"plugin {plugin_id!r} is not user-toggleable")
        state = self._asserted()
        state[plugin_id] = bool(connected)
        self.fs.write_file(PLUGINS_FILE, json.dumps(state, indent=2))

    def list(self, ollama_reachable: bool = False,
             connector_states: dict[str, str] | None = None) -> list[dict[str, Any]]:
        connector_states = connector_states or {}
        asserted = self._asserted()
        out = []
        for p in CATALOG:
            entry = dict(p)
            cstate = connector_states.get(p["id"])
            if p["kind"] == "builtin":
                entry["connected"], entry["state"] = True, "built-in"
            elif cstate == "live":
                entry["connected"], entry["state"] = True, "live · verified"
            elif cstate == "unreachable":
                entry["connected"], entry["state"] = False, "configured · unreachable"
            elif p["kind"] == "verified":
                entry["connected"] = bool(ollama_reachable)
                entry["state"] = "verified" if ollama_reachable else "not reachable"
            else:
                entry["connected"] = bool(asserted.get(p["id"]))
                entry["state"] = "connected (your word)" if entry["connected"] else "not connected"
            out.append(entry)
        return out
