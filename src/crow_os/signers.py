"""Capability signers.

One contract, two backends:
- HmacSigner: stdlib dev signer (shared secret).
- Ed25519Signer: production signer (requires the `cryptography` package).
  Verification needs only the public key, so a verifier never holds the
  private key — the property TrainLLM's governance lacked when it trusted
  the key embedded in the attestation itself. Here the broker is
  constructed with a trusted signer/verifier; capabilities carry no keys.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any, Protocol


def canon(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


class Signer(Protocol):
    name: str

    def sign(self, body: dict[str, Any]) -> str: ...
    def verify(self, body: dict[str, Any], sig: str) -> bool: ...


class HmacSigner:
    name = "hmac-sha256"

    def __init__(self, secret: bytes):
        if len(secret) < 16:
            raise ValueError("secret too short")
        self._secret = secret

    def sign(self, body: dict[str, Any]) -> str:
        return hmac.new(self._secret, canon(body).encode(), hashlib.sha256).hexdigest()

    def verify(self, body: dict[str, Any], sig: str) -> bool:
        return hmac.compare_digest(self.sign(body), sig)


class Ed25519Signer:
    name = "ed25519"

    def __init__(self, private_key=None, public_key=None):
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey,
        )
        if private_key is None and public_key is None:
            private_key = Ed25519PrivateKey.generate()
        self._private = private_key
        self._public = public_key or private_key.public_key()

    @classmethod
    def generate(cls) -> "Ed25519Signer":
        return cls()

    @classmethod
    def verifier(cls, public_key) -> "Ed25519Signer":
        """A verify-only signer holding just a public key."""
        obj = cls.__new__(cls)
        obj._private = None
        obj._public = public_key
        return obj

    def public_key(self):
        return self._public

    def sign(self, body: dict[str, Any]) -> str:
        if self._private is None:
            raise ValueError("verify-only signer cannot sign")
        return self._private.sign(canon(body).encode()).hex()

    def verify(self, body: dict[str, Any], sig: str) -> bool:
        from cryptography.exceptions import InvalidSignature
        try:
            self._public.verify(bytes.fromhex(sig), canon(body).encode())
            return True
        except (InvalidSignature, ValueError):
            return False
