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
- `src/crow_os/signers.py` — HMAC dev signer + Ed25519 production signer
- `src/crow_os/llm.py` / `worker.py` — Ollama client and the
  capability-gated LLM worker (v0.2)
- `src/crow_os/cli.py` — `crow` command line

## Run

```bash
PYTHONPATH=src python3 -m crow_os demo --home /tmp/crow-home
# LLM-backed run (local Ollama; model via --model or CROW_OS_MODEL,
# default qwen2.5-coder:32b; endpoint via --ollama or OLLAMA_HOST):
crow --home /tmp/crow-home run --objective "build the OS"
PYTHONPATH=src python3 -m crow_os verify --home /tmp/crow-home
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

Status: v0.2 — kernel + Ollama worker behind the capability gate. Deliberately small — the kernel proves the trust
invariants first; LLM agents, fleet dispatch and federation plug in
behind the same capability and ledger contracts.

## Local Ollama + AVRONE integration (opt-in)

Crow OS remains the authority for its ledger, capabilities, workflow roles
and tool permissions. AVRONE can supply *evidence*, but an imported
conclusion is not a Crow OS governance approval and cannot execute a tool.

### Run Ollama on the local Mac or Linux machine

Install Ollama, launch the Ollama app or start its server, and download a
model that fits the local machine's available RAM. Example:

    ollama list
    ollama pull qwen2.5-coder:7b
    export CROW_OS_MODEL=qwen2.5-coder:7b
    export OLLAMA_HOST=http://127.0.0.1:11434

    python3 -m venv .venv
    . .venv/bin/activate
    python -m pip install -e .
    crow ollama-check --smoke
    crow --home "$HOME/.crow-os" run --objective "Verify the local Ollama pipeline"
    crow --home "$HOME/.crow-os" verify
    crow --home "$HOME/.crow-os" serve --port 8787

If the Ollama desktop app already serves port 11434, do not start a
second ollama serve. The check reports whether the configured model is
installed; it does not implicitly download models or contact cloud providers.
The optional --model and --base-url arguments override the defaults.

### Import selected AVRONE observations

Use a local AVRONE Bridge journal under the AVRONE data directory,
such as AVRONE_DATA_DIR/bridge/<run-id>.jsonl:

    crow --home "$HOME/.crow-os" avrone-import \
      --path "/path/to/avrone-data/bridge/your-run.jsonl"
    crow --home "$HOME/.crow-os" verify

This is explicit one-way ingestion of untrusted, bounded, deduplicated
observations into the Crow OS signed ledger. It is not automatic tool
invocation, remote fleet dispatch, model download or a daemon.
Malformed journals, symlinks, oversized files and unsafe tombstones are
rejected before writing.

### Failure behavior

An interrupted Ollama NDJSON stream, an explicit error, invalid Unicode, an
oversized frame, or a failed connection raises LLMUnavailable. The worker
records a failed role instead of approving incomplete work. A single legacy
JSON fixture without a done field remains accepted for compatibility with
earlier test stubs. Only localhost is selected by default; remote Ollama is
an explicit operator configuration.

See [the source and verification registry](docs/registry/avrone-local-ollama-2026-11-15.md).


## Four-agent local chat mission

Crow OS already provides a **New agent** form in the Home Screen. Users can
name agents, write multiline custom instructions, choose avatars, and
explicitly grant tools; definitions persist across restarts.

With a local Ollama model running, start Crow OS in one terminal:

    PYTHONPATH=src python3 -m crow_os serve --port 8787

In another terminal, review and then launch the mission:

    python3 scripts/edge_mission.py --dry-run
    python3 scripts/edge_mission.py --enable-network --deadline 180 \
      --evidence crow-os-edge-mission-results.json

This sends the requested edge-hardening directive as the first line to four
named agents, with a public Crow OS source URL appended when networking is
explicitly enabled. The runner watches job status, writes a local audit JSON
file, and never converts incomplete/failed jobs into false success claims.
It does not grant arbitrary shell access or auto-merge suggested changes.
See the [mission evidence registry](docs/registry/agent-mission-evidence-2026-11-15.md).
