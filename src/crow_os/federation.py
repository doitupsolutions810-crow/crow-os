"""Federation (v0.15) — Crow homes that can work together.

Each home has an Ed25519 identity: a keypair generated once and kept
in the home directory (identity.ed25519, mode 0600). Homes register
each other as peers (name + URL + public key) in /system/peers.json.

Dispatch is a signed request: the sender signs the canonical body
{from, agent, objective, ts} with its private key; the receiver
verifies against the REGISTERED public key for that peer name,
refuses anything unsigned, forged, stale (>120s old) or from an
unknown peer, and only then runs the job — as a normal governed run
on its own ledger. Both sides attest: federation.dispatched on the
sender, federation.received on the receiver, each naming the other's
job. Verification needs only public keys, so no home ever holds
another home's secret.

Replay protection (v0.16.2): a signed body is only honoured once
inside the 120s window. The receiver keeps a seen-request store
(/system/federation-seen.json, persisted in CrowFS so a restart
inside the window does not reopen it), keyed by a hash of the
canonical body + signature and pruned once the SIGNED timestamp is
older than MAX_SKEW (v0.16.4: pruning by receipt time let a
future-dated body outlive its entry — see check_replay). The key is
the full body, not the timestamp: two distinct objectives sharing
one ts must both still run. A replay is refused 401 before the
receipt is attested or the job runs.

Transport is plain HTTP carrying the signature; the identity and
verification layer is the substance (mTLS can wrap the same
endpoints later without changing the trust model).
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any

from .fs import CrowFS, FSError

PEERS_FILE = "/system/peers.json"
SEEN_FILE = "/system/federation-seen.json"
MAX_PEERS = 32
MAX_SKEW = 120  # seconds


class FederationError(Exception):
    pass


def _slug(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(name).lower()).strip("-")
    return slug or "peer"


class Identity:
    """This home's Ed25519 identity (requires the cryptography pkg)."""

    def __init__(self, home: Path, name: str):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey,
        )
        self.name = name
        key_file = home / "identity.ed25519"
        if key_file.exists():
            raw = bytes.fromhex(key_file.read_text().strip())
            self._private = Ed25519PrivateKey.from_private_bytes(raw)
        else:
            self._private = Ed25519PrivateKey.generate()
            raw = self._private.private_bytes(
                serialization.Encoding.Raw,
                serialization.PrivateFormat.Raw,
                serialization.NoEncryption())
            key_file.write_text(raw.hex())
            os.chmod(key_file, 0o600)
        self._public = self._private.public_key()
        pub_raw = self._public.public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        self.pubkey_hex = pub_raw.hex()
        self.fingerprint = hashlib.sha256(pub_raw).hexdigest()[:16]

    def sign(self, body: dict[str, Any]) -> str:
        from .signers import canon
        return self._private.sign(canon(body).encode()).hex()

    @staticmethod
    def verify(pubkey_hex: str, body: dict[str, Any], sig_hex: str) -> bool:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PublicKey,
        )
        from .signers import canon
        try:
            pub = Ed25519PublicKey.from_public_bytes(bytes.fromhex(pubkey_hex))
            pub.verify(bytes.fromhex(sig_hex), canon(body).encode())
            return True
        except (InvalidSignature, ValueError, TypeError):
            return False


