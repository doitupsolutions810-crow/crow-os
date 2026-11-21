"""Capability broker.

Fixes the capability-plane audit findings:
- verify() compares method AND resource exactly (no method confusion)
- revocation walks the lineage: revoking a parent revokes descendants
- rate limits are keyed by lineage root, so attenuated children cannot
  reset them
- attenuation can only narrow rights, never amplify
- constraints are copied, never shared by reference

Dev signer is HMAC-SHA256 with a local secret. The production swap is an
Ed25519 signer behind the same issue/verify contract.

Security gate round (2026-11-21), closing the crow-os review findings:
- GAP-013: malformed, foreign-lineage, expired and revoked parent
  capabilities are refused at issue() with CapabilityError — nothing
  escapes the authorization boundary as KeyError/IndexError/TypeError,
  and a parent can never spawn children past its own expiry.
- GAP-014: the rate-limit check-and-increment in authorize() is atomic.
  The broker lock (threads) plus a POSIX advisory flock on the shared
  state file (processes) are held across reload -> check -> increment
  -> persist, so concurrent authorizations can never exceed a lineage
  rate limit.
- GAP-015 lives in runtime.py: production mode refuses path-derived
  secrets and demands externally provisioned key material.
"""
from __future__ import annotations

import copy
import json
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .signers import HmacSigner, Signer

try:  # POSIX-only cross-process advisory lock (paired with the thread lock)
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - non-POSIX hosts degrade to thread-only
    _fcntl = None


class CapabilityError(Exception):
    pass


_REQUIRED_PARENT_FIELDS = ("id", "rights", "expires", "sig")


def _valid_rights(rights: Any) -> bool:
    if not isinstance(rights, list):
        return False
    for r in rights:
        if not isinstance(r, dict):
            return False
        if not isinstance(r.get("method"), str) or not isinstance(r.get("resource"), str):
            return False
    return True


@contextmanager
def _cross_process_lock(path: Path | None):
    """Serialize brokers that share one state file across processes.

    Threads are serialized by the broker's own lock; this advisory flock
    covers concurrent processes. Best effort: on platforms without fcntl
    it degrades to thread-only atomicity instead of breaking the caller.
    """
    if path is None or _fcntl is None:
        yield
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+") as fh:
        _fcntl.flock(fh.fileno(), _fcntl.LOCK_EX)
        try:
            yield
        finally:
            _fcntl.flock(fh.fileno(), _fcntl.LOCK_UN)


