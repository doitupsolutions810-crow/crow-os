"""Strict attested ledger.

Fixes the failure modes found in the attested-history audit:
- sequence numbers are verified monotonic (0, 1, 2, ...)
- prev_hash chain is verified
- digests are verified over canonical JSON
- a missing ledger file is an error, not "empty log - nothing to verify"
- malformed lines are reported as errors, not tracebacks
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any


def _canon(obj: dict[str, Any]) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def _digest(obj: dict[str, Any]) -> str:
    return hashlib.sha256(_canon(obj).encode()).hexdigest()


class LedgerError(Exception):
    pass


class Ledger:
    def __init__(self, path: Path):
        self.path = Path(path)

    def append(self, source: str, kind: str, raw: dict[str, Any]) -> dict[str, Any]:
        entries = self.read()
        prev = entries[-1] if entries else None
        entry = {
            "seq": (prev["seq"] + 1) if prev else 0,
            "ts": time.time(),
            "source": source,
            "kind": kind,
            "prev_hash": prev["digest"] if prev else None,
            "raw": raw,
            "digest": "",
        }
        entry["digest"] = _digest({k: v for k, v in entry.items() if k != "digest"})
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as f:
            f.write(_canon(entry) + "\n")
        return entry

    def read(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        out = []
        for line in self.path.read_text().splitlines():
            if line.strip():
                out.append(json.loads(line))
        return out

    def verify(self) -> dict[str, Any]:
        """Return {ok, entries, errors}. Never raises for bad data."""
        if not self.path.exists():
            return {"ok": False, "entries": 0,
                    "errors": [f"ledger missing: {self.path}"]}
        errors: list[str] = []
        entries: list[dict[str, Any]] = []
        prev_digest = None
        for i, line in enumerate(self.path.read_text().splitlines()):
            if not line.strip():
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                errors.append(f"line {i}: malformed JSON")
                continue
            entries.append(e)
            if e.get("seq") != len(entries) - 1:
                errors.append(f"line {i}: seq {e.get('seq')!r} != expected {len(entries) - 1}")
            if e.get("prev_hash") != prev_digest:
                errors.append(f"seq {e.get('seq')}: chain break")
            body = {k: v for k, v in e.items() if k != "digest"}
            if _digest(body) != e.get("digest"):
                errors.append(f"seq {e.get('seq')}: digest mismatch")
            prev_digest = e.get("digest")
        return {"ok": not errors, "entries": len(entries), "errors": errors}
