"""Routines (v0.14) — agents that run on a schedule.

A routine binds an agent to an objective and an interval. Definitions
live in the file system (/system/routines/<id>.json) next to the
agents they drive. Firing is a normal governed agent run; the firing
itself is attested (routine.fired links routine -> job in the signed
ledger), and the store records last_run / runs so due-checks survive
restarts. The server ticks due-checks on a loop; the kernel's
due/fire split keeps the logic testable without threads.
"""
from __future__ import annotations

import json
import re
import time
from typing import Any

from .fs import CrowFS, FSError

ROUTINES_DIR = "/system/routines"
MIN_INTERVAL = 10
MAX_ROUTINES = 64


class RoutineError(Exception):
    pass


def _slug(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "routine"


class RoutineStore:
    def __init__(self, fs: CrowFS):
        self.fs = fs
        self.fs.mkdir(ROUTINES_DIR)

    def create(self, name: str, agent_id: str, objective: str,
               interval_seconds: int) -> dict[str, Any]:
        if not isinstance(name, str) or not name.strip():
            raise RoutineError("routine name is required")
        if not isinstance(objective, str) or not objective.strip():
            raise RoutineError("routine objective is required")
        if len(objective) > 10_000:
            raise RoutineError("routine objective too long (max 10000 chars)")
        try:
            interval = int(interval_seconds)
        except (TypeError, ValueError):
            raise RoutineError("interval_seconds must be a number") from None
        if interval < MIN_INTERVAL:
            raise RoutineError(f"interval_seconds must be >= {MIN_INTERVAL}")
        existing = self.list()
        if len(existing) >= MAX_ROUTINES:
            raise RoutineError("routine limit reached")
        base, rid, n = _slug(name), _slug(name), 2
        ids = {r["id"] for r in existing}
        while rid in ids:
            rid = f"{base}-{n}"; n += 1
        routine = {"id": rid, "name": name.strip()[:60], "agent": agent_id,
                   "objective": objective, "interval_seconds": interval,
                   "enabled": True, "last_run": 0.0, "runs": 0,
                   "created": time.time()}
        self.fs.write_file(f"{ROUTINES_DIR}/{rid}.json",
                           json.dumps(routine, indent=2))
        return routine

    def get(self, routine_id: str) -> dict[str, Any]:
        try:
            return json.loads(self.fs.read_file(f"{ROUTINES_DIR}/{routine_id}.json"))
        except (FSError, json.JSONDecodeError):
            raise RoutineError(f"unknown routine: {routine_id}") from None

    def list(self) -> list[dict[str, Any]]:
        out = []
        for entry in self.fs.list_dir(ROUTINES_DIR):
            if entry["name"].endswith(".json"):
                try:
                    out.append(json.loads(
                        self.fs.read_file(f"{ROUTINES_DIR}/{entry['name']}")))
                except (FSError, json.JSONDecodeError):
                    continue
        return sorted(out, key=lambda r: r["id"])

    def _save(self, routine: dict[str, Any]) -> None:
        self.fs.write_file(f"{ROUTINES_DIR}/{routine['id']}.json",
                           json.dumps(routine, indent=2))

    def set_enabled(self, routine_id: str, enabled: bool) -> dict[str, Any]:
        routine = self.get(routine_id)
        routine["enabled"] = bool(enabled)
        self._save(routine)
        return routine

    def mark_run(self, routine_id: str, when: float) -> dict[str, Any]:
        routine = self.get(routine_id)
        routine["last_run"] = when
        routine["runs"] = int(routine.get("runs") or 0) + 1
        self._save(routine)
        return routine

    def delete(self, routine_id: str) -> None:
        self.get(routine_id)
        real = self.fs.resolve(f"{ROUTINES_DIR}/{routine_id}.json")
        if real.exists():
            real.unlink()

    def due(self, now: float | None = None) -> list[dict[str, Any]]:
        now = time.time() if now is None else now
        return [r for r in self.list()
                if r.get("enabled")
                and now - float(r.get("last_run") or 0) >= int(r["interval_seconds"])]
