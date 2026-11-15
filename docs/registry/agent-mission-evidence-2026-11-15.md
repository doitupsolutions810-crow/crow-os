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


## 2026-11-15 follow-up validation

The isolated Python 3.13.5 agent-registry and mock-HTTP mission harness
passed **20/20 tests** after including the 900-digit timestamp overflow
regression. These checks cover 16 parallel thread creations, eight
independent process creations, bad lock paths, model and custom-instruction
validation, and four mock chat submissions with terminal mocked responses.

The committed source fixes are in
[agents.py](../../src/crow_os/agents.py) and
[worker.py](../../src/crow_os/worker.py). The repository-level tests
[test_named_agent_hardening.py](../../tests/test_named_agent_hardening.py),
[test_edge_mission.py](../../tests/test_edge_mission.py) and
[test_agent_http_admission.py](../../tests/test_agent_http_admission.py)
are separate and need Python 3.11/3.12 hosted CI. No user Mac or installed
Ollama model was accessed; no actual live chat jobs ran in the user's OS.

Release decision remains HOLD.


## Mission durability and observed-tool-use verification — 2026-11-15

| Resource | Tested behavior | Evidence |
| --- | --- | --- |
| [Mission runner](../../scripts/edge_mission.py) | Preflight ledger integrity and local Ollama availability; bounded 1-5 rounds; atomic mode-0600 per-submission and per-poll checkpoints; halt on failure | Source updated on integration branch |
| [Recovery HTTP tests](../../tests/test_edge_mission_recovery.py) | Eight cases, including failure on third submission while preserving two job IDs, missing Ollama, unverified ledger, multi-round completion, symlink-safe evidence and interrupted local API | New repository regression module |
| [Observed ledger tooling tests](../../tests/test_edge_mission_ledger_evidence.py) | Three cases distinguish network grants from signed-ledger-reported use, preserve unknown status, and record worker failure messages | New repository regression module |
| [Existing user agent creation tests](../../tests/test_named_agent_hardening.py) | Named agents with custom instructions, durable storage and concurrent name isolation | Existing code and tests retained |
| [CI checks](https://github.com/doitupsolutions810-crow/crow-os/pull/1/checks) | Full unit and probe suite must run on the exact PR head | Pending / not certified |

On isolated Python 3.13.5, the existing 20 tests plus 8 mission-recovery
and 3 ledger-observation tests passed: **31/31 tests** in approximately
10.1 seconds. The HTTP responses and job outcomes in these tests are
scripted test doubles, **not live local-Ollama inferences**. The user's
Crow OS endpoints at 127.0.0.1:8787 and Ollama at 127.0.0.1:11434
were not reachable from the runtime used for testing; Desktop Commander
returned no connected devices. No real network-capable Crow OS agent was
started or monitored from the user's machine.

The mission client only observes and reports existing network-tool events.
It does not grant arbitrary shell access, patch the running OS, or
auto-merge code. Release decision: **HOLD**.


## Parallel fleet / connector integration verification — 2026-11-15

Reconciled with upstream Crow OS v0.16 (main baseline
1c46f7dbef28f4cd36e08e1f2941f7f4bd9301b6). The source tree
retains upstream scheduled routines, federation peer endpoints, device
hosting and GitHub connector catalog. Release decision: HOLD.

| Accountable source | Resource / assertion | Execution evidence |
| --- | --- | --- |
| [Agent mission](../../scripts/edge_mission.py) | Native /api/fleet; four named custom agents; bounded 1..5 rounds; strict job identity and fleet acknowledgement; checkpoint at each submission/poll | Isolated mock HTTP tests passed |
| [Fleet tests](../../tests/test_edge_fleet_mission.py) | Two parallel fleet rounds (8 child status summaries), reuse and fail-stop, network grant opt-in | Four isolated cases passed |
| [Mission identity tests](../../tests/test_edge_mission_identity.py) | 250 deterministic malformed IDs, duplicate agent ID, wrong job status attribution, false fleet start | Six isolated tests passed |
| [Connector tool gate](../../src/crow_os/connector_tooling.py) | Explicit, allowlisted github.repos / github.whoami directives; at most two calls; broker grant before network; no untrusted model commands; serialize results before signed success attestation | Eight isolated mock connector tests passed |
| [Connector tool tests](../../tests/test_connector_directives.py) | Missing/unknown grants, budget, malformed source, two calls, non-JSON results do not mint success evidence | Committed; full repository test pending |
| [Named agents](../../src/crow_os/agents.py) | Custom instructions, IDs and avatars persist; known connector tools accepted only via approved extra_tools catalog | Four isolated connector grant checks passed |
| [Kernel](../../src/crow_os/runtime.py) | Initializes the native ConnectorRegistry; passes it to agent worker only when tools are enabled | Committed; full kernel verification pending |
| [Agent worker](../../src/crow_os/worker.py) | Injects validated connector results as untrusted context; checks actual agent.spawn TOOL grant before child dispatch; keeps v0.16 agent memory/routine behavior | Committed; CI pending |
| [CLI](../../src/crow_os/cli.py) | Agent creation recognizes native connector catalog | Committed; CI pending |
| [HTTP server](../../src/crow_os/server.py) | Applies 64 KiB object JSON bound to 10 POST routes including federation, and passes trusted connector tool catalog when creating agents | Committed; CI pending |
| [Full kernel connector tests](../../tests/test_native_github_connector_agents.py) | Four tests: persistence, mocked read-only GitHub grant, signed tool-use event, ungranted denial | Committed; exact-head CI pending |
| [CI](https://github.com/doitupsolutions810-crow/crow-os/pull/1/checks) | Python 3.11/3.12 full suite and all upstream probe harnesses | No completed exact-head run yet |

**Isolated local execution:** Python 3.13.5, 53/53 tests passed,
including 8 mock connector tests and multi-process duplicate-name
tests. A synthetic fleet trace exercised two parent jobs with eight
member-status summaries. No genuine GitHub connector token or network
response was used in that trace. These are NOT complete checkout,
user-Mac Ollama, hosted federation or deployment results.

**Physical integration block:** No connected Remote Desktop device;
loopback Crow OS 127.0.0.1:8787 and Ollama 127.0.0.1:11434 were not
reachable from the test runtime. The user's live chat received no
request from this session. No production merge, deployment or automatic
running-OS patch was performed.
