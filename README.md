# Crow OS

A personal agent operating system, built as the consolidated form of the
Crow / DoItUp experiments audited on 2026-11-15.

The audit's conclusion drives the design: the agent runtime was becoming
real, while the trust layer was becoming a description of itself. Crow OS
inverts that priority order:

1. **Trust verifies itself.** The ledger is append-only, hash-chained and
   sequence-checked; verification runs in the test suite and CI from day
   one. A missing ledger is an error, never a silent green.
2. **Capabilities bind exactly.** A right names a method and a resource.
   A `GET` right can never satisfy a `DELETE` requirement. Revoking a
   parent revokes its whole lineage. Rate limits are keyed by lineage, so
   attenuation cannot reset them.
3. **Work completes by role, not by count.** A job advances only when the
   role that owns the current step signs off. Ten identical events from
   one role complete nothing.
4. **State survives restarts.** Jobs and events live in the ledger on
   disk, not in an in-memory map.
5. **One source of truth.** One kernel, one ledger format, one capability
   model. No diverged generations.

## Layout

- `src/crow_os/ledger.py` — strict attested ledger
- `src/crow_os/capabilities.py` — capability broker (HMAC dev signer;
  Ed25519 signer interface is the production swap)
- `src/crow_os/workflow.py` — role-checked job chain
- `src/crow_os/runtime.py` — kernel tying ledger + capabilities + workflow
- `src/crow_os/cli.py` — `crow` command line

## Run

```bash
PYTHONPATH=src python3 -m crow_os demo --home /tmp/crow-home
PYTHONPATH=src python3 -m crow_os verify --home /tmp/crow-home
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

Status: v0.1 kernel. Deliberately small — the kernel proves the trust
invariants first; LLM agents, fleet dispatch and federation plug in
behind the same capability and ledger contracts.