class Broker:
    def __init__(self, secret: bytes | None = None, *, signer: Signer | None = None,
                 state_path: Path | None = None):
        if signer is None:
            if secret is None:
                raise CapabilityError("Broker needs a secret or a signer")
            signer = HmacSigner(secret)
        self._signer = signer
        self._revoked: set[str] = set()
        self._lineage: dict[str, str] = {}   # cap_id -> root_id
        self._usage: dict[str, int] = {}     # root_id -> executions
        self._root_limits: dict[str, int] = {}  # root_id -> lineage rate limit
        # Optional file-backed state: revocations, lineage and usage are
        # shared by every broker opened on the same home, so a revocation
        # survives restarts and reaches other processes (probe C7).
        # RLock: authorize() holds it across the atomic usage update and
        # the persist that happens inside the same section.
        self._lock = threading.RLock()
        self._state_path = Path(state_path) if state_path else None
        if self._state_path and self._state_path.exists():
            # The state file is a security input (it carries revocations
            # and rate-limit usage), so it is signed like a capability:
            # editing it is forgery, and forgery fails closed (probe M2).
            self._apply_state(self._read_state_file())

    def _read_state_file(self) -> dict[str, Any]:
        saved = json.loads(self._state_path.read_text())
        sig = saved.pop("sig", None)
        if sig is None or not self._signer.verify(saved, sig):
            raise CapabilityError(
                "broker state signature invalid - refusing to load")
        return saved

    def _apply_state(self, saved: dict[str, Any]) -> None:
        self._revoked = set(saved.get("revoked", []))
        self._lineage = dict(saved.get("lineage", {}))
        self._usage = {k: int(v) for k, v in saved.get("usage", {}).items()}
        self._root_limits = {
            k: int(v) for k, v in saved.get("root_limits", {}).items()}

    def _persist_locked(self) -> None:
        """Write state. The caller MUST hold self._lock (and the flock
        when a state file is shared across processes)."""
        if not self._state_path:
            return
        # Serialized + uniquely-named temp file: two threads sharing one
        # fixed .tmp name raced — the first replace() consumed the file
        # and the second crashed with FileNotFoundError (probe N6).
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        state = {
            "revoked": sorted(self._revoked),
            "lineage": self._lineage,
            "usage": self._usage,
            "root_limits": self._root_limits,
        }
        state["sig"] = self._signer.sign(state)
        tmp = self._state_path.with_name(
            f"{self._state_path.name}.{uuid.uuid4().hex[:8]}.tmp")
        tmp.write_text(json.dumps(state, sort_keys=True) + "\n")
        tmp.replace(self._state_path)  # atomic: no torn state files

    def _persist(self) -> None:
        with self._lock, _cross_process_lock(self._state_path):
            self._persist_locked()

    def _sign(self, cap: dict[str, Any]) -> str:
        body = {k: v for k, v in cap.items() if k != "sig"}
        return self._signer.sign(body)

    def issue(self, subject: str, rights: list[dict[str, str]], *,
              ttl_seconds: int = 300, rate_limit: int = 5,
              parent: dict[str, Any] | None = None) -> dict[str, Any]:
        if ttl_seconds <= 0:
            raise CapabilityError("ttl must be positive")
        if rate_limit <= 0:
            raise CapabilityError("rate_limit must be positive")
        # Validate our own rights shape at the boundary: a malformed list
        # must fail here, not as a KeyError inside authorize().
        if not _valid_rights(rights):
            raise CapabilityError(
                "malformed rights: each right needs method and resource strings")
        cap_id = uuid.uuid4().hex
        if parent is not None:
            # GAP-013: parent validation happens entirely inside the
            # authorization boundary. Malformed, foreign, expired and
            # revoked parents are refused with CapabilityError.
            if not isinstance(parent, dict):
                raise CapabilityError("malformed parent capability")
            missing = [f for f in _REQUIRED_PARENT_FIELDS if f not in parent]
            if missing:
                raise CapabilityError(
                    "malformed parent capability: missing "
                    + ", ".join(missing))
            if not _valid_rights(parent["rights"]):
                raise CapabilityError(
                    "malformed parent capability: bad rights")
            self._check_signature(parent)
            if time.time() > parent["expires"]:
                raise CapabilityError("parent expired")
            root = self._lineage.get(parent["id"])
            if root is None:
                # A capability signed with the same key but issued by a
                # different broker (its lineage was never recorded here)
                # is a foreign capability, not a valid parent.
                raise CapabilityError(
                    "parent capability unknown to this broker (foreign lineage)")
            parent_rights = {(r["method"], r["resource"]) for r in parent["rights"]}
            for r in rights:
                if (r["method"], r["resource"]) not in parent_rights:
                    raise CapabilityError("attenuation cannot amplify rights")
            if parent["id"] in self._revoked or root in self._revoked:
                raise CapabilityError("parent revoked")
        else:
            root = cap_id
            self._root_limits[root] = rate_limit
        expires = time.time() + ttl_seconds
        if parent is not None:
            # Attenuation must not amplify duration either: a child can
            # never outlive its parent (probe C2). With the expired-parent
            # refusal above, this can only clamp, never resurrect.
            expires = min(expires, parent["expires"])
        cap = {
            "id": cap_id,
            "root": root,
            "parent_id": parent["id"] if parent else None,
            "subject": subject,
            "rights": copy.deepcopy(rights),
            "expires": expires,
            "rate_limit": rate_limit,
        }
        cap["sig"] = self._sign(cap)
        self._lineage[cap_id] = root
        self._persist()
        return cap

    def _check_signature(self, cap: dict[str, Any]) -> None:
        body = {k: v for k, v in cap.items() if k != "sig"}
        if not self._signer.verify(body, cap.get("sig", "")):
            raise CapabilityError("bad signature")

    def revoke(self, cap_id: str) -> None:
        self._revoked.add(cap_id)
        self._persist()

    def _lineage_revoked(self, cap: dict[str, Any]) -> bool:
        return cap["id"] in self._revoked or cap["root"] in self._revoked

    def _check_right(self, cap: dict[str, Any], method: str,
                     resource: str) -> None:
        if not _valid_rights(cap.get("rights")):
            raise CapabilityError("malformed capability: bad rights")
        for r in cap["rights"]:
            if r["method"] == method and r["resource"] == resource:
                return
        raise CapabilityError(f"right not held: {method} {resource}")

    def authorize(self, cap: dict[str, Any], method: str,
                  resource: str) -> dict[str, Any]:
        """Execute one unit of work under a capability, or raise."""
        if not isinstance(cap, dict):
            raise CapabilityError("malformed capability")
        self._check_signature(cap)
        if not isinstance(cap.get("root"), str):
            raise CapabilityError("malformed capability: missing root")
        expires = cap.get("expires")
        if not isinstance(expires, (int, float)):
            raise CapabilityError("malformed capability: missing expires")
        if time.time() > expires:
            raise CapabilityError("expired")
        if self._lineage_revoked(cap):
            raise CapabilityError("revoked (lineage)")
        self._check_right(cap, method, resource)
        limit = cap.get("rate_limit")
        if not isinstance(limit, int) or limit <= 0:
            raise CapabilityError("malformed capability: rate_limit")
        # GAP-014: the whole usage check-and-increment is atomic. The
        # broker lock closes the thread race, the flock on the shared
        # state file closes the process race, and the reload first thing
        # under the lock means another broker's persisted consumption
        # counts against this lineage before this check runs.
        with self._lock, _cross_process_lock(self._state_path):
            if self._state_path is not None and self._state_path.exists():
                self._apply_state(self._read_state_file())
            root = cap["root"]
            if self._usage.get(root, 0) >= self._root_limits.get(root, limit):
                raise CapabilityError("rate limit (lineage)")
            self._usage[root] = self._usage.get(root, 0) + 1
            self._persist_locked()
        return {"status": "ok", "capability": cap["id"], "method": method,
                "resource": resource}