class PeerStore:
    def __init__(self, fs: CrowFS):
        self.fs = fs
        # Round 22: check_replay is read-check-write; without this lock
        # two threads serving the same signed dispatch both read a store
        # that does not yet contain the key and both run the job.
        self._replay_lock = threading.Lock()

    def _load(self) -> dict[str, Any]:
        try:
            data = json.loads(self.fs.read_file(PEERS_FILE))
            return data if isinstance(data, dict) else {}
        except (FSError, json.JSONDecodeError):
            return {}

    def _save(self, peers: dict[str, Any]) -> None:
        self.fs.write_file(PEERS_FILE, json.dumps(peers, indent=2))

    def register(self, name: str, url: str, pubkey_hex: str) -> dict[str, Any]:
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            raise FederationError("peer url must be http(s)")
        try:
            raw = bytes.fromhex(pubkey_hex or "")
        except ValueError:
            raise FederationError("peer pubkey must be hex") from None
        if len(raw) != 32:
            raise FederationError("peer pubkey must be a 32-byte Ed25519 key (hex)")
        peers = self._load()
        pid = _slug(name)
        if pid not in peers and len(peers) >= MAX_PEERS:
            raise FederationError("peer limit reached")
        peers[pid] = {"name": pid, "url": url.rstrip("/"),
                      "pubkey": pubkey_hex.lower()}
        self._save(peers)
        return peers[pid]

    def get(self, name: str) -> dict[str, Any]:
        peer = self._load().get(_slug(name))
        if peer is None:
            raise FederationError(f"unknown peer: {name}")
        return peer

    def list(self) -> list[dict[str, Any]]:
        return sorted(self._load().values(), key=lambda p: p["name"])

    def remove(self, name: str) -> None:
        peers = self._load()
        pid = _slug(name)
        if pid not in peers:
            raise FederationError(f"unknown peer: {name}")
        del peers[pid]
        self._save(peers)

    def check_replay(self, payload: dict[str, Any]) -> None:
        """Refuse a signed federation request seen before in the window.

        Keyed by a hash of the canonical body + signature (the full
        request, not the timestamp — two distinct objectives sharing
        one ts must both still run). The store is persisted in CrowFS
        so a restart inside the 120s window does not reopen the hole,
        and entries are pruned once the SIGNED timestamp is older
        than MAX_SKEW. Call after verify_incoming and before the job
        runs, so a replay is refused before federation.received is
        attested. Raises FederationError on a replay.
        """
        from .signers import canon
        body = {k: v for k, v in payload.items() if k != "sig"}
        key = hashlib.sha256(
            (canon(body) + "|" + str(payload.get("sig") or "")).encode()
        ).hexdigest()
        now = time.time()
        # The whole read-check-write holds the lock (Round 22): the
        # server is threaded, and without it two identical dispatches
        # arriving together both pass. A corrupt store now fails closed
        # (Round 22, C4): swallowing JSONDecodeError and resetting to {}
        # let a replay run after the store was damaged. A missing file
        # (first run) is still an empty store — only a present-but-
        # unparseable file refuses.
        # Round 23: the stored value is the SIGNED timestamp, not the
        # receipt time. verify_incoming accepts future timestamps
        # within MAX_SKEW (clock skew), so a body dated now+110 is
        # still verifiable at receipt+130 — but an entry recorded at
        # receipt time would already be pruned by then and the replay
        # would run again. Pruning by the signed ts keeps the entry
        # until ts+MAX_SKEW, past which verify_incoming refuses the
        # body as stale anyway. Old stores hold receipt times; those
        # are epoch seconds too, so the same prune still applies.
        try:
            body_ts = float(body.get("ts"))
        except (TypeError, ValueError):
            body_ts = now
        if not math.isfinite(body_ts):
            # Round 25: a NaN ts is not a timestamp. Storing it would
            # write a value the prune pass (now - v <= MAX_SKEW, False
            # for NaN) drops on the next call, so the entry would never
            # block a replay. verify_incoming refuses non-finite ts
            # first; this keeps check_replay self-consistent for any
            # direct caller.
            body_ts = now
        with self._replay_lock:
            try:
                seen = json.loads(self.fs.read_file(SEEN_FILE))
            except FSError:
                seen = {}
            except json.JSONDecodeError:
                raise FederationError(
                    "federation seen-store unreadable; refusing dispatch"
                ) from None
            if seen and not isinstance(seen, dict):
                raise FederationError(
                    "federation seen-store unreadable; refusing dispatch"
                )
            seen = {k: v for k, v in seen.items()
                    if isinstance(v, (int, float)) and now - v <= MAX_SKEW}
            if key in seen:
                raise FederationError("replayed federation request")
            seen[key] = body_ts
            self.fs.write_file(SEEN_FILE, json.dumps(seen))


