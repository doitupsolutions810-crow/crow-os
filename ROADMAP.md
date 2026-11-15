# Crow OS roadmap

## v0.1 — kernel (done)
Strict ledger, exact capabilities, role-checked workflow, demo runtime,
CLI, tests, CI. Every invariant is a regression test named after the
audit failure it prevents.

## v0.2 — real agents (done)
- LLM worker (Ollama) behind per-step EXECUTE capabilities; the gate is
  checked before the model is called, and an unreachable model fails the
  job closed — no synthetic passes
- Ed25519 capability signer alongside the HMAC dev signer; verify-only
  brokers hold just a public key
- Still open: persistent broker state (revocations, lineage, usage) in
  the ledger; Avrone-style HMAC fleet dispatch arrives with v0.4

## v0.3 — home screen (shipped)

- `crow serve`: the OS Home Screen — chat with the squad, a live
  operations index, expandable per-agent thinking cards rendered from
  the signed ledger's own event stream, sandbox bar, code/app views
  and a ledger inspector. Runs execute on the real kernel in threads;
  a crashed run fails its job in the ledger instead of stranding it.
- Persistent broker state is signed and written atomically under a
  lock (concurrent runs cannot race it).

## v0.4 — wiring & stability (shipped)

- One chain implementation: the server pre-creates the job and the
  kernel executes it — demo and LLM paths both accept a pre-created
  job_id, so the run API is uniform and no step logic is duplicated.
- `crow verify` now checks entry signatures when the home has a
  secret (previously it verified digests only).
- `scripts/check.sh` runs the unit suite plus every probe harness;
  CI runs it too.

## v0.5 — fleet
- Node registry + signed dispatch between Crow OS homes
- Agent-home migration as a ledger-attested operation

## v0.6 — federation
- mTLS peer links (living-intermediate's verified design), digest-checked
  ingest — no unauthenticated peers

## Explicitly not carried over
- Self-signed governance attestations (TrainLLM pattern)
- Identity fallbacks that claim a production trust domain
- In-memory job state
- Any component whose README claims more than its tests prove
