# W1 Divergence Analysis: Avrone (frozen trunk) vs avrone-daemon (canonical)

Wave: **W1, candidate 1** (read-only semantic diff). No source files were modified in either repo; this
document is the artifact. Classification method and the complete finding table are below.

## Verdict in one line

The 89 diverged files confirm the W0 trunk verdict: **avrone-daemon supersedes Avrone in 88 of 89
files**. One file requires a manual port before Avrone can be fully retired: `avrone/bridge.py`.

## Method

1. Both trees were walked recursively via the GitHub contents API
   (Avrone at `mirror/pre-W0-freeze-prep` = commit `52cc1fe6...`, the pre-freeze state;
   avrone-daemon at `main` = commit `3a1fbc19...`), collecting every blob SHA.
2. SHA set-difference produced the census: **230 identical, 89 diverged, 8 Avrone-only, 325 daemon-only**.
   (Two raw-SHA "divergences" from the first pass, `skills/runaway-process/SKILL.md` and
   `avrone-squad/RETRIEVAL.md`, proved to be SHA-transcription artifacts: their contents are identical.)
3. Each of the 89 diverged files was fetched from both sides and compared with a normalized
   line-bag metric (lines trimmed, empty lines dropped, multiplicity preserved). The counts below are
   **Avrone-only lines** (present in Avrone, absent in daemon) vs **daemon-only lines**.

## Census totals across the 89 diverged files

| Metric | Value |
|---|---|
| Avrone-only normalized lines | 1,867 |
| Daemon-only normalized lines | 11,259 |
| Files where daemon supersedes | 88 / 89 |
| Files where Avrone holds unique content | 1 (`avrone/bridge.py`) |

## Classification

- **daemon_superset (61 files):** Avrone's remaining lines are pre-refactor variants of logic the
  daemon has since hardened (defensive parsing, `off_loop` decorators, redaction, bounded I/O, arg
  coercion). Nothing to port.
- **cosmetic (21 files):** near-identical (whitespace/one-line comment drift); where Avrone differs,
  the daemon version is the regression-tested one.
- **review (6 files):** large concurrent evolution on both sides, daemon still dominant. A human
  skim is warranted at next intake, not a blocker: `avrone/evolve.py` (104/383), `avrone/fleet.py`
  (108/754), `avrone/mcp_client.py` (64/394), `avrone/server.py` (60/261), `avrone/tools_extra.py`
  (95/516), `avrone/trinity.py` (66/206).
- **av_at_risk (1 file):** `avrone/bridge.py` - see below.

## Tier-1 items (manual port into avrone-daemon, then reconcile)

1. **`avrone/bridge.py` (850 vs 696 norm-lines; 295 Avrone-only vs 141 daemon-only).** The only file
   in the tree where Avrone is the content superset. Avrone's side carries the `/api/blackboard`
   cross-run fan-in layer (cross-run board reads and the platform-bridge hand-off contracts). The
   daemon rewrote parts of the same module. Port = re-express Avrone's blackboard fan-in section
   against the daemon's current `bridge.py` internals, with tests, in the daemon. After that, no
   fetch from Avrone is ever needed again.
2. **`.env.example` (14 Avrone-only lines).** Avrone documents the platform-bridge environment
   (`AVRONE_PLATFORM_TARGETS_JSON`, `AVRONE_PLATFORM_WRITE_ENABLED=false`,
   `AVRONE_BRIDGE_*` tokens/tenant/capability knobs with `default [] grants no named capability`).
   The daemon's `.env.example` lacks this block. Port the block (as documentation only; the
   `platform_bridge.py` plugin is an Avrone-only file and needs the same decision: port or drop).
3. **`tests/test_robust_v027_access.py` (16 Avrone-only lines).** Avrone still carries
   `test_non_ascii_token_is_refused` (the `hmac.compare_digest` non-ASCII 500 regression). Coverage
   on the daemon side for that branch should be verified locally before the Avrone mirror is
   archived; re-add the test if missing.
4. **`tests/test_tool_env_report.py` (10 Avrone-only lines).** Avrone's version retains
   `admin_token_previous` rotation-grace redaction assertions. Confirm the daemon covers
   `AVRONE_ADMIN_TOKEN_PREVIOUS` presence-only redaction.

## Explicitly dismissible

