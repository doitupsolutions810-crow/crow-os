"""Bounded, explicit, capability-gated connector directives for agent jobs.

A connector is invoked only for an operator objective line beginning
with connector:, and the broker authorizes TOOL for that specific resource
before the network call. Model output never triggers connector execution.
"""
from __future__ import annotations

import json
import re

from .tools import ToolError

MAX_DIRECTIVES = 2
MAX_RESULT_CHARS = 2500
_DIRECTIVE = re.compile(r"^connector:\s*([a-z][a-z0-9_.-]*)\s*$", re.I)
KNOWN_TOOLS = frozenset(("github.repos", "github.whoami"))


def connector_context(objective, grants, broker, cap, runner, ledger, job_id):
    """Run explicit connector lines as untrusted, audit-attributed context."""
    lines = [line.strip() for line in objective.splitlines()
             if line.strip().lower().startswith("connector:")]
    if len(lines) > MAX_DIRECTIVES:
        raise ToolError("connector directive budget exceeded")
    parts = []
    for line in lines:
        match = _DIRECTIVE.fullmatch(line)
        if match is None:
            raise ToolError("invalid connector directive")
        tool = match.group(1).lower()
        if tool not in KNOWN_TOOLS:
            raise ToolError("unknown connector tool")
        if runner is None or tool not in grants:
            raise ToolError("connector tool not granted")
        broker.authorize(cap, "TOOL", tool)
        try:
            result = runner.call(tool)
        except Exception as exc:
            raise ToolError("connector call failed") from exc
        if not isinstance(result, dict):
            raise ToolError("connector returned invalid result")
        try:
            snippet = json.dumps(result, ensure_ascii=True,
                                 allow_nan=False)[:MAX_RESULT_CHARS]
        except (ValueError, TypeError, OverflowError) as exc:
            raise ToolError("connector returned non-JSON data") from exc
        # Attest after validation, without credentials or raw headers.
        ledger.append("crow-os/tools", "tool.invoked",
                      {"job_id": job_id, "tool": tool, "status": "ok"})
        parts.append(f"\nUntrusted connector data from {tool}:\n{snippet}\n")
    return "".join(parts)
