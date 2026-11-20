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

## v0.16.3 — federation replay, concurrent (shipped)

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

## v0.16.4 — federation replay, future-dated dispatch (staged)

Round 23 probed the v0.16.2/22 replay fix with a body it had never
seen: a signed dispatch whose ts is in the FUTURE (sender clock
~110s fast — verify_incoming accepts any ts within abs(now - ts) <=
120s, for clock skew). One real defect, fixed:
- F1 future-dated replay: check_replay() pruned the seen-store by
  RECEIPT time, while verify_incoming judges staleness by the SIGNED
  ts. A body dated receipt+110 was still verifiable at receipt+130
  (abs diff 20), but its store entry had already pruned at
  receipt+120 — so the captured dispatch ran a SECOND time, up to
  ~240s after receipt. check_replay() now stores the signed ts
  (not receipt time) and prunes once the signed ts is older than
  MAX_SKEW; past ts+120 verify_incoming refuses the body as stale
  anyway. Old stores hold receipt times (also epoch seconds), so the
  same prune still applies to them.
- F2/F3 controls pass as-is: a body with ts 130s old is refused 401
  outright, and two distinct future-dated objectives both run.
Totals: 180 probes + unit suite, all green via scripts/check.sh
(harness 23 wired in).

Round 24 stress-tested the v0.16.4 fix in the combinations Rounds
21-23 never tried together — and found nothing: 8x concurrent
identical future-dated (+110s) dispatches ran exactly once (7x401);
a future-dated dispatch, home restart, then replay inside the
window still 401 (entry survives restart ts-keyed); 4x concurrent
distinct future-dated objectives sharing one ts all ran once each;
a normal dispatch after future-dated traffic still runs. Totals:
184 probes + unit suite, all green via scripts/check.sh
(harness 24 wired in). No code change — v0.16.4 stands as staged.
Superseded before push by v0.16.5 (Round 25 below) — v0.16.4's
fix ships inside v0.16.5, so one push carries both.

## v0.16.5 — federation payload types, incl. NaN-ts replay (staged)
Superseded before push by v0.16.6 (Round 26 below) — v0.16.5's
fix ships inside v0.16.6, so one push carries both.

Round 25 probed the federation surface Rounds 21-24 never touched:
malformed-but-signed bodies. verify_incoming checked presence +
signature + staleness, never TYPES, and /api/federation/run is
synchronous and unguarded. Real defects, all reproduced end-to-end
before the fix, all fixed:
- F1 dropped connections: ts of None / list / dict raised TypeError
  in float(), which the handler does not catch — the client saw a
  dropped connection, no HTTP status. Same for a sig of int / dict
  (TypeError in bytes.fromhex inside Identity.verify).
- F2 false attestation: objective of int / None passed verification
  and the replay check, federation.received was attested, THEN
  run_agent crashed on objective.splitlines() — a receipt in the
  ledger for a job that never ran.
- F3 unbounded replay: ts=NaN. abs(now - NaN) > MAX_SKEW is False,
  so the body never went stale, while the prune pass
  (now - v <= MAX_SKEW, also False for NaN) dropped its seen-store
  entry on the next call. One signed NaN-ts body ran three times,
  three distinct jobs — the replay protection Rounds 21-24 built
  did not apply to it at all.
- F4 type confusion: a numeric-string ts was accepted and ran.
Fix: verify_incoming now requires from/agent/objective to be
strings, ts to be a finite number (bool and numeric strings
refused), and sig to be a string — wrong types refuse 401 before
any attestation or run. Identity.verify also catches TypeError,
check_replay treats a non-finite stored ts as now, and
federation_receive re-checks agent/objective types before
attesting, so a direct caller cannot leave a false receipt either.
Probes: 11/11 (R1-R5, R9-R10 malformed fields 401; R6 NaN refused
on first send and replay; R7-R8 no false attestation; R11 control
normal dispatch 200 with exactly one attestation). Totals: 195
probes + unit suite, all green via scripts/check.sh (harness 25
wired in).

## v0.16.6 — blank federation fields + malformed shapes in combination (staged)

Round 26 stress-tested the v0.16.5 fix under the combined
conditions that broke every earlier replay fix — concurrency,
restart, future-dated, mixed valid/malformed traffic — and
probed the shapes Round 25 never sent. 6/7 green on first run;
it caught one real defect:
- F5 blank objective, false attestation + dropped connection:
  objective="" is a string, so it passed the Round-25 type check,
  passed the replay check, and federation.received was attested —
  THEN workflow.create_job refused it ("objective must be a
  non-empty string", WorkflowError, which the /api/federation/run
  handler does not catch), so the client saw a dropped connection
  and the ledger kept a receipt for a job that never ran. Same
  shape as the Round-25 non-string objective, one field-value
  further along. Reproduced end-to-end before the fix.
