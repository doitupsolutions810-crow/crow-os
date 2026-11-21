# Security Gate GAPS 013 / 014 / 015

Findings from the crow-os source review of 2026-11-21 became failing
tests in this round. Where the fix is in this repository, it ships in
the same change; regression tests are the permanent gate.

## GAP-013 — parent capability validation

Finding: issue() indexed the lineage map directly and read parent
fields without checks, so a malformed or foreign-lineage parent
escaped the authorization boundary as KeyError, and an expired parent
could still spawn children (clamped to an already-past expiry instead
of being refused).

Fix (src/crow_os/capabilities.py):
- parent is validated before any use: required fields, rights shape,
  signature (GAP-013)
- explicit refusals: expired parent, foreign lineage (parent id not in
  this broker's lineage map), revoked parent — all CapabilityError
- malformed capability shapes inside authorize() are likewise refused
  with CapabilityError, never with a raw KeyError

## GAP-014 — concurrent rate-limit consumption

Finding: authorize() read the usage count, checked the limit,
incremented and persisted without holding the lock across the whole
operation, so concurrent requests could race between check and
increment.

Fix (src/crow_os/capabilities.py):
- the broker lock is held across reload -> check -> increment -> persist
- the file-backed state reloads inside that section, so consumption
  persisted by another process counts too
- a POSIX advisory flock (fcntl) on the state file serializes brokers
  across processes where fcntl exists (thread-level atomicity elsewhere)

Gate test: concurrent threads fire authorize() against a capability
with a small rate limit; successful authorizations must never exceed
the limit (tests/test_gate_security_gaps.py).

## GAP-015 — default signing-key initialization

Finding: Kernel derived the default HMAC secret from the resolved home
directory path (sha256("crow-os:" + home)), which is reproducible by
anyone who knows the path convention. Ed25519 infrastructure existed
(federation identity) but the broker ledger chain still initialized
from the deterministic secret.

Fix (src/crow_os/runtime.py):
- production mode (CROW_OS_MODE=production, or Kernel(home,
  production=True)) refuses to start without CROW_OS_SIGNING_KEY:
  externally provisioned hex or base64 key material of at least 32
  bytes; the path-derived secret is explicitly a dev-only convenience
  and production never derives it
- with a provisioned key, the kernel hands it to the ledger and broker
  as before, so ledger chains and capability signatures work unchanged
- the Ed25519 federation identity remains the production-facing
  asymmetric identity; the broker signing key is the operator's
  custodial secret
