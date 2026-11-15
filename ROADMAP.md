# Crow OS roadmap

## v0.1 — kernel (done)
Strict ledger, exact capabilities, role-checked workflow, demo runtime,
CLI, tests, CI. Every invariant is a regression test named after the
audit failure it prevents.

## v0.2 — real agents (done)
- LLM worker (Ollama) behind per-step EXECUTE capabilities; the gate is
  checked before the model is called, and an unreachable model fails the
  job closed — no synthetic passes
- Ed25519 capability signer alongside the HMAC dev signer; verify-only
  brokers hold just a public key
- Still open: persistent broker state (revocations, lineage, usage) in
  the ledger; Avrone-style HMAC fleet dispatch arrives with v0.4

## v0.3 — home screen (shipped)

- `crow serve`: the OS Home Screen — chat with the squad, a live
  operations index, expandable per-agent thinking cards rendered from
  the signed ledger's own event stream, sandbox bar, code/app views
  and a ledger inspector. Runs execute on the real kernel in threads;
  a crashed run fails its job in the ledger instead of stranding it.
- Persistent broker state is signed and written atomically under a
  lock (concurrent runs cannot race it).

## v0.4 — wiring & stability (shipped)

- One chain implementation: the server pre-creates the job and the
  kernel executes it — demo and LLM paths both accept a pre-created
  job_id, so the run API is uniform and no step logic is duplicated.
- `crow verify` now checks entry signatures when the home has a
  secret (previously it verified digests only).
- `scripts/check.sh` runs the unit suite plus every probe harness;
  CI runs it too.

## v0.5 — Grok-style front end on the Ollama backend (shipped)

- The Home Screen is now a chat-bot front end in the Grok mold: left
  rail of chats/operations with search, a composer whose model
  selector is populated from the OS's own Ollama (/api/model reads
  /api/tags), and a slide-over Build panel (Code / App / Ledger /
  Sandbox). All of it is a thin skin over the kernel backend.
- Runs default to auto: LLM mode when the OS-installed Ollama is
  reachable, demo runtime when it is not. Model choice in the UI is
  honored end-to-end; chat streams token-by-token (live buffer in the
  job outputs) and job progress streams over SSE with polling fallback.
- Ledger reads take a shared lock: unlocked reads racing appends
  could observe torn lines and strand jobs under load.

## v0.6 — network-connected tooling (shipped)

- Agents get real network tools, built like everything else here:
  capability-gated (`TOOL net.fetch` / `TOOL net.resolve`, checked
  before any socket opens), SSRF-guarded (http/https only; the host's
  resolved IPs must all be public — loopback, RFC1918, link-local,
  metadata and benchmarking ranges refused; redirects re-validated
  hop by hop), bounded (10s timeout, 64KB read cap), and attested
  (every invocation lands in the signed ledger as tool.invoked).
- When a chat objective names a URL, the coder fetches it through the
  tool and builds on the fetched context. Intranet destinations need
  the operator's explicit opt-in (Kernel allow_private_tools /
  CROW_OS_ALLOW_PRIVATE_TOOLS=1); the default posture refuses them.

## v0.7 — the file system + Grok-look polish (shipped)

- CrowFS: the OS now has its entire file system — a standard tree
  (/system, /home/user/{Documents,Downloads,Pictures,Music}, /apps,
  /var/{log,cache}, /tmp) rooted at <home>/fs, speaking virtual paths
  that can never name a host file. Traversal and symlink escapes are
  refused, writes are atomic and size-capped, and agent access is
  capability-gated (TOOL fs.list/fs.read/fs.write) and attested.
  Completed jobs persist their artifacts to
  /home/user/Documents/<job>.md through the gated tool. Operator
  surface: `crow fs ls|cat|write|tree`, /api/fs + /api/fs/read, and a
  Files tab in the Build panel.
- Front end, closer to Grok so the system reads at a glance: "What do
  you want to know?" greeting with suggestion cards (one runs the
  edge-case instruction, one fetches a page, one opens the file
  system, one verifies the ledger), "Ask anything" composer with a
  ＋ button, and the model selector moved to the top bar.

