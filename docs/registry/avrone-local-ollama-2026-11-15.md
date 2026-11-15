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
| [AVRONE importer tests](../../tests/test_avrone_import.py) | 15 sandbox stub-ledger tests covering duplicates, parse failures, symlink/hardlink, size, tombstones and no executable authority | 15 passed against isolated source; full checkout CI required |
| [Crow OS runtime](../../src/crow_os/runtime.py) | Existing kernel remains the sole capability and workflow owner; no replacement runtime | Existing source unchanged |
| [Crow OS worker](../../src/crow_os/worker.py) | Existing worker catches LLMUnavailable and records failed role instead of completing | Existing source unchanged; tested through new kernel integration fixtures in CI |
| [CLI](../../src/crow_os/cli.py) | Adds ollama-check --smoke and explicit avrone-import --path | Committed, full-package CI required |
| [Module entrypoint](../../src/crow_os/__main__.py) | Enables documented python -m crow_os invocation | Source committed |
| [End-to-end stub integration](../../tests/test_crow_ollama_integration.py) | Tests kernel workflow success/failure, CLI smoke and signed ledger evidence import with localhost mock Ollama | Added to existing unittest suite; hosted CI pending |
| [CI](../../.github/workflows/ci.yml) | Two Python legs, 3.11 and 3.12; runs scripts/check.sh including all tests | Existing configuration inspected |
| [Ollama API specification](https://docs.ollama.com/api/chat) | /api/chat stream, message, done, done_reason semantics | Official vendor docs checked |
| [Ollama installed-models specification](https://docs.ollama.com/api/tags) | /api/tags returns installed models | Official vendor docs checked |

## Local run evidence and limits

Sandbox execution of isolated source and isolated stub-ledger harness:
- python -m unittest discover -s tests -q
- 37 tests, 37 passed, approximately 9.6 seconds.
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
