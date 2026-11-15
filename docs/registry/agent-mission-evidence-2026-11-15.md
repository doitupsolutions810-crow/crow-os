# Crow OS four-agent chat mission and custom-agent hardening

Date: 2026-11-15
Source: [Crow OS integration PR #1](https://github.com/doitupsolutions810-crow/crow-os/pull/1)
Public network scope: https://github.com/doitupsolutions810-crow/crow-os
Requested mission instruction:

> Continue Even Longer more robust Test against even more Edge cases and Fix Along the way

## Accountable source and result registry

| Resource | Claim / user requirement | Observable evidence |
| --- | --- | --- |
| [Four-agent mission client](../../scripts/edge_mission.py) | Explicitly creates or reuses four named agents with role-specific instructions; posts the requested prompt through /api/run and polls /api/jobs until terminal or bounded timeout | Source committed |
| [Mission HTTP regressions](../../tests/test_edge_mission.py) | Asserts four agent create requests, four chat submissions, exact prompt first line, model and network grants, reuse, no silent overwrite, failure and timeout reporting | Added to full Crow OS unittest suite |
| [Agent Registry](../../src/crow_os/agents.py) | Validates name, instructions, model, avatar and granted tools; exclusive multi-process lock prevents duplicate-name overwrites; validates IDs on read/delete and supports safe legacy records | Source committed |
| [Agent Registry and capability tests](../../tests/test_named_agent_hardening.py) | Concurrent naming, persistent custom instructions and avatar, invalid tool inputs, unsafe IDs, legacy corruption and delegated TOOL permission gate | Added to full Crow OS unittest suite |
| [Agent Worker](../../src/crow_os/worker.py) | Checks a signed TOOL agent.spawn capability for each actual delegation, not only a claim in agent metadata | Source committed |
| [HTTP server](../../src/crow_os/server.py) | Bounds JSON POST body before reading and refuses non-object payloads; upstream transcript routes retained | Source committed |
| [HTTP creation tests](../../tests/test_agent_http_admission.py) | End-to-end named agent creation with multiline instructions and grants; duplicate isolation and invalid requests returning 400 | Added to full Crow OS unittest suite |
| [Local mock mission trace](../../scripts/edge_mission.py) | Four named agent creations, four /api/run submissions and four terminal mock results with the scoped public-source URL | Executed on isolated local HTTP stub, NOT a real Ollama model |
| [Crow OS CI](https://github.com/doitupsolutions810-crow/crow-os/pull/1/checks) | Full Python 3.11 and 3.12, live HTTP integration, existing probe harnesses | Exact-head CI remains required |

## Local reproduction

The isolated sandbox exercised a local HTTP mock of Crow OS and a
filesystem-backed agent registry reproduction on Python 3.13.5:

- 9 mission tests passed: exact prompt, named agent setup, idempotence,
  non-overwrite, enabled and disabled network grants, timeout, failure and
  loopback-only endpoint restrictions
- 10 registry tests passed: named and persisted custom instructions, avatars,
  reserved names, malformed input, 16-thread and eight-process duplicate
  naming stress, symlink lock and corrupt definitions
- 4 mock job completions were observed (scripted fake HTTP responses;
  not a local Ollama inference or real Crow OS agent run)
- The new GitHub repository tests are a *separate* full-package test suite,
  not covered by these isolated 19 passes

## Execute against a real locally running Crow OS

Start local Ollama with an installed model, then start Crow OS on the same
machine (default loopback 127.0.0.1:8787). From the Crow OS repository root:

    python3 scripts/edge_mission.py --dry-run
    python3 scripts/edge_mission.py --enable-network \
        --deadline 180 \
        --evidence crow-os-agent-mission-results.json

The --enable-network option grants net.fetch/net.resolve to the *four
mission agents only*, and adds the public Crow OS repository URL after
the exact requested instruction so the current worker can perform an
explicit URL-triggered fetch. It is not a general arbitrary network
scanner, nor does it grant shell access or bypass SSRF/tool capabilities.
Without --enable-network, mission agents have no tool grants.

The mission runner *observes* job outcomes and writes local JSON evidence.
It does not auto-merge code or execute unreviewed model-proposed patches.
The actual local Ollama response, full end-to-end system output and network
reachability are unverified until an operator runs the mission.

## Release decision

HOLD: do not merge, deploy, enable unrestricted network tools, or claim
live agent execution before exact-head CI and real-model local smoke tests.
No connected Desktop Commander device; Browser has no Crow OS tab.
The available Railway account has no service for this application.