## v0.8 — custom agents (shipped)

- Users create agents: a name, custom instructions (the agent's
  system prompt), an optional model, and tool grants from the OS
  catalog. Definitions live in the file system
  (/system/agents/<id>.json), so agents persist across restarts and
  are browsable in the Files tab.
- Talking to an agent runs its own chain — [agent, governance] —
  behind the capability gate: it receives EXECUTE only for itself
  and TOOL rights only for its granted tools. Instructions are data
  for the model, never authority; governance still verifies the
  ledger last, and no chain may skip it (the workflow now carries a
  per-job chain and enforces governance-last at creation).
- Surfaces: Agents section in the Home Screen rail (create form +
  select-to-talk), /api/agents (GET/POST/DELETE), `crow agent
  create|list`, `crow run --agent <id>`.

## v0.9 — simplify: Chat + Agents are the surface (shipped)

- Researched against the real thing: Grok's app (sidebar with new
  chat + history, profile menu bottom-left, gear top-right, model
  picker inside the message box, send button that appears once the
  composer has content) and the Cursor SDK (@cursor/sdk: a durable
  Agent holding model + settings across prompts; a Run per prompt
  with a streamed event feed — the same shape as our agent registry
  and ledger/SSE job stream; one contrast: headless Cursor agents
  auto-approve tool calls, Crow OS keeps them capability-gated).
- The Home Screen now shows only Chat and Agents by default. The
  Build panel (Code/App/Ledger/Sandbox/Files), the system status
  bar and backend details are hidden unless the user enables
  "Developer panels" in Settings (⚙️, persisted in localStorage).
  Nothing was removed — it all still runs on the backend.

## v0.9.1: local Ollama works out of the box (shipped)

- **Proxies.** Calls to a loopback Ollama skip HTTP(S)_PROXY. With a proxy in the environment and no NO_PROXY, every 127.0.0.1 call went to the proxy. That made a running Ollama look unreachable and left 4 unit tests and 18 probes red.
- **Model choice.**
  - The built-in default (qwen2.5-coder:32b) is replaced by the best installed chat model when it isn't pulled. Families are preferred in this order: qwen2.5-coder, qwen3-coder, qwen2.5, qwen3, llama3.x, gemma3, mistral, phi. Embedding models are skipped.
  - A model chosen explicitly (argument, agent setting or CROW_OS_MODEL) is never swapped. If it isn't installed, the run fails closed with Ollama's own reason.
  - Auto mode runs the demo when Ollama is up but has no chat model.
- **Ollama's own error text** ("model 'x' not found", mid-stream "llama runner process has terminated") now reaches the job error. Before, it read "unreachable: HTTP Error 404".
- **Stream robustness.** A non-object JSON line, a truncated stream or an HTML /api/tags fails closed with LLMUnavailable, never AttributeError. The stream stops at `done`.
- **OLLAMA_HOST** is accepted in Ollama's own forms: `host:port`, `0.0.0.0`, or a quoted value.
- **Coder repair.** When the coder drops the plan reference line, it is asked once more and a `coder.repair` entry is logged in the ledger. The tester's exact check is unchanged. A failed tester now gives its reason in the job error. Before, `crow run` printed a failed job with no error.
- **`python -m crow_os`** now works as the README shows; there was no `__main__`.
- **Tests:** tests/test_v09_local_ollama.py (19, plus 1 live test gated by CROW_OS_LIVE_OLLAMA=1) and probes/edge_harness11.py (W1–W8, a hostile or half-broken Ollama). Live run on qwen2.5:3b: the full 4-role chain completed and the signed ledger verified.

## v0.10 — fleet
- Node registry + signed dispatch between Crow OS homes
- Agent-home migration as a ledger-attested operation

## v0.11 — federation
- mTLS peer links (living-intermediate's verified design), digest-checked
  ingest — no unauthenticated peers

## Explicitly not carried over
- Self-signed governance attestations (TrainLLM pattern)
- Identity fallbacks that claim a production trust domain
- In-memory job state
- Any component whose README claims more than its tests prove
