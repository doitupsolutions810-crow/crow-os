"""Custom agents (v0.8) — users create agents, name them, and write
their instructions.

An agent definition is a small JSON document in the OS file system
(/system/agents/<id>.json), so agents persist across restarts and are
browsable in the Files tab like everything else:

    {"id": "edge-runner", "name": "Edge Runner",
     "instructions": "You hunt edge cases...", "model": null,
     "tools": ["net.fetch"], "created": 1794...}

Rules that keep this safe inside the OS's discipline:
- Names are required (<= 60 chars); instructions are required
  (<= 4000 chars) — they become the agent's system prompt verbatim.
- Tool grants are a subset of the OS tool catalog; an agent only
  ever receives capabilities for the tools it was granted.
- Custom instructions are data for the model, never authority: an
  agent still runs behind the capability gate, and its chain still
  ends with the deterministic governance step (see workflow.py).
- Reserved ids (the built-in squad roles) cannot be taken.
"""
from __future__ import annotations

import json
import re
import threading
import time
import unicodedata
from typing import Any, Callable, Iterable

from .fs import CrowFS, FSError
from .tools import TOOLS

AGENTS_DIR = "/system/agents"
MAX_NAME = 60
MAX_INSTRUCTIONS = 4000
RESERVED = {"debugger", "coder", "tester", "governance", "kernel", "squad"}

# The Create-a-Bot sheet from the Grok Bot app: a grid of blob shapes
# and a row of colors. Agents carry both, rendered everywhere they
# appear (rail, chat header, profile).
AVATAR_SHAPES = ["blob", "round", "square", "tall", "wide", "drop",
                 "diamond", "ring", "pill", "star", "hex", "wave", "egg"]
AVATAR_COLORS = {"white": "#f5f5f5", "brown": "#8d6e63", "red": "#ef5350",
                 "orange": "#ff7a1a", "yellow": "#ffd54f", "green": "#66bb6a",
                 "teal": "#26a69a", "blue": "#42a5f5", "purple": "#ab47bc",
                 "pink": "#ec407a", "gray": "#9e9e9e"}
DEFAULT_AVATAR = {"shape": "blob", "color": "orange"}


class AgentError(Exception):
    pass


def _has_control_chars(value: str, *, allow_line_breaks: bool = False) -> bool:
    return any(
        unicodedata.category(ch) in {"Cc", "Cf", "Cs"}
        and not (allow_line_breaks and ch in "\\n\\r\\t")
        for ch in value
    )


def _valid_agent_id(agent_id: object) -> bool:
    return (
        isinstance(agent_id, str)
        and len(agent_id) <= 80
        and re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", agent_id) is not None
    )


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "agent"


