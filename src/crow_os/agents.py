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
import time
from typing import Any

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


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "agent"


class AgentRegistry:
    def __init__(self, fs: CrowFS):
        self.fs = fs
        self.fs.mkdir(AGENTS_DIR)

    def create(self, name: str, instructions: str,
               model: str | None = None,
               tools: list[str] | None = None,
               avatar: dict[str, str] | None = None,
               extra_tools: list[str] | None = None) -> dict[str, Any]:
        if not isinstance(name, str) or not name.strip():
            raise AgentError("agent name is required")
        name = name.strip()
        if len(name) > MAX_NAME:
            raise AgentError(f"name too long (max {MAX_NAME} chars)")
        if not isinstance(instructions, str) or not instructions.strip():
            raise AgentError("custom instructions are required")
        instructions = instructions.strip()
        if len(instructions) > MAX_INSTRUCTIONS:
            raise AgentError(
                f"instructions too long (max {MAX_INSTRUCTIONS} chars)")
        grants = []
        catalog = TOOLS + list(extra_tools or [])
        for t in tools or []:
            if t not in catalog:
                raise AgentError(f"unknown tool: {t}")
            if t not in grants:
                grants.append(t)
        base = _slugify(name)
        if base in RESERVED:
            raise AgentError(f"'{name}' collides with a built-in role")
        agent_id, n = base, 2
        while self.fs.exists(f"{AGENTS_DIR}/{agent_id}.json"):
            agent_id = f"{base}-{n}"
            n += 1
        look = dict(DEFAULT_AVATAR)
        if avatar:
            if avatar.get("shape") is not None:
                if avatar["shape"] not in AVATAR_SHAPES:
                    raise AgentError(f"unknown avatar shape {avatar['shape']!r}")
                look["shape"] = avatar["shape"]
            if avatar.get("color") is not None:
                if avatar["color"] not in AVATAR_COLORS:
                    raise AgentError(f"unknown avatar color {avatar['color']!r}")
                look["color"] = avatar["color"]
        agent = {"id": agent_id, "name": name, "instructions": instructions,
                 "model": model or None, "tools": grants, "avatar": look,
                 "created": time.time()}
        self.fs.write_file(f"{AGENTS_DIR}/{agent_id}.json",
                           json.dumps(agent, indent=2))
        return agent

    def get(self, agent_id: str) -> dict[str, Any]:
        try:
            return json.loads(self.fs.read_file(f"{AGENTS_DIR}/{agent_id}.json"))
        except FSError:
            raise AgentError(f"unknown agent: {agent_id}") from None
        except json.JSONDecodeError:
            raise AgentError(f"corrupt agent definition: {agent_id}") from None

    def list(self) -> list[dict[str, Any]]:
        out = []
        for e in self.fs.list_dir(AGENTS_DIR):
            if e["type"] == "file" and e["name"].endswith(".json"):
                try:
                    out.append(self.get(e["name"][:-5]))
                except AgentError:
                    continue
        return sorted(out, key=lambda a: a["created"])

    def delete(self, agent_id: str) -> None:
        real = self.fs.resolve(f"{AGENTS_DIR}/{agent_id}.json")
        if not real.exists():
            raise AgentError(f"unknown agent: {agent_id}")
        real.unlink()
