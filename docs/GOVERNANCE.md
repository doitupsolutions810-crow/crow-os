# Governance

## The golden rules

1. **Nothing moves without a receipt.** Every audit, merge decision,
   move and archive appends an entry to the receipt bus before it is
   considered done.
2. **Fail closed.** A step whose guard is missing (no diff, no tests,
   no backup branch) refuses to execute instead of degrading.
3. **One trunk, one writer.** While two trunks of the same product
   accept pushes, no other wiring step may run except those that close
   the split.
4. **Read-only by default.** Mutations require explicit owner
   authorization; analysis and preparation are always permitted.
5. **Pings are work.** Open issues are pinged with a decision request
   each heartbeat; two silent heartbeats make an item STALE, never
   closed.

## Roles

- Owner: authorizes writes; owns canon decisions (canonical trunk,
  satellite verdicts, hosting targets).
- Orchestrator: holds the Gap Directory, plans waves, escalates.
- Verifier: independent, fresh-context check of exit criteria; never
  the author of what it verifies.

## Mutation accounting

Every run reports a zero-mutation or receipted-mutation table. Analysis
performed without receipts is explicitly labeled observed-and-prepared.
