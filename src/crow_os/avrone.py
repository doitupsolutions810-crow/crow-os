"""Explicit, evidence-only AVRONE journal import into Crow OS's signed ledger.

AVRONE bridge posts are untrusted observations, not Crow OS authorization,
not task completion, and never commands. The operator explicitly selects the
local JSONL file. Every record is validated before any ledger append occurs.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import stat
import threading
from pathlib import Path
from typing import Any

MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_POSTS = 100
MAX_LINE_BYTES = 128 * 1024
KINDS = frozenset({"finding", "question", "answer", "correction", "conclusion"})
ID = re.compile(r"[A-Za-z0-9_.:-]{1,80}\Z", re.ASCII)


class AvroneImportError(ValueError):
    pass


_LOCAL_IMPORT_LOCK = threading.RLock()


@contextlib.contextmanager
def _import_lock(ledger: Any):
    """Serialize read-check-append across threads and independent processes."""
    path = getattr(ledger, "path", None)
    if path is None:
        with _LOCAL_IMPORT_LOCK:
            yield
        return
    lockfile = Path(path).with_name(".avrone-import.lock")
    try:
        fd = os.open(lockfile, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) |
                     getattr(os, "O_NONBLOCK", 0), 0o600)
        with os.fdopen(fd, "a+b") as stream:
            meta = os.fstat(stream.fileno())
            if not stat.S_ISREG(meta.st_mode) or meta.st_nlink != 1:
                raise AvroneImportError("unsafe AVRONE import lock")
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    except OSError as e:
        raise AvroneImportError("AVRONE import lock unavailable") from e


def _string(value: Any, field: str, maxlen: int, *, required: bool = False) -> str:
    if not isinstance(value, str) or len(value) > maxlen or "\x00" in value:
        raise AvroneImportError(f"invalid {field}")
    try:
        value.encode("utf-8")
    except UnicodeError as e:
        raise AvroneImportError(f"invalid Unicode in {field}") from e
    if required and not value.strip():
        raise AvroneImportError(f"missing {field}")
    return value


def _post(row: Any) -> dict[str, Any]:
    if not isinstance(row, dict):
        raise AvroneImportError("AVRONE post must be an object")
    pid = _string(row.get("id"), "id", 80, required=True)
    if not ID.fullmatch(pid):
        raise AvroneImportError("invalid AVRONE post id")
    run_id = _string(row.get("run_id"), "run_id", 1024, required=True)
    kind = row.get("kind")
    if not isinstance(kind, str) or kind not in KINDS:
        raise AvroneImportError("invalid AVRONE post kind")
    text = _string(row.get("text"), "text", 4000, required=True)
    agent = _string(row.get("agent", ""), "agent", 80)
    origin = _string(row.get("origin_node", ""), "origin_node", 80)
    refs = row.get("refs", [])
    if not isinstance(refs, list) or len(refs) > 20 or any(
            not isinstance(x, str) or not ID.fullmatch(x) for x in refs):
        raise AvroneImportError("invalid AVRONE post references")
    # Only allowlisted fields enter Crow OS's ledger; arbitrary synthetic
    # metadata cannot masquerade as a tool invocation or role completion.
    return {"id": pid, "run_id": run_id, "kind": kind, "text": text,
            "agent": agent, "origin_node": origin, "refs": list(refs)}


def _read(path: Path) -> tuple[list[dict[str, Any]], str]:
    if path.is_symlink() or not path.is_file():
        raise AvroneImportError("AVRONE source must be a regular file, not a symlink")
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) |
                     getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(fd, "rb") as stream:
            meta = os.fstat(stream.fileno())
            if not stat.S_ISREG(meta.st_mode) or meta.st_nlink != 1 or meta.st_size > MAX_FILE_BYTES:
                raise AvroneImportError("unsafe or oversized AVRONE journal")
            raw = stream.read(MAX_FILE_BYTES + 1)
    except OSError as e:
        raise AvroneImportError("cannot read AVRONE journal") from e
    if len(raw) > MAX_FILE_BYTES:
        raise AvroneImportError("AVRONE journal exceeds byte limit")
    rows = []
    tombstones: set[str] = set()
    for i, line in enumerate(raw.splitlines(), 1):
        if not line.strip():
            continue
        if len(line) > MAX_LINE_BYTES:
            raise AvroneImportError("AVRONE journal exceeds line budget")
        try:
            data = json.loads(line)
        except (ValueError, RecursionError, UnicodeError) as e:
            raise AvroneImportError(f"malformed AVRONE journal line {i}") from e
        if isinstance(data, dict) and set(data) == {"tombstones"}:
            ids = data["tombstones"]
            if not isinstance(ids, list) or len(ids) > 5000 or any(
                    not isinstance(pid, str) or not ID.fullmatch(pid) for pid in ids):
                raise AvroneImportError("invalid AVRONE tombstone record")
            tombstones.update(ids)
            continue
        if len(rows) >= MAX_POSTS:
            raise AvroneImportError("AVRONE journal exceeds post budget")
        rows.append(_post(data))
    return [row for row in rows if row["id"] not in tombstones], hashlib.sha256(raw).hexdigest()


def import_avrone_journal(kernel: Any, path: str | Path) -> dict[str, Any]:
    """Import as evidence; dedupe across repeat imports by (run_id, post_id).

    If any line fails preflight, the ledger remains unchanged. Existing ledger
    integrity must hold before an import. Imported rows are marked untrusted
    and cannot advance a workflow role or authorize a tool.
    """
    source = Path(path)
    rows, digest = _read(source)
    with _import_lock(kernel.ledger):
        verdict = kernel.ledger.verify()
        if not verdict.get("ok"):
            raise AvroneImportError("Crow OS ledger verification failed before import")
        # Preflight the entire admission batch. A reused immutable identity
        # must not silently shadow contradictory findings or override prior
        # signed evidence, even if the conflict occurs after valid new rows.
        fields = ("id", "run_id", "kind", "text", "agent", "origin_node", "refs")
        known: dict[tuple[str, str], dict[str, Any]] = {}
        for entry in kernel.ledger.read():
            if entry.get("kind") != "avrone.observation":
                continue
            body = entry.get("raw")
            if not isinstance(body, dict):
                raise AvroneImportError("invalid existing AVRONE evidence")
            prior = {field: body.get(field) for field in fields}
            key = (prior["run_id"], prior["id"])
            if not all(isinstance(v, str) for v in key):
                raise AvroneImportError("invalid existing AVRONE identity")
            if key in known and known[key] != prior:
                raise AvroneImportError("conflicting existing AVRONE evidence")
            known[key] = prior
        staged: list[dict[str, Any]] = []
        duplicates = 0
        for row in rows:
            key = (row["run_id"], row["id"])
            if key in known:
                if known[key] != row:
                    raise AvroneImportError("conflicting AVRONE post identity")
                duplicates += 1
                continue
            known[key] = row
            staged.append(row)
        for row in staged:
            body = {**row, "untrusted": True, "source_file": source.name,
                    "source_sha256": digest}
            kernel.ledger.append("integration/avrone", "avrone.observation", body)
        return {"accepted": len(staged), "duplicates": duplicates,
                "source_sha256": digest, "ledger_ok": kernel.ledger.verify()["ok"]}
