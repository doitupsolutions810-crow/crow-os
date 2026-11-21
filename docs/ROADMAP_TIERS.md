# Tiered Prerequisite Map

Each tier lists the named gaps that gate it, and the exit criterion
that proves the tier is real. Gap IDs follow the Gap Directory in the
Orchestration report (GAP-001..GAP-018).

## Tier 1: Sovereign solo (team-of-one fleet at 24/7)

Prerequisites:
- GAP-001 dual AVRONE trunk closed: one active writer, the loser a
  read-only mirror (W0; blocked until owner authorizes the freeze)
- GAP-002 tree diff (done 2026-11-21: 232 identical / 89 diverged /
  46+369 unique; canonical verdict: avrone-daemon)
- GAP-003 the 38-file Avrone-only crow-os-workspace application
  migrated intact, tests green
- GAP-013 / GAP-014 / GAP-015 security gates fixed and gated
  (PR: fix/security-gates-gap-013-014-015)
- GAP-010 ledger and secret regression suite required before any move
- GAP-004 one source of truth for CONE/heat/psi with twin-side parity
  tests
- GAP-009 + GAP-011 zombies archived read-only with redirect READMEs

Exit criterion: 40 surfaces reduced to roughly 12; one trunk; local
verification green; every move receipted.

## Tier 2: Receipt-backed work (verifiably outsourced agent labor)

Prerequisites:
- GAP-006 attested-history absorbed as the kernel receipt bus
- GAP-016 evidence schema reconciled across sources
- signing defaults to externally provisioned key material (GAP-015
  follow-through): production homes reject dev secrets
- a receipt verifier that outsiders can run without holding the
  private key (Ed25519 verifier path from signers.py)
- receipt export: human-readable attested transcripts

Exit criterion: a third party can verify an audit trail of agent work
without access to the kernel's secrets.

## Tier 3: Owner-class platform (multi-tenant custody)

Prerequisites:
- tenant-scoped kernel homes: isolated capability scopes, keys and
  ledgers per tenant
- GAP-012 private product repos kept as satellites or absorbed with an
  explicit owner decision and clean ACL boundaries
- quotas and resource boundaries per tenant (extends GAP-014's atomic
  accounting)
- governance acknowledged by a second reviewer: no single point of
  silent authority
- GAP-018 simulation-governance properties (identity, state isolation,
  collision resistance, resource bounds, replayable randomness,
  linearizable mutations) as a tested package

Exit criterion: two tenants can run agent fleets on one kernel without
either gaining the other's authority or reading the other's history.

## Tier 4: Federated kernels (cross-org custodianship)

Prerequisites:
- GAP-008 CONTROL12 boundary settled: workers as plugins vs satellite
  with a declared deployment contract
- cross-kernel receipt verification: peers verify each other's ledger
  chains via Ed25519 identity (federation.py is the working embryo)
- scoped capability exchange: a peer may be granted a capability, never
  a master key
- federation failure drills: dispatch/receive under peer deletion,
  stale identity, replay (the Round 25/26/28/29 race lessons)

Exit criterion: agent work crosses an organizational boundary with
verifiable provenance on both sides and zero shared secrets.
