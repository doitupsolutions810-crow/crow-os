# Crow OS roadmap

## v0.1 — kernel (done)
Strict ledger, exact capabilities, role-checked workflow, demo runtime,
CLI, tests, CI. Every invariant is a regression test named after the
audit failure it prevents.

## v0.2 — real agents
- LLM worker behind the EXECUTE capability (Avrone daemon's gateway
  contract: HMAC dispatch, nonce replay protection, allowlist)
- Ed25519 capability signer replacing the HMAC dev signer
- Persistent broker state (revocations, lineage, usage) in the ledger

## v0.3 — fleet
- Node registry + signed dispatch between Crow OS homes
- Agent-home migration as a ledger-attested operation

## v0.4 — federation
- mTLS peer links (living-intermediate's verified design), digest-checked
  ingest — no unauthenticated peers

## Explicitly not carried over
- Self-signed governance attestations (TrainLLM pattern)
- Identity fallbacks that claim a production trust domain
- In-memory job state
- Any component whose README claims more than its tests prove