Fix: verify_incoming now requires from/agent/objective to be
non-empty after strip (401 at the federation boundary, before
any attestation or run), and federation_receive re-checks
non-emptiness before attesting, so a direct caller cannot leave
a false receipt either. The other Round-25 edge is closed too:
ts=+Infinity / -Infinity / 1e400 all refuse 401 with no
attestation (NaN's untested sibling — same "every comparison
is False" logic, now probed).
Probes: 7/7 (R1 Infinity variants 401; R2 8x concurrent identical
NaN all 401, no jobs; R3 mixed burst 4 valid run once each, 4
malformed 401; R4 NaN refused before and after restart, valid
control runs; R5 malformed bodies sharing a ts do not poison the
seen-store for a later valid dispatch, same-ts and future-dated;
R6 blank objective/agent 401, no attestation; R7 control normal
dispatch 200 with exactly one attestation). Totals: 202 probes
+ unit suite, all green via scripts/check.sh (harness 26 wired
in). Includes the v0.16.5 fix, so one push carries both.
Superseded before push by v0.16.7 (Round 27 below) — v0.16.6's
fix ships inside v0.16.7, so one push carries all three.

## v0.16.7 — unknown-agent replay poison + oversized agent id crash (staged)

Round 27 probed the validation that still ran AFTER the replay
check — agent existence, which Rounds 25-26 never sent, since
every earlier harness only ever dispatched to an agent that
existed. 3/7 green on first run; it caught two real defects:
- F1 unknown-agent dispatch poisoned the seen-store: the handler
  ran verify_incoming -> check_replay -> federation_receive, and
  agents.get (the first agent-existence check) lives in
  federation_receive — so a signed dispatch to an agent that did
  not exist yet was recorded in the seen-store FIRST, then refused
  400. Creating the agent and resending the identical signed body
  inside the 120s window then hit 401 "replayed", even though the
  job never ran and was never attested. A corrupt agent definition
  hit the same path, and the poison survived restart because the
  seen-store is persisted in CrowFS. Same class as the Round-26
  R5 store-poison rule, one validation later in the chain.
  Reproduced end-to-end before the fix (R1/R2/R5 all 401 on retry).
- F2 oversized agent id dropped the connection: agent="x"*500 made
  the OS refuse the definition path itself (ENAMETOOLONG) before
  CrowFS could report "no such file" — that OSError escaped
  agents.get, which the /api/federation/run handler does not catch,
  so the client saw a dropped connection instead of a 400. Same
  shape as the Round-25 wrong-typed-field crash.
Fix: the handler now validates agent existence (agents.get) after
signature verification but BEFORE check_replay, so a refused body
never consumes a seen-store slot; federation_receive still
re-checks before attesting, so a direct caller is covered too.
agents.get now converts an OSError on the definition read into
"unknown agent" (400) instead of letting it crash the handler.
One honest residual: an agent deleted between the handler's check
and federation_receive still burns the body — closing that needs
a lock across the two, and agent deletion mid-dispatch is an
operator action, not a peer one. Also fixed in this staging:
pyproject.toml still said 0.16.5 while __init__ said 0.16.6 (the
Round-26 bump missed it); both now say 0.16.7.
Probes: 7/7 (R1 unknown agent, create, same body retries 200;
R2 corrupt definition, repair, same body retries 200; R3 8x
concurrent unknown agent all 400, none burns a slot; R4 ghost
sharing a ts does not block a valid dispatch; R5 poison does not
survive restart — same body retries 200 after restart+create;
R6 traversal/oversized agent ids all 400, no attestation, server
alive; R7 control normal dispatch 200 with exactly one
attestation). Totals: 209 probes + unit suite, all green via
scripts/check.sh (harness 27 wired in). Includes the v0.16.5 and
v0.16.6 fixes, so one push carries all three.
Superseded before push by v0.16.8 (Round 28 below) — v0.16.7's
fix ships inside v0.16.8, so one push carries all four.

## v0.16.8 — delete-mid-dispatch race: burned bodies + false attestations (shipped 2026-11-20)

Round 28 probed the honest residual Round 27 stated in server.py:
an agent deleted between the handler's existence check and
federation_receive still burns the body. 1/5 green on first run
(R3 failed); it caught both halves of the race:
- W1  delete landing between the handler check and
  federation_receive's re-check: the receive raised AgentError ->
  400, but check_replay had already recorded the body, so
  recreating the agent and resending the identical signed body hit
  401 "replayed" for a job that never ran and was never attested.
  15 of 48 storm bodies burned this way. The window is not
  microscopic — check_replay's CrowFS read + write of the
  seen-store sits between the two checks.
- W2  delete landing between the attestation and run_agent's
  re-fetch by id: the ledger already held federation.received,
  then run_agent raised AgentError -> 400 — a receipt for a job
  that never ran, the false-attestation class Rounds 25-26 closed
  for malformed shapes. The storm left 5 excess attestations
  against 0 jobs run. The burn persisted across restart (R4: the
  burned body still 401 after restart + recreate), because the
  seen-store lives in CrowFS.
Fix: the handler now holds the AgentRegistry lock across
agents.get -> check_replay -> federation_receive, agents.delete
takes the same lock, and the validated agent definition is captured
and passed through federation_receive into run_agent (new optional
`agent` parameter on both) — so the attestation and the run use the
agent as validated, never a re-fetch by id. The lock is released
before the run itself: a delete arriving after validation waits,
then removes the agent, while the already-validated dispatch still
runs to completion; a delete arriving first makes the dispatch
refuse cleanly (400, body never recorded).
Probes: 5/5 (R1 control 200, +1 attestation; R2 Round-27 regression
still holds; R3 delete-storm: 48 dispatches, 15 ran, 33 clean
refusals that all resend 200, 0 burned, 0 excess attestations; R4
no burns to persist; R5 control 200, +1 attestation, ledger ok).
Totals: 214 probes + unit suite, all green via scripts/check.sh
(harness 28 wired in). Includes the v0.16.5, v0.16.6 and v0.16.7
fixes, so one push carries all four.
One honest residual, same class one step out: spawn_subagent and
fleet_run also validate an agent, attest, then re-fetch it in
run_agent — a delete in that window leaves agent.spawned /
fleet.member receipts for a member that never ran. The captured-
agent pass-through now exists to close those the same way; they
have no dedicated probe yet.

## v0.16.9 — spawn/fleet delete race: attested members that never ran (shipped 2026-11-20)

Round 29 probed the honest residual v0.16.8's section stated:
spawn_subagent and fleet_run also validate an agent, attest, then
re-fetch it in run_agent. Reading the code firsthand confirmed the
pattern in three places — both validation results were DISCARDED:
- Kernel.spawn_subagent: agents.get (discarded) -> create child ->
  agent.spawned attested -> run_agent re-fetches by id.
- Kernel.fleet_run: agents.get each member (discarded) -> create
  children -> fleet.member attested for each -> member threads'
  run_agent re-fetches by id.
- HTTP /api/spawn (server.py) duplicated the spawn pattern with a
  wider window: validate-discard -> create -> attest -> run on a
  thread -> run_agent re-fetch. (HTTP /api/fleet only pre-validates
  and attests nothing, so the kernel fix covers it.)
Harness 29 against the unfixed tree: 5/8. The race reproduced in
all three paths:
- R3 spawn storm (48 calls, waves of 8, delete+recreate loop):
  10 attested, 7 attested-not-completed, all 7 child jobs stranded
  "running"; the caller saw a plain AgentError, indistinguishable
  from a clean refusal — only the ledger told them apart.
- R6 fleet: a continuous storm alone attested nothing (the agent
  was almost always missing at the validation instant, so fleets
  refused cleanly — stated honestly), but a targeted phase that
  deletes the moment the first fleet.member entry appears in the
  ledger hit the window every time: 64 of 64 attested members
  never ran, all 64 child jobs stranded "running", all 16 fleet
  parents reporting "failed" for members the ledger says joined.
- R8 HTTP spawn storm (32 posts): 8 attested, 4 never ran.
Fix (same mechanism as Round 28, no parallel one): the registry
lock is held across validate -> create -> attest in
spawn_subagent, fleet_run, and the /api/spawn handler; the
validated definitions are captured and passed through run_agent's
existing `agent` parameter (fleet captures one per member). The
lock is released before any run: a delete arriving after
validation waits, then removes the agent, while the validated
run completes; a delete arriving first refuses cleanly with no
attestation.
Probes: 8/8 after the fix (R3: 25 attested, 25 completed, 0
false, 23 clean refusals; R6: 68 attested members, 68 completed,
0 stranded — targeted phase 16/16 fleets completed; R8: 11
attested, 11 completed, 0 false). Totals: on-disk PASS lines sum
to 217 edge probes (209 through harness 28 + 8 new) + unit suite,
all green via scripts/check.sh (harness 29 wired in). The running
total quoted in earlier sections (214) carries the known +5
offset against the on-disk sum and was not re-audited.
One honest residual, same class one step out: Kernel.fire_routine
also creates a job, attests routine.fired, then lets run_agent
re-fetch the agent by id — a delete in that window would leave a
routine.fired receipt for a run that raises. It was not probed
or fixed this round (Round 29's scope was spawn + fleet); the
captured-agent pass-through closes it the same way when probed.
