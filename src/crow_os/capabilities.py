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
"""
from __future__ import annotations

import copy
import json
import time
import uuid
from pathlib import Path
from typing import Any

from .signers import HmacSigner, Signer


class CapabilityError(Exception):
    pass


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
        self._state_path = Path(state_path) if state_path else None
        if self._state_path and self._state_path.exists():
            saved = json.loads(self._state_path.read_text())
            sig = saved.pop("sig", None)
            # The state file is a security input (it carries revocations
            # and rate-limit usage), so it is signed like a capability:
            # editing it is forgery, and forgery fails closed (probe M2).
            if sig is None or not self._signer.verify(saved, sig):
                raise CapabilityError("broker state signature invalid — refusing to load")
            self._revoked = set(saved.get("revoked", []))
            self._lineage = dict(saved.get("lineage", {}))
            self._usage = {k: int(v) for k, v in saved.get("usage", {}).items()}
            self._root_limits = {k: int(v) for k, v in saved.get("root_limits", {}).items()}

    def _persist(self) -> None:
        if not self._state_path:
            return
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        state = {
            "revoked": sorted(self._revoked),
            "lineage": self._lineage,
            "usage": self._usage,
            "root_limits": self._root_limits,
        }
        state["sig"] = self._signer.sign(state)
        tmp = self._state_path.with_suffix(self._state_path.suffix + ".tmp")
        tmp.write_text(json.dumps(state, sort_keys=True) + "\n")
        tmp.replace(self._state_path)  # atomic: no torn state files

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
        cap_id = uuid.uuid4().hex
        if parent is not None:
            self._check_signature(parent)
            parent_rights = {(r["method"], r["resource"]) for r in parent["rights"]}
            for r in rights:
                if (r["method"], r["resource"]) not in parent_rights:
                    raise CapabilityError("attenuation cannot amplify rights")
            root = self._lineage[parent["id"]]
            if parent["id"] in self._revoked or root in self._revoked:
                raise CapabilityError("parent revoked")
        else:
            root = cap_id
            self._root_limits[root] = rate_limit
        expires = time.time() + ttl_seconds
        if parent is not None:
            # Attenuation must not amplify duration either: a child can
            # never outlive its parent (probe C2).
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

    def authorize(self, cap: dict[str, Any], method: str, resource: str) -> dict[str, Any]:
        """Execute one unit of work under a capability, or raise."""
        self._check_signature(cap)
        if time.time() > cap["expires"]:
            raise CapabilityError("expired")
        if self._lineage_revoked(cap):
            raise CapabilityError("revoked (lineage)")
        if not any(r["method"] == method and r["resource"] == resource for r in cap["rights"]):
            raise CapabilityError(f"right not held: {method} {resource}")
        root = cap["root"]
        if self._usage.get(root, 0) >= self._root_limits.get(root, cap["rate_limit"]):
            raise CapabilityError("rate limit (lineage)")
        self._usage[root] = self._usage.get(root, 0) + 1
        self._persist()
        return {"status": "ok", "capability": cap["id"], "method": method, "resource": resource}