- `scripts/mac-avrone-autopilot.sh` (9/1 lines): Avrone's side is only an older retry-literal; nothing else.
- `avrone/static/index.html`, `pyproject.toml`, `tools/make_master_prompt.py`, `MASTER_PROMPT.md`:
  single-line/rebuildable drift; daemon regenerates `MASTER_PROMPT.md` from code.

## completeness statement

`skills/runaway-process/SKILL.md` and `avrone-squad/RETRIEVAL.md` were SHA-listed as diverged in the
first pass but are content-identical; they are excluded from the 89. Every other path in the census
is classified in the table below. This analysis is read-only: no commits were made to Avrone or
avrone-daemon for this document.

## Full table (89 diverged files)

"Av. norm-lines" / "Dm. norm-lines" are whitespace-normalized, non-empty line counts after the
MCP download layer; "Avrone-only" / "Daemon-only" are the line-bag differences.

| Path | Av. norm-lines | Dm. norm-lines | Avrone-only | Daemon-only | Class |
|---|---|---|---|---|---|
| `.env.example` | 82 | 81 | 14 | 13 | daemon_superset |
| `.github/workflows/ci.yml` | 42 | 45 | 5 | 8 | daemon_superset |
| `.github/workflows/squad-round-notify.yml` | 66 | 70 | 16 | 20 | daemon_superset |
| `avrone/__init__.py` | 2 | 4 | 1 | 3 | daemon_superset |
| `avrone/agentcontainer.py` | 173 | 296 | 16 | 139 | daemon_superset |
| `avrone/agentfs_tools.py` | 234 | 331 | 38 | 135 | daemon_superset |
| `avrone/agentfs.py` | 101 | 138 | 16 | 53 | daemon_superset |
| `avrone/agentkit.py` | 328 | 631 | 57 | 360 | daemon_superset |
| `avrone/agentpack_tools.py` | 100 | 143 | 13 | 56 | daemon_superset |
| `avrone/agentpack.py` | 382 | 561 | 23 | 202 | daemon_superset |
| `avrone/bootstrap.py` | 205 | 264 | 19 | 78 | daemon_superset |
| `avrone/bridge.py` | 850 | 696 | 295 | 141 | av_at_risk |
| `avrone/checkpoint.py` | 277 | 559 | 27 | 309 | daemon_superset |
| `avrone/cli.py` | 198 | 227 | 8 | 37 | daemon_superset |
| `avrone/codex.py` | 193 | 554 | 30 | 391 | daemon_superset |
| `avrone/config.py` | 182 | 422 | 11 | 251 | daemon_superset |
| `avrone/ctxspawn.py` | 229 | 404 | 16 | 191 | daemon_superset |
| `avrone/doctor.py` | 324 | 326 | 27 | 29 | daemon_superset |
| `avrone/evolve.py` | 959 | 1238 | 104 | 383 | review |
| `avrone/fleet.py` | 1614 | 2260 | 108 | 754 | review |
| `avrone/fsunify.py` | 121 | 160 | 4 | 43 | daemon_superset |
| `avrone/gateway.py` | 546 | 615 | 25 | 94 | daemon_superset |
| `avrone/governor.py` | 242 | 480 | 30 | 268 | daemon_superset |
| `avrone/greatscale.py` | 128 | 155 | 6 | 33 | daemon_superset |
| `avrone/jobqueue.py` | 153 | 567 | 24 | 438 | daemon_superset |
| `avrone/llm.py` | 115 | 118 | 4 | 7 | daemon_superset |
| `avrone/mcp_client.py` | 454 | 784 | 64 | 394 | review |
| `avrone/mcp_server.py` | 329 | 470 | 13 | 154 | daemon_superset |
| `avrone/memstore.py` | 265 | 501 | 25 | 261 | daemon_superset |
| `avrone/netdiag.py` | 172 | 178 | 6 | 12 | daemon_superset |
| `avrone/osint.py` | 113 | 162 | 13 | 62 | daemon_superset |
| `avrone/plugins.py` | 101 | 156 | 18 | 73 | daemon_superset |
| `avrone/presence.py` | 130 | 300 | 30 | 200 | daemon_superset |
| `avrone/recon.py` | 438 | 576 | 15 | 153 | daemon_superset |
| `avrone/server.py` | 607 | 808 | 60 | 261 | review |
| `avrone/sovereign.py` | 161 | 434 | 24 | 297 | daemon_superset |
| `avrone/static/index.html` | 193 | 193 | 1 | 1 | cosmetic |
| `avrone/telemetry.py` | 209 | 328 | 22 | 141 | daemon_superset |
| `avrone/tools_agenthome.py` | 194 | 241 | 20 | 67 | daemon_superset |
| `avrone/tools_codexstats.py` | 180 | 218 | 21 | 59 | daemon_superset |
| `avrone/tools_extra.py` | 1078 | 1499 | 95 | 516 | review |
| `avrone/tools_flakeprobe.py` | 169 | 234 | 20 | 85 | daemon_superset |
| `avrone/tools_fleetstatus.py` | 199 | 251 | 26 | 78 | daemon_superset |
| `avrone/tools_gatewayview.py` | 84 | 131 | 12 | 59 | daemon_superset |
| `avrone/tools_hostview.py` | 181 | 288 | 30 | 137 | daemon_superset |
| `avrone/tools_introspect.py` | 160 | 200 | 10 | 50 | daemon_superset |
| `avrone/tools_lessonforget.py` | 114 | 167 | 14 | 67 | daemon_superset |
| `avrone/tools_lessonrecall.py` | 102 | 169 | 12 | 79 | daemon_superset |
| `avrone/tools_limits.py` | 240 | 324 | 36 | 120 | daemon_superset |
| `avrone/tools_mempeek.py` | 134 | 154 | 16 | 36 | daemon_superset |
| `avrone/tools_packpeer.py` | 117 | 152 | 20 | 55 | daemon_superset |
| `avrone/tools_researchnote.py` | 141 | 208 | 13 | 80 | daemon_superset |
| `avrone/tools_routerstatus.py` | 171 | 225 | 16 | 70 | daemon_superset |
| `avrone/tools_secrets.py` | 178 | 434 | 33 | 289 | daemon_superset |
| `avrone/tools_statehealth.py` | 156 | 298 | 36 | 178 | daemon_superset |
| `avrone/tools.py` | 242 | 652 | 28 | 438 | daemon_superset |
| `avrone/trinity.py` | 1274 | 1414 | 66 | 206 | review |
| `MASTER_PROMPT.md` | 336 | 340 | 31 | 35 | daemon_superset |
| `pyproject.toml` | 23 | 23 | 1 | 1 | cosmetic |
| `README.md` | 419 | 1634 | 3 | 1218 | daemon_superset |
| `scripts/AVRONE-HANDOFF.md` | 269 | 1052 | 0 | 783 | daemon_superset |
| `scripts/mac-avrone-autopilot.sh` | 629 | 621 | 9 | 1 | cosmetic |
| `tests/test_agentkit_edges.py` | 279 | 281 | 8 | 10 | daemon_superset |
| `tests/test_cov11_a.py` | 81 | 81 | 2 | 2 | daemon_superset |
| `tests/test_cov14_a.py` | 48 | 60 | 1 | 13 | daemon_superset |
| `tests/test_cov15_a.py` | 45 | 58 | 4 | 17 | daemon_superset |
| `tests/test_cov16_a.py` | 64 | 65 | 0 | 1 | cosmetic |
| `tests/test_cov17_a.py` | 24 | 41 | 6 | 23 | daemon_superset |
| `tests/test_cov8_a.py` | 246 | 246 | 2 | 2 | cosmetic |
| `tests/test_cov8_b.py` | 500 | 509 | 3 | 12 | cosmetic |
| `tests/test_cov9_a.py` | 130 | 131 | 1 | 2 | cosmetic |
| `tests/test_edgepack.py` | 584 | 584 | 1 | 1 | cosmetic |
| `tests/test_evolve_loop.py` | 234 | 233 | 2 | 1 | cosmetic |
| `tests/test_evolve.py` | 71 | 70 | 2 | 1 | daemon_superset |
| `tests/test_mcp_client_edges.py` | 508 | 509 | 1 | 2 | cosmetic |
| `tests/test_netdiag_edges.py` | 210 | 210 | 2 | 2 | cosmetic |
| `tests/test_robust_v020_homes.py` | 377 | 377 | 1 | 1 | cosmetic |
| `tests/test_robust_v022_async.py` | 303 | 304 | 0 | 1 | cosmetic |
| `tests/test_robust_v025_state.py` | 58 | 59 | 1 | 2 | daemon_superset |
| `tests/test_robust_v027_access.py` | 77 | 61 | 16 | 0 | daemon_superset |
| `tests/test_robust_v034_replay.py` | 60 | 61 | 0 | 1 | cosmetic |
| `tests/test_skill_r13.py` | 37 | 37 | 1 | 1 | cosmetic |
| `tests/test_tool_env_report.py` | 102 | 95 | 10 | 3 | daemon_superset |
| `tests/test_tool_exposure.py` | 47 | 47 | 1 | 1 | cosmetic |
| `tests/test_tool_packpeer.py` | 106 | 108 | 0 | 2 | cosmetic |
| `tests/test_tool_routerstatus.py` | 108 | 108 | 1 | 1 | cosmetic |
| `tests/test_tool_secrets.py` | 69 | 69 | 1 | 1 | cosmetic |
| `tests/test_tool_state_health.py` | 91 | 93 | 0 | 2 | cosmetic |
| `tools/make_master_prompt.py` | 196 | 194 | 4 | 2 | cosmetic |

