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

import contextlib
import fcntl
import json
import math
import os
import re
import stat
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


ID_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z", re.ASCII)


def _clean_text(value: str, field: str, limit: int, *, multiline: bool = False) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AgentError(f"{field} is required")
    value = value.strip()
    if len(value) > limit:
        raise AgentError(f"{field} too long (max {limit} chars)")
    if any((ord(ch) < 32 and (not multiline or ch not in "\n\t\r")) or
           ord(ch) == 127 for ch in value):
        raise AgentError(f"{field} contains a control character")
    try:
        value.encode("utf-8")
    except UnicodeError as e:
        raise AgentError(f"{field} contains invalid Unicode") from e
    return value


@contextlib.contextmanager
def _creation_lock(fs: CrowFS):
    """Keep duplicate agent names distinct across threads and processes."""
    try:
        target = fs.resolve(f"{AGENTS_DIR}/.create.lock")
        fd = os.open(target, os.O_RDWR | os.O_CREAT |
                     getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "a+b") as stream:
            st = os.fstat(stream.fileno())
            if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
                raise AgentError("unsafe agent creation lock")
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    except (OSError, FSError) as e:
        raise AgentError("agent creation lock unavailable") from e


def _safe_id(agent_id: str) -> str:
    if not isinstance(agent_id, str) or len(agent_id) > 120 or not ID_RE.fullmatch(agent_id):
        raise AgentError("invalid agent ID")
    return agent_id


class AgentRegistry:
    def __init__(self, fs: CrowFS):
        self.fs = fs
        self.fs.mkdir(AGENTS_DIR)

    def create(self, name: str, instructions: str,
               model: str | None = None,
               tools: list[str] | None = None,
               avatar: dict[str, str] | None = None) -> dict[str, Any]:
        name = _clean_text(name, "agent name", MAX_NAME)
        instructions = _clean_text(
            instructions, "custom instructions", MAX_INSTRUCTIONS, multiline=True)
        if model is not None:
            if not isinstance(model, str):
                raise AgentError("model must be a string")
            model = _clean_text(model, "model", 200) if model.strip() else None
            if model is not None and any(ch.isspace() for ch in model):
                raise AgentError("model name cannot contain whitespace")
        if tools is None:
            tools = []
        if not isinstance(tools, (list, tuple)):
            raise AgentError("tools must be a list of approved tool names")
        grants = []
        for t in tools:
            if not isinstance(t, str) or t not in TOOLS:
                raise AgentError(f"unknown tool: {t!r}")
            if t not in grants:
                grants.append(t)
        look = dict(DEFAULT_AVATAR)
        if avatar is not None:
            if not isinstance(avatar, dict):
                raise AgentError("avatar must be an object")
            if avatar.get("shape") is not None:
                if not isinstance(avatar["shape"], str) or avatar["shape"] not in AVATAR_SHAPES:
                    raise AgentError("unknown avatar shape")
                look["shape"] = avatar["shape"]
            if avatar.get("color") is not None:
                if not isinstance(avatar["color"], str) or avatar["color"] not in AVATAR_COLORS:
                    raise AgentError("unknown avatar color")
                look["color"] = avatar["color"]
        base = _slugify(name)
        if base in RESERVED:
            raise AgentError(f"{name!r} collides with a built-in role")
        with _creation_lock(self.fs):
            agent_id, n = base, 2
            while self.fs.exists(f"{AGENTS_DIR}/{agent_id}.json"):
                agent_id = f"{base}-{n}"
                n += 1
            agent = {"id": agent_id, "name": name, "instructions": instructions,
                     "model": model, "tools": grants, "avatar": look,
                     "created": time.time()}
            self.fs.write_file(f"{AGENTS_DIR}/{agent_id}.json",
                               json.dumps(agent, indent=2))
        return agent

    def get(self, agent_id: str) -> dict[str, Any]:
        agent_id = _safe_id(agent_id)
        try:
            agent = json.loads(self.fs.read_file(f"{AGENTS_DIR}/{agent_id}.json"))
        except (FSError, ValueError, UnicodeError) as e:
            raise AgentError(f"invalid or missing agent: {agent_id}") from e
        if (not isinstance(agent, dict) or agent.get("id") != agent_id or
                not isinstance(agent.get("name"), str) or
                not isinstance(agent.get("instructions"), str)):
            raise AgentError(f"corrupt agent definition: {agent_id}")
        # Legacy Crow OS agents created before the avatar schema sometimes
        # lack these optional fields; retain them with least-privilege defaults.
        agent.setdefault("created", 0.0)
        agent.setdefault("tools", [])
        created = agent["created"]
        if (type(created) not in (int, float) or
                not 0 <= created <= 10_000_000_000_000 or
                (type(created) is float and not math.isfinite(created)) or
                not isinstance(agent.get("tools"), list) or
                any(not isinstance(t, str) or t not in TOOLS for t in agent["tools"]) or
                (agent.get("model") is not None and
                 (not isinstance(agent["model"], str) or
                  any(ch.isspace() for ch in agent["model"])))):
            raise AgentError(f"corrupt agent definition: {agent_id}")
        agent.setdefault("avatar", dict(DEFAULT_AVATAR))
        return agent

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
        agent_id = _safe_id(agent_id)
        try:
            real = self.fs.resolve(f"{AGENTS_DIR}/{agent_id}.json")
        except FSError as e:
            raise AgentError(f"invalid agent ID: {agent_id}") from e
        if not real.exists():
            raise AgentError(f"unknown agent: {agent_id}")
        real.unlink()
