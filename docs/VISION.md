# Crow OS: Vision and Positioning

One kernel. One ledger format. One capability model. One receipt bus.
Every action attested; every agent governed; every claim verifiable
after the fact.

## What Crow OS is

Crow OS is a **self-auditing agent kernel**: an operating system whose
native instruction format is "capability-gated action plus attested
receipt". Agent runtimes (the AVRONE line) are execution packages the
kernel orchestrates and receipts, never implicitly trusts. Domain
payloads (Great Scale residual math, CycleKernal fusion) are versioned
libraries. CONTROL12 workers are optional fleet plugins. Product
surfaces are apps or satellite repos bound by the same contracts.

## Why attestation-first

The rare property in the agent ecosystem is not "agents that act"; it
is "agent work you can prove afterwards". Crow OS makes that property
the kernel's core: nothing imperative is trusted, everything imperative
is licensed and logged, and history is a signed append-only chain.

## The reach ladder

The platform advances in four tiers; every tier has named prerequisite
gaps in docs/ROADMAP_TIERS.md:

1. **Sovereign solo**: one owner safely running an agent fleet around
   the clock (repos, servers, tools) with fail-closed custody.
2. **Receipt-backed work**: agent output handed to humans or clients
   with cryptographic provenance; auditable, contractable, invoiceable.
3. **Owner-class platform**: tenants get their own capability scopes,
   keys and ledgers; Crow OS hosts other people's agent businesses.
4. **Federated kernels**: separate Crow kernels exchanging signed
   receipts and scoped capabilities across trust boundaries.

## The canon (per the declared consolidation)

- crow-os is the kernel root; it does not contain the runtime.
- The runtime (AVRONE) lives as a package with one active writer.
- The Great Scale math has one source of truth with parity tests.
- The capability layer is singular and fail-closed.
- attested-history is the kernel's receipt bus, not a satellite.
- Zombies and scaffolds are archived read-only with redirect notes.

## Non-goals

- A pure monorepo of all 40 repositories (product satellites and
  host/K8s workers have different deploy and compliance boundaries).
- Trust-the-agent designs: any feature that would bypass the
  capability broker or write to the ledger around its interface.
- Silent mutation: work without a receipt is work that did not happen.
