# W1: PR Dependency Train, Great Scale Parity, and Repo Triage

Wave: **W1, candidates 2-4**. Read-only analysis; no source files were modified in any repository.
Companion to `docs/W1_DIVERGENCE_ANALYSIS.md` (candidate 1).

## 1. avrone-daemon PR dependency train (candidate 2)

Census from the PR API (state=all, 2026-11-21): **88 PRs exist** (numbers 1-89; #73 is not returned
by the listing). **84 open, 2 merged, 2 closed unmerged. The only merged PRs in the repo's history
are #19 and #20** (fusion blackboard + delivery contract, both landed on main 2026-11-10).

The open set is not one train but **three lineages**:

**Lineage A - evidence-contract stack (the original [Stacked] chain).**
#2 `integration/evidence-contract` -> #3 flakeprobe calibration -> #4 fleet subprocess fix ->
#5 Great Scale evidence receiver. All four target chained branches, all stale against current main.

**Lineage B - "port the trial receiver onto current mainline" (five generations of the same intent).**
#6, #11, #12 (draft), #14 (draft), #15, #16 (v1/v2 duplicate pair), #17 (draft, duplicate of #16),
#33, #68, #82. The last generation is still alive: #82's description is the current intent; #6/#33/#68
are superseded by it. Note #16 vs #15: same title, different branch suffix (`-2026-11-08` both, v2 vs v1).
#34 and #83 stack on top of the `integration/trial-receiver-mainline` branch lineage.

**Lineage C - hardening rounds 181-227 (the big test-driven queue).**
#28-#32 (181-185) stack linearly from main.
#34-#57 (186-205) continue from #33's base.
Then the tree **forks three ways** from the same rounds:
- lane 1: #55-#62 (201-source-trigger -> 206-bounded-migration) -> #63-#67 (207 -> 211-crossprocess-wal-umask), tip #67;
- lane 2: #48-#50 (196-consistent-audit-snapshot, 197-streaming-trial-evidence);
- lane 3 (the durable trunk): #58-#80 (202-primary-key -> ... -> 222-retained-receipt-causal-order), then #86-#89 (223-crash-consistency -> 226-wal-snapshot-checkpoint, tip #89, created 2026-11-21);
- side: #81 (223-event-sequence-highwater) also stacks on 222, but is a *different* round 223 than #86's - a round-number collision.
#83-#85 (225-227) stack on #82's integration branch.

Round-number collisions found: **196 (x2), 201 (x2), 202 (x2), 203 (x2), 204 (x2), 223 (x2)** -
six pairs of distinct PRs claiming the same round, from different lanes.