---

## Tier-1 port completion (W1 execution, PR avrone-daemon#90)

All four tier-1 items from the open-ports list above are resolved as of the
`port/w1-tier1-platform-bridge` merge into avrone-daemon `main`
(merge commit `84c841fbf0b137570a7517de43714dd8b242d081`, previously based on
`3a1fbc19c867449a3b781f4dc9557b694cd7c974`).

### 1. `avrone/bridge.py` blackboard fan-in: superseded, no port needed
Re-examination of the daemon trunk showed the port target already exists in a
better form. The daemon extracted Avrone's monolithic blackboard section into
`avrone/fusion_blackboard.py` with full contract parity, confirmed by reading
both sides:

| Avrone `bridge.py` element | Daemon equivalent |
| --- | --- |
| post normalization + validation | `normalize_blackboard_post` |
| `blackboard_posts` / acceptance flow | `blackboard_posts`, `blackboard_accept` |
| cross-run fan-in ordering | persistent `_fanin_order.json` with `_load_order` / `_save_order` |
| tombstones | tombstone set carried in the same persistence file |
| `GET /api/blackboard` / `POST /api/blackboard` | `server.py` routes with 413-before-auth, BOM strip, admin token + `AVRONE_ADMIN_TOKEN_PREVIOUS` grace |

