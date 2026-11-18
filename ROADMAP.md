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

## v0.10 — HUD, devices & sub-agents (shipped)

Upgrades integrated from across the account's repos, re-analyzed at
40 repos: sub-agent spawning (swarm-vm-runner / Avrone's squad
spawning), a control-room HUD (avrone-control-room), device &
hosting awareness (the daemon's hosting model).

- **Sub-agent spawning**: agents granted `agent.spawn` can delegate
  with "delegate to <agent-id>: <objective>" lines. Every spawn is
  attested (`agent.spawned` links parent -> child in the signed
  ledger), depth-capped at 2, max 3 per run; children run their own
  [agent, governance] chains. POST /api/spawn spawns directly.
- **Devices & hosting**: the local device profile is computed
  (hostname, platform, CPUs, live Ollama state); users register
  other devices into /system/devices.json — shown as registered,
  never pretend-verified. GET/POST /api/devices; status reports
  hosting (serving port).
- **Tooling HUD** on the chat surface: this device + Ollama state,
  hosting state, a Tools master switch wired into every new run
  (off = agents run with zero tool calls), and a live count of
  agents at work / active runs.

## v0.11 — match the snapshot (shipped)

Brandon sent a screen recording of the real Grok Bot app; the OS
now matches its environment: agents carry blob avatars (13 shapes x
11 colors from the Create-a-Bot sheet, rendered with the two oval
eyes), user messages are orange bubbles, every agent has a profile
view (avatar, Library, Computer status, Permissions, Routines,
Notifications), Settings carries the snapshot's sections — Connect
apps (a plugin catalog with categories and Connect buttons; states
are verified / built-in / user-asserted, never faked), Bot Computer
("the one computer your agents share" — real disk and home sizes),
Manage your devices, Permissions (capability grants per agent).

## v0.12 — output the chats (shipped)

- Every job exports as a Markdown transcript — the user's message,
  each agent's output, tool invocations, sub-agent links, steps and
  the governance verdict — assembled from the workflow record, the
  outputs sink and the signed ledger, so the export is what was
  attested. GET /api/jobs/<id>/transcript and `crow transcript
  <job_id>`; transcripts stay complete after a restart by falling
  back to the persisted artifact in Documents.

## v0.13 — agents get file tooling (shipped)

- fs grants were inert for agent runs; they are real now. An agent
  granted fs.read / fs.list acts on "fs read: <path>" /
  "fs list: <path>" lines in the objective: gated before any disk
  I/O, attested as tool.invoked, content pulled into its context,
  failures fail soft. Combined with net.fetch and agent.spawn, one
  chat message can now fetch a page, read a file and delegate —
  all in one governed run.

## v0.14 — bridging the hypotheticals (shipped)

The three near-term evolutions, wired:

- **Agent memory**: every agent has a memory file in CrowFS
  (/system/agents/<id>.memory.md). "remember: <text>" lines in a
  chat are stored before the run (attested as agent.remembered);
  memory rides into later runs' context (bounded to the newest
  2,000 chars; file capped at 32KB); each completed run auto-notes
  its objective and result. GET /api/agents/<id>/memory.
- **Routines**: scheduled agent runs — definitions in
  /system/routines, due-check + fire split in the kernel, a tick
  loop in `crow serve`, routine.fired attested per firing.
  GET/POST/DELETE /api/routines, POST /api/routines/<id>/toggle.
  The bot profile's Routines count is real now.
- **Fleet dispatch**: one objective across 1–8 agents in parallel;
  membership attested (fleet.member) before anyone runs; the
  parent completes only if ALL members complete; merged output on
  the parent job. POST /api/fleet.

## v0.15 — federation (shipped)

Not hypothetical anymore. Each home has an Ed25519 identity
(identity.ed25519 in the home dir, mode 0600; GET /api/identity).
Homes register peers (name + URL + public key, /system/peers.json;
GET/POST/DELETE /api/peers). Dispatch is a signed request — the
receiver verifies against the REGISTERED public key, refuses
unsigned / forged / stale (>120s, anti-replay) / unknown-peer
requests with 401, validates the agent BEFORE attesting
federation.received, and runs the job under its own governance.
The sender attests federation.dispatched with the remote job id;
the reply carries the remote transcript. POST
/api/federation/dispatch + /api/federation/run. Transport is HTTP
carrying the signature — mTLS can wrap the same endpoints later
without changing the trust model. Round-18 probes cover the full
refusal matrix.

