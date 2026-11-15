"""Strict attested ledger.

Fixes the failure modes found in the attested-history audit:
- sequence numbers are verified monotonic (0, 1, 2, ...)
- prev_hash chain is verified
- digests are verified over canonical JSON
- a missing ledger file is an error, not "empty log - nothing to verify"
- malformed lines are reported as errors, not tracebacks
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import threading
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
    def __init__(self, path: Path, signer=None):
        self.path = Path(path)
        self._lock = threading.Lock()
        # Optional entry signer. An unkeyed hash chain detects edits but
        # not a full rewrite (probe M1): anyone can recompute digests.
        # With a signer, every entry carries a signature over its body
        # and verification fails closed on unsigned or forged entries.
        self._signer = signer

    def append(self, source: str, kind: str, raw: dict[str, Any]) -> dict[str, Any]:
        # Read-modify-write must be atomic: without the lock, concurrent
        # appends read the same tail and mint duplicate seqs (probe L7).
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a+") as f:
                fcntl.flock(f, fcntl.LOCK_EX)
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
                if self._signer is not None:
                    entry["sig"] = self._signer.sign(
                        {k: v for k, v in entry.items() if k != "sig"})
                f.write(_canon(entry) + "\n")
                f.flush()
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
            body = {k: v for k, v in e.items() if k not in ("digest", "sig")}
            if _digest(body) != e.get("digest"):
                errors.append(f"seq {e.get('seq')}: digest mismatch")
            if self._signer is not None:
                sig = e.get("sig")
                if sig is None:
                    errors.append(f"seq {e.get('seq')}: unsigned entry in signed ledger")
                elif not self._signer.verify(
                        {k: v for k, v in e.items() if k != "sig"}, sig):
                    errors.append(f"seq {e.get('seq')}: bad entry signature")
            prev_digest = e.get("digest")
        return {"ok": not errors, "entries": len(entries), "errors": errors}