def signed_dispatch(identity: Identity, peer: dict[str, Any],
                    agent: str, objective: str,
                    timeout: float = 120.0) -> dict[str, Any]:
    """POST a signed run request to a peer; return its parsed reply."""
    body = {"from": identity.name, "agent": agent,
            "objective": objective, "ts": int(time.time())}
    payload = dict(body)
    payload["sig"] = identity.sign(body)
    req = urllib.request.Request(
        peer["url"] + "/api/federation/run", method="POST",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read().decode()).get("error", "")
        except Exception:  # noqa: BLE001
            detail = ""
        raise FederationError(f"peer refused ({e.code}): {detail}") from None
    except Exception as e:  # noqa: BLE001 - unreachable, DNS, timeout
        raise FederationError(f"peer unreachable: {e}") from None


def verify_incoming(peers: PeerStore, payload: dict[str, Any]) -> dict[str, Any]:
    """Validate a federation/run payload; return the peer record.

    Raises FederationError with the reason on any failure: unknown
    peer, stale timestamp, or a signature that does not verify
    against the registered public key.

    Round 25: field TYPES are part of the contract, checked here
    before anything else. verify_incoming previously passed
    body["ts"] straight to float() — a ts of None, a list or a
    dict raised TypeError, which the /api/federation/run handler
    does not catch, so the client saw a dropped connection instead
    of a refusal. A non-string objective passed verification and
    only crashed later in run_agent (objective.splitlines()) —
    after federation.received had already been attested, leaving a
    receipt in the ledger for a job that never ran. A numeric-string
    ts ("173...") was accepted and ran. And a NaN ts slipped both
    guards at once: abs(now - NaN) > MAX_SKEW is False, so the body
    never went stale, while the prune pass (now - v <= MAX_SKEW,
    also False for NaN) dropped its seen-store entry on the next
    call — so a captured NaN-ts dispatch replayed forever, not
    just inside the 120s window. Wrong-typed or non-finite fields
    are now refused 401 here, before any attestation or run.
    """
    if not isinstance(payload, dict):
        raise FederationError("body must be an object")
    sig = payload.get("sig")
    body = {k: v for k, v in payload.items() if k != "sig"}
    for field in ("from", "agent", "objective", "ts"):
        if field not in body:
            raise FederationError(f"missing field: {field}")
    for field in ("from", "agent", "objective"):
        if not isinstance(body[field], str):
            raise FederationError(f"field must be a string: {field}")
    # Round 26: "" is a string, so it passed the Round-25 type check —
    # but workflow.create_job refuses a blank objective, and it raises
    # WorkflowError AFTER federation_receive attests federation.received
    # (and the /api/federation/run handler does not catch WorkflowError),
    # so an empty-objective dispatch dropped the connection AND left a
    # receipt for a job that never ran. Blank fields refuse here, at
    # the federation boundary, before any attestation or run.
    for field in ("from", "agent", "objective"):
        if not body[field].strip():
            raise FederationError(f"field must be a non-empty string: {field}")
    ts = body["ts"]
    if isinstance(ts, bool) or not isinstance(ts, (int, float)):
        raise FederationError("field must be a number: ts")
    if not math.isfinite(ts):
        raise FederationError("field must be finite: ts")
    if not isinstance(sig, str):
        raise FederationError("signature does not verify")
    peer = peers.get(body["from"])  # unknown peer -> FederationError
    if abs(time.time() - float(ts)) > MAX_SKEW:
        raise FederationError("stale or future timestamp (replay window)")
    if not sig or not Identity.verify(peer["pubkey"], body, sig):
        raise FederationError("signature does not verify")
    return peer