## v0.16 — connectors go live (shipped)

The Connect-apps catalog's last hypothetical is wired. A connector
framework (connectors.py) gives each integration three honest
states — not configured / configured-but-unreachable / live —
checked against the real endpoint. Secrets come only from the
environment or a 0600 file in the home's secrets/ directory; they
never pass through chat, the API, CrowFS, or the ledger (probed).
The GitHub connector ships read-only tools (github.repos,
github.whoami) against api.github.com by default; agents are
granted them like any tool and invoke them with
"connector: <tool>" lines — capability-gated before the call,
attested as tool.invoked. When the check passes, the catalog entry
flips from "connected (your word)" to "live · verified"; when the
API refuses, it drops to "configured · unreachable" and agent
directives fail soft with no fake data. GET /api/connectors,
POST /api/connectors/<id>.

## v0.16.1 — combined-surfaces fix (shipped)

Round 20 probed the v0.14–v0.16 surfaces in combination for the
first time — the gap flagged after v0.16.0. Six probes, all green:
- GG1 routine writes agent memory, recalled by a later run
- GG2 federated run writes the REMOTE agent's memory; nothing leaks
  into the sender's memory or ledger
- GG3 federated run uses a connector; the token appears nowhere on
  the sender and the remote ledger still verifies
- GG4 a misdirected connector is NOT live (see fix below)
- GG5 fleet writes per-member memory for every member
- GG6 routine fires a granted connector, attested and in the
  transcript

One real defect, fixed: the GitHub live check accepted any endpoint
that answered 200 with JSON, so a non-GitHub service (or a stub)
counted as "live · verified". The check now requires a GitHub user
object — a dict with a `login` — and anything else is
"unreachable". Totals: 168 probes + unit suite, all green via
scripts/check.sh.
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

## v0.16.2 — federation replay fix (shipped)

Round 21 probed federation for replay: the identical signed dispatch
body POSTed twice to /api/federation/run executed TWO remote jobs.
verify_incoming checked signature + timestamp (120s window) only —
no nonce, no seen-request store — so anyone on the HTTP path between
homes who captured one dispatch could re-execute it inside the
window, connector calls and memory writes included.

Fix: the receiver records every accepted signed request in a
persistent seen-store (CrowFS /system/federation-seen.json, so a
restart does not reopen the window) and refuses a replay with 401
before the federation.received attestation or the run. The store is
keyed by a hash of the canonical signed body, not the timestamp —
two distinct objectives sharing one timestamp still both run (R4).
Probes: R1/R5 replays refused 401, R2 stale ts refused, R3 tampered
objective refused, R4 same-ts distinct objectives both run. Totals:
173 probes + unit suite, all green via scripts/check.sh.

## v0.16.3 — federation replay, concurrent (unpushed)

Round 22 probed the v0.16.2 replay fix under concurrency and restart,
which Round 21 never did. Three real defects, all fixed:
- C1 concurrent replay: 8 identical signed dispatches at once ran
  the job 2–3 times — PeerStore.check_replay() is read-check-write
  with no lock on a threaded server. Now held under a per-store
  threading.Lock; exactly 1 run per burst, the rest 401.
- C1/C3 dropped connections: concurrent writes to the same CrowFS
  path shared one fixed tmp name (`.<name>.tmp`), so one thread's
  os.replace() moved the tmp file out from under another, which died
  with FileNotFoundError — uncaught in server.py, so the client saw
  a dropped connection. CrowFS.write_file now uses a unique tmp name
  per call (pid + random token); this protects every concurrent write
  to the same path (seen-store, agent memory, peers.json).
- C4 corrupt seen-store failed open: check_replay() swallowed
  JSONDecodeError and reset the store to {}, so a replay ran after
  the store was damaged. A present-but-unparseable (or non-dict)
  store now refuses the dispatch with 401; a missing file (first
  run) is still an empty store.
C2 passed as-is: the seen-store survives a restart of the same home,
replay still 401. Totals: 177 probes + unit suite, all green via
scripts/check.sh (harness 22 wired in).