The daemon side additionally ships `tests/test_fusion_blackboard.py` (22 KB),
which covers the whole Avrone contract. Conclusion: the one file where Avrone
was the superset was already absorbed by the canonical trunk.

### 2-4. Real gaps, ported in PR #90
The remaining three items were genuine gaps and are now closed (tests are not
executed in this sandbox; validation rides on daemon CI):

1. `plugins/platform_bridge.py` ported verbatim from the Avrone freeze tree
   (verified self-contained against the daemon: only `httpx` and
   `avrone.tools.Tool` imports; daemon `load_plugins` signature and
   `avrone_plugin_<stem>` module naming are compatible). Its test pair came
   with it: `tests/test_platform_bridge.py` (byte-identical to Avrone) and
   `tests/test_platform_bridge_hardening.py`.
2. `.env.example`: platform-bridge env block appended
   (`AVRONE_PLATFORM_TARGETS_JSON`, `AVRONE_PLATFORM_WRITE_ENABLED=false`,
   `AVRONE_BRIDGE_*` knobs). Present on merged main as blob
   `9182e8a70c03df89f2d84b58a9156089bb21d27d`.
3. `tests/test_robust_v027_access.py`: `test_non_ascii_token_is_refused`
   added (Avrone-only regression; daemon `server.caller_allowed` and
   `require_admin` confirmed as the code under test).
4. `tests/test_tool_env_report.py`: `test_json_and_prefix` gained a d3 block
   asserting that a set `AVRONE_ADMIN_TOKEN_PREVIOUS` is reported as
   presence-only (`***set***`) in both env and settings, with the secret value
   itself absent from the JSON dump.

### Ledger
Receipted in `attested-history/history.jsonl` seq 5 (`w1_tier1_ports`,
digest `7c1467aee604ec93f94cff18d49c8ae139a454997a2f477443cdcf21a8d150a3`),
chained after a seq 4 digest repair (stored seq 4 digest did not verify under
the canonical writer algorithm; content and links unchanged). Full 6-entry
chain re-verified after the push.

### Remaining for W1 close-out (owner-manual)
- Flip Avrone to read-only in GitHub repo settings (no API capability for the
  visibility/permission flip in this toolset).
- Archive the 12 zero-risk repos listed in `docs/W1_TRAIN_TRIAGE.md`.