class AgentRegistry:
    def __init__(self, fs: CrowFS):
        self.fs = fs
        # Round 28: the federation handler must hold this lock across
        # agents.get -> check_replay -> federation_receive, and delete()
        # must take it too — otherwise a delete landing between the
        # handler's existence check and the receive's re-check burns
        # the dispatch's body in the seen-store (or, landing between
        # the attestation and run_agent's re-fetch, leaves a receipt
        # for a job that never ran). An RLock: the handler re-enters
        # through federation_receive's own agents.get.
        self.lock = threading.RLock()
        self._extra_tools_provider: Callable[[], Iterable[str]] = lambda: ()
        self.fs.mkdir(AGENTS_DIR)

    def set_extra_tools_provider(self, provider: Callable[[], Iterable[str]]) -> None:
        """Attach the current trusted connector catalog supplied by the kernel."""
        if not callable(provider):
            raise TypeError("extra tool provider must be callable")
        self._extra_tools_provider = provider

    def _tool_catalog(self) -> set[str]:
        catalog = set(TOOLS)
        try:
            provided = self._extra_tools_provider()
        except Exception:
            # A connector registry error must not widen permissions.
            provided = ()
        if isinstance(provided, (list, tuple, set)):
            catalog.update(item for item in provided
                           if isinstance(item, str) and item and not _has_control_chars(item))
        return catalog

    def create(self, name: str, instructions: str,
               model: str | None = None,
               tools: list[str] | None = None,
               avatar: dict[str, str] | None = None,
               extra_tools: list[str] | None = None) -> dict[str, Any]:
        if not isinstance(name, str) or not name.strip():
            raise AgentError("agent name is required")
        name = name.strip()
        if _has_control_chars(name):
            raise AgentError("agent name contains control characters")
        if len(name) > MAX_NAME:
            raise AgentError(f"name too long (max {MAX_NAME} chars)")
        if not isinstance(instructions, str) or not instructions.strip():
            raise AgentError("custom instructions are required")
        instructions = instructions.strip()
        # Newlines and tabs are formatting in a system prompt, not hostile
        # control data. Other control/format/surrogate code points remain refused.
        if _has_control_chars(instructions, allow_line_breaks=True):
            raise AgentError("custom instructions contain control characters")
        if len(instructions) > MAX_INSTRUCTIONS:
            raise AgentError(f"instructions too long (max {MAX_INSTRUCTIONS} chars)")

        if model is not None:
            if not isinstance(model, str):
                raise AgentError("model must be a model name string")
            model = model.strip()
            if model and (
                len(model) > 128
                or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]*", model) is None
            ):
                raise AgentError("model must be a valid model name")
            model = model or None

        if tools is not None and not isinstance(tools, list):
            raise AgentError("tools must be a list of tool names")
        if extra_tools is not None:
            if not isinstance(extra_tools, list) or any(not isinstance(item, str) for item in extra_tools):
                raise AgentError("extra_tools must be a list of tool names")
        catalog = self._tool_catalog()
        # The kernel owns the connector catalog. A caller-supplied list may
        # describe that catalog but can never expand it.
        if extra_tools is not None and any(item not in catalog for item in extra_tools):
            raise AgentError("extra_tools contains a tool outside the trusted catalog")
        grants: list[str] = []
        for tool in tools or []:
            if not isinstance(tool, str) or not tool or _has_control_chars(tool):
                raise AgentError("tool names must be non-empty strings")
            if tool not in catalog:
                raise AgentError(f"unknown tool: {tool}")
            if tool not in grants:
                grants.append(tool)

        if avatar is not None and not isinstance(avatar, dict):
            raise AgentError("avatar must be an object")
        avatar = avatar or {}
        look = dict(DEFAULT_AVATAR)
        shape = avatar.get("shape")
        color = avatar.get("color")
        if shape is not None:
            if not isinstance(shape, str) or shape not in AVATAR_SHAPES:
                raise AgentError(f"unknown avatar shape {shape!r}")
            look["shape"] = shape
        if color is not None:
            if not isinstance(color, str) or color not in AVATAR_COLORS:
                raise AgentError(f"unknown avatar color {color!r}")
            look["color"] = color

        base = _slugify(name)
        if base in RESERVED:
            raise AgentError(f"'{name}' collides with a built-in role")

        # The name check and file write form one critical section. Without it,
        # parallel same-name requests can choose the same suffix and overwrite
        # one another even though CrowFS writes use unique temporary files.
        with self.lock:
            agent_id, n = base, 2
            while self.fs.exists(f"{AGENTS_DIR}/{agent_id}.json"):
                agent_id = f"{base}-{n}"
                n += 1
            agent = {
                "id": agent_id,
                "name": name,
                "instructions": instructions,
                "model": model,
                "tools": grants,
                "avatar": look,
                "created": time.time(),
            }
            self.fs.write_file(
                f"{AGENTS_DIR}/{agent_id}.json",
                json.dumps(agent, indent=2),
            )
            return agent

    def _read_definition(self, agent_id: str) -> dict[str, Any]:
        try:
            raw = json.loads(self.fs.read_file(f"{AGENTS_DIR}/{agent_id}.json"))
        except FSError:
            raise AgentError(f"unknown agent: {agent_id}") from None
        except OSError:
            raise AgentError(f"unknown agent: {agent_id}") from None
        except json.JSONDecodeError:
            raise AgentError(f"corrupt agent definition: {agent_id}") from None

        if not isinstance(raw, dict):
            raise AgentError(f"corrupt agent definition: {agent_id}")
        # Hand-edited definitions may predate optional display fields. Retain
        # readable, least-privileged definitions; never infer additional grants.
        if not isinstance(raw.get("name"), str) or not raw["name"].strip():
            raise AgentError(f"corrupt agent definition: {agent_id}")
        if not isinstance(raw.get("instructions"), str):
            raise AgentError(f"corrupt agent definition: {agent_id}")
        stored_id = raw.get("id", agent_id)
        if stored_id != agent_id:
            raise AgentError(f"corrupt agent definition: {agent_id}")
        model = raw.get("model")
        if model is not None and not isinstance(model, str):
            raise AgentError(f"corrupt agent definition: {agent_id}")
        grants = raw.get("tools", [])
        if not isinstance(grants, list) or any(not isinstance(item, str) for item in grants):
            raise AgentError(f"corrupt agent definition: {agent_id}")
        catalog = self._tool_catalog()
        if any(item not in catalog for item in grants):
            raise AgentError(f"agent definition contains an unregistered tool: {agent_id}")
        created = raw.get("created", 0)
        if not isinstance(created, (int, float)) or isinstance(created, bool):
            created = 0
        avatar = raw.get("avatar")
        if not isinstance(avatar, dict):
            avatar = dict(DEFAULT_AVATAR)
        else:
            avatar = dict(DEFAULT_AVATAR) | {
                "shape": avatar.get("shape") if avatar.get("shape") in AVATAR_SHAPES else DEFAULT_AVATAR["shape"],
                "color": avatar.get("color") if avatar.get("color") in AVATAR_COLORS else DEFAULT_AVATAR["color"],
            }
        return {
            "id": agent_id,
            "name": raw["name"].strip(),
            "instructions": raw["instructions"],
            "model": model,
            "tools": list(dict.fromkeys(grants)),
            "avatar": avatar,
            "created": created,
        }

    def get(self, agent_id: str) -> dict[str, Any]:
        if not _valid_agent_id(agent_id):
            raise AgentError(f"unknown agent: {agent_id}")
        with self.lock:
            return self._read_definition(agent_id)

    def list(self) -> list[dict[str, Any]]:
        with self.lock:
            out = []
            for entry in self.fs.list_dir(AGENTS_DIR):
                if entry["type"] == "file" and entry["name"].endswith(".json"):
                    agent_id = entry["name"][:-5]
                    if not _valid_agent_id(agent_id):
                        continue
                    try:
                        out.append(self._read_definition(agent_id))
                    except AgentError:
                        continue
            return sorted(out, key=lambda agent: (agent["created"], agent["id"]))

    def delete(self, agent_id: str) -> None:
        # Keep delete ordered with dispatch validation and creation.
        if not _valid_agent_id(agent_id):
            raise AgentError(f"unknown agent: {agent_id}")
        if agent_id in RESERVED:
            raise AgentError(f"cannot delete reserved agent: {agent_id}")
        with self.lock:
            try:
                real = self.fs.resolve(f"{AGENTS_DIR}/{agent_id}.json")
                if not real.is_file():
                    raise AgentError(f"unknown agent: {agent_id}")
                real.unlink()
            except FSError:
                raise AgentError(f"unknown agent: {agent_id}") from None
            except OSError:
                raise AgentError(f"unknown agent: {agent_id}") from None