**Operational read:**
1. The hardening queue (~50 PRs) is additive test-file work; each links on its parent. The cheapest
   collapse is: merge the surviving linear trunk in order (#58-#67 trunk pieces and #69-#80, then
   #86-#89) after one CI green, and **close the duplicated lane heads** (#55, #48, #50, #81, and the
   dead mainline generations #6/#33/#68) recording which lane won.
2. Lineage A + old receiver drafts (#2-#5, #6, #7, #9, #11-#18, #21-#25, #27) should be closed as
   superseded or re-based; the surviving receiver work is #82 (+ its stack #83-#85).
3. Nothing in the open set is required for the crow-os consolidation; main has already absorbed the
   daemon hardening directly (that is what the W1 diff measured).

## 2. Great Scale math parity contract (candidate 3)

**Reference:** `great-scale/public/residual.cjs` at `d5ce13f1` ("single source of truth for the
logarithmic golden cascade").
**Peer:** `avrone-daemon/avrone/greatscale.py` (blob `65d7f543`, main `3a1fbc19`).
Third instance: `GreatScale-Avrone-Grok-Fusion-Bridge` (web/bridge packaging).

Constants, both sides, identical: `PHI = (1+sqrt(5))/2`, `PHI_INV = PHI-1`,
`CONE = PHI_INV^2 = phi^-2 ~ 0.381966`, `FIXED = 4.5`, `RESIDUAL_PARTITION = (4,3,1)`,
`DUAL_ATLAS = {4: Watch, 3: Observer, 1: Forward}`.

Function-by-function parity (Python port vs JS spec):

| Function | JS spec | Python peer | Parity |
|---|---|---|---|
| heat(h) | clamp, x(1-x) | same + NaN pass-through (round 55) | match (Python fixes a JS-visible NaN defect) |
| residual_count(S) | 8 - 4S | same | match |
| cascade_lambda(n) | PHI^-n | same | match |
| log_coordinate(S) | 4S | same | match |
| psi(S) | CONE + S | same | match |
| cone_compliant(S) | psi >= CONE - 1e-12 | same | match |
| atlas_role(block) | DUAL_ATLAS[block] \|\| null | .get(block) | match |
| from_topological_saturation(tau) | S=(1-tau^2)/4 + full block | same fields | match |
| soft_contract(x,k) | ((x%9)+9)%9 contraction | same | match |

Numeric verification of the shared coordinate (executed, not asserted):
`CONE = 0.3819660112501052 = (3-sqrt(5))/2` within 1e-15; `heat(1/2) = 1/4` (peak);
4S = 1 at the peak; heat symmetric (heat(p)=heat(1-p)); psi(floor)=CONE; tau=1 sits on the floor,
tau=0 carries maximal S=1/4; a p=1/200 defect needs **598 runs** for 95% detection; p=0.01 needs
**299 runs** (correcting an eyeballed 298: 1-0.99^298 = 0.94996 < 0.95).

**Contract ruling:** the daemon's `greatscale.py` is a **faithful superset** of `residual.cjs`:
identical shared coordinate, plus an additive AVRONE evidence channel (`runs_to_detect`,
`evidence_snapshot`, orientation channel) that the upstream spec does not need. The Python-only
guards (NaN pass-through, `log1p` precision, integer-only counts, `MAX_RUNS` cap) are documented
defect fixes, not divergences. Parity verdict: **PASS; daemon version is the reference for any
future consumer.** The parity contract to enforce going forward: any change to the shared
coordinate must touch `residual.cjs` and `greatscale.py` in the same change-set, with this file
cited in the PR.

## 3. Repo triage / mass-archive plan (candidate 4)

Census (search API, 2026-11-21): **40 public+private repos**. Classification:

**Keep (the platform):** `crow-os`, `attested-history`, `avrone-daemon`, `Avrone` (until W1 tier-1
ports land and the owner flips it read-only), `great-scale`, `GreatScale-Avrone-Grok-Fusion-Bridge`,
`capability-authority`, `capability-plane`, `living-intermediate-control-plane`,
`control12-lattice-ops`, `control12-workspace`, `control704-proxy-system`, `CycleKernal`,
`grok-bot-`, `swarm-vm-runner`, `Aiminer`, `v0-ai-agent-for-servers`, `TrainLLM`.

**Archive now - empty or throwaway, zero risk (12):** `ghostshell` (0 KB), `avrone-control-room`
(0 KB), `sacp-platform` (0 KB), `cyclekernel-attestpipe_q6-platform-upgrade-2` (0 KB),
`onyx-pine-mint-pearl` (0 KB, private), `github-api-automation` (6 KB),
`UnifiedToolStackApplication-utsp-platform` (1 KB), `artifact-hosted-demo` (13 KB),
`cleanroom-agent-system` (18 KB), `seed-platform-v10x-docs` (22 KB), `trinity-core` (29 KB),
`code` (41 KB). All untouched for months (oldest push Aug/Sep/Jan), no stars, no opens.

**Archive in a second pass - private/one-shot, look once then archive (6):** `wallaby-tutorial`,
`Understanding-Uploaded-Files_-pasted_content-1-`, `x-near-me`, `cyclekernel-showcase` (Manus
showcase, is_template, idle since January), `pilot-cloud-finch-rose`, `wolf-pixel-bolt-frost`
(16 MB, single week of activity in October).

**Decide (2):** `ghostshell-docs` (idle 10 months, 1 open issue: close or keep as doc outlet),
`local-creator-radar` (private, idle 2 months).

**Execution status:** this session's GitHub toolset exposes no repository-archive capability
(there is no update-repository-settings tool, and archive is an admin API action), so the flips
must be done by the owner: Settings -> General -> Danger Zone -> Archive this repository.
Archive is reversible by the owner. 12 repos are zero-risk immediately; that is the list above.
