# Crow OS + AVRONE + local Ollama evidence registry
Date: 2026-11-15
Target repository: https://github.com/doitupsolutions810-crow/crow-os
Integration branch: integration/avrone-local-ollama-2026-11-15
Base: dfe080b35c8eacb597207809930bb264a6d115fc
Upstream AVRONE hardening: https://github.com/doitupsolutions810-crow/Avrone/pull/26
Upstream AVRONE observed head: fb10a348f1c544f1b903220247de7e82be0e2c79

## Source-to-result registry

| Resource | Claim and check | Evidence state |
| --- | --- | --- |
| [Local Ollama client](../../src/crow_os/llm.py) | Bounds prompt, tags, reply and NDJSON frame bytes; checks error and terminal frames; preserves one-message legacy stub compatibility | Source committed |
| [Ollama tests](../../tests/test_ollama_hardening.py) | 22 stdlib localhost mock-server tests for partial streams, malformed responses, output limits, endpoint validation, callbacks and model presence | 22 passed against isolated source in sandbox; full checkout CI required |
| [AVRONE importer](../../src/crow_os/avrone.py) | Explicit read-only JSONL ingress; validates file/link/line/post budgets; filters tombstones; imports only allowlisted event fields as untrusted observations | Source committed |
| [AVRONE importer tests](../../tests/test_avrone_import.py) | 17 sandbox stub-ledger tests covering duplicates, parse failures, symlink/hardlink, size, tombstones and no executable authority | 17 passed against isolated source; full checkout CI required |
| [Crow OS runtime](../../src/crow_os/runtime.py) | Existing kernel remains the sole capability and workflow owner; no replacement runtime | Existing source unchanged |
| [Crow OS worker](../../src/crow_os/worker.py) | Existing worker catches LLMUnavailable and records failed role instead of completing | Existing source unchanged; tested through new kernel integration fixtures in CI |
| [CLI](../../src/crow_os/cli.py) | Adds ollama-check --smoke and explicit avrone-import --path | Committed, full-package CI required |
| [Module entrypoint](../../src/crow_os/__main__.py) | Enables documented python -m crow_os invocation | Source committed |
| [End-to-end stub integration](../../tests/test_crow_ollama_integration.py) | Tests kernel workflow success/failure, CLI smoke, signed ledger evidence import and cross-process deduplication with localhost mock Ollama | Added to existing unittest suite; hosted CI pending |
| [CI](../../.github/workflows/ci.yml) | Two Python legs, 3.11 and 3.12; runs scripts/check.sh including all tests | Existing configuration inspected |
| [Ollama API specification](https://docs.ollama.com/api/chat) | /api/chat stream, message, done, done_reason semantics | Official vendor docs checked |
| [Ollama installed-models specification](https://docs.ollama.com/api/tags) | /api/tags returns installed models | Official vendor docs checked |

## Local run evidence and limits

Sandbox execution of isolated source and isolated stub-ledger harness:
- python -m unittest discover -s tests -q
- 39 tests, 39 passed, approximately 9.6 seconds.
- No external network, model download or physical local Ollama instance was used.
- The isolated environment did not contain the entire Crow OS checkout.
- GitHub branch contents and the end-to-end Crow OS tests require exact-head CI verification before promotion.
- The sandbox could not git-clone from github.com because host DNS resolution failed.
- Desktop Commander reported no connected computer, so the user's Mac and installed Ollama models could not be inspected.
- Imported AVRONE entries are evidence-only; there is no privileged execution, automatic autonomous swarm dispatch or role-completion mapping.

## Promotion criteria

- Both Python 3.11 and 3.12 full check jobs pass on exact integration-branch head.
- Existing ledger/capability and probe harness suites stay green.
- Integration tests show partial Ollama output marks a job failed and successful output reaches deterministic governance.
- Local operator checks actual Ollama model health and one smoke inference on their machine.
- No merge or deployment until these checks pass.

## v0.9 integration preservation

This branch was merged with main 786db364f033e06d593a36780d630bcbbbe8c059. This keeps the native v0.9 loopback proxy bypass, installed-model selection, error reporting, coder repair and its tests. Existing vendor behavior is not intentionally removed. Full CI on the merge head is pending.

## Concurrency hardening — c700de1f04b6f1b1e1e0bcb2e8645cf60963a40d

The AVRONE importer now guards the full ledger verify/read/check/append critical section with a POSIX flock on a single-link no-follow sidecar file (and uses a process-local lock for test adapters without a ledger path). This prevents two independent Crow OS processes from both accepting the same AVRONE event during concurrent imports. New isolated coverage includes 8 competing threads and a hostile lock symlink; an added full-kernel two-process integration check remains pending in GitHub CI.

**Isolated evidence:** 39/39 tests passed with Python 3.13.5 on a reconstructed module harness. **Integration:** 5 full-package tests await hosted CI. No real local Ollama model was queried.

## Subsequent upstream reconciliation

The integration branch now merges Crow OS upstream main `9083a1e8e1feb2daf48d0eaf2656dae0635d6a3d`, which adds device management, host-status display and sub-agent spawning. Those modules and new probe harnesses are retained from main. The hardened Ollama client remains the transport overlay to protect streaming and bound resources. Exact-head CI remains pending.


## Extended importer integrity round — 2026-11-15

Baseline: GitHub PR #1, after reconciliation with upstream v0.10 at commit
9083a1e8e1feb2daf48d0eaf2656dae0635d6a3d.

| Resource | Regression / evidence | Status |
| --- | --- | --- |
| [AVRONE importer](../../src/crow_os/avrone.py) | Fixed exactly 100 posts followed by valid tombstones (marker not a post); validated contradictory immutable identities before any new ledger append | Source committed |
| [New edge tests](../../tests/test_avrone_edge_v2.py) | 17 new unittest cases; 300 deterministic evidence mutation scenarios; 12 simultaneous importers; storage failure and replay; prior-ledger corruption; conflict fail-closed | 17/17 locally passed against edited real importer module |
| [Existing Ollama and AVRONE tests](../../tests/test_ollama_hardening.py) | Original 39 isolation tests still run alongside new regression module | Local 56/56 passed on Python 3.13.5 |
| [PR checks](https://github.com/doitupsolutions810-crow/crow-os/pull/1/checks) | Python 3.11 and 3.12 full repository with probe harnesses | Exact-head CI required; not certified |

### Local executable evidence

Local Python 3.13.5 command:
\`PYTHONPATH=src python -m unittest discover -s tests -v\` in the isolated
Crow OS module reconstruction. Result: **56 tests run, 56 passed**. This
includes 300 seeded post-mutation iterations, a 12-thread import race, and
recovery after an injected storage error. The earlier unpatched importer
rejected 100 posts followed by one valid tombstone and silently skipped
a conflicting duplicate; both defects were reproduced before changing
the implementation.

Limits: This was **not** a full git clone, because github.com DNS is
unavailable in the sandbox. The physical user's Ollama, full Crow OS
package (worker, server, device runtime), multi-process signed ledger,
and Python 3.11 / 3.12 CI have not been directly executed in the sandbox.

Release decision: **HOLD** until exact-head CI and operator-local model smoke.


## Multiprocess evidence verification — 2026-11-15

| Resource | Exact claim and scope | Outcome |
| --- | --- | --- |
| [File-backed process test](../../tests/test_avrone_multiprocess_edges.py) | Eight Python spawn processes imported 15 distinct events from one source into a common file-backed ledger adapter; exactly one process admitted each event | Passed on local Python 3.13.5 |
| [Replay under independent processes](../../tests/test_avrone_multiprocess_edges.py) | Two fresh processes replayed a previous four-event import, yielding zero new admissions and eight duplicate observations | Passed |
| [Unsafe filesystem locking](../../tests/test_avrone_multiprocess_edges.py) | Hardlink and directory at .avrone-import.lock fail closed without appending | Passed |
| [Corrupt predecessor](../../tests/test_avrone_multiprocess_edges.py) | A prior malformed JSON line in the file-backed test ledger blocks new evidence | Passed |
| [Source implementation](../../src/crow_os/avrone.py) | Cross-process flock and preflight contradiction checking; marker-aware 100-post budget | Reviewed against committed source |
| [GitHub CI](https://github.com/doitupsolutions810-crow/crow-os/pull/1/checks) | Full Crow OS v0.10, existing probe harnesses, 3.11/3.12 signed ledger and Ollama integration | Awaiting exact-head runner completion |

Local executable command in the isolated Crow OS modules:
\`PYTHONPATH=src python -m unittest discover -s tests -q\`.
Result: **61 tests, 61 passed, 12.766 seconds, Python 3.13.5**.
Previous baseline: 39/39; this round contributed 22 new individual
regression tests, including one with 300 deterministic data-mutation
iterations and one with eight independent spawn processes.

The file-backed process adapter is NOT Crow OS's actual signed Ledger;
its purpose is to test the import lock independently. The full-kernel
two-process test already in tests/test_crow_ollama_integration.py must
pass in GitHub CI before claiming signed-ledger multi-process success.
No user's physical Ollama service or model was accessed.
No production rollout or merge was performed.

Release decision remains **HOLD**.
