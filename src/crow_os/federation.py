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

Transport is plain HTTP carrying the signature; the identity and
verification layer is the substance (mTLS can wrap the same
endpoints later without changing the trust model).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.request
from pathlib import Path
from typing import Any

from .fs import CrowFS, FSError

PEERS_FILE = "/system/peers.json"
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
        except (InvalidSignature, ValueError):
            return False


class PeerStore:
    def __init__(self, fs: CrowFS):
        self.fs = fs

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
    """
    if not isinstance(payload, dict):
        raise FederationError("body must be an object")
    sig = payload.get("sig")
    body = {k: v for k, v in payload.items() if k != "sig"}
    for field in ("from", "agent", "objective", "ts"):
        if field not in body:
            raise FederationError(f"missing field: {field}")
    peer = peers.get(body["from"])  # unknown peer -> FederationError
    if abs(time.time() - float(body["ts"])) > MAX_SKEW:
        raise FederationError("stale or future timestamp (replay window)")
    if not sig or not Identity.verify(peer["pubkey"], body, sig):
        raise FederationError("signature does not verify")
    return peer
