# Crow-OS AVRONE Workspace

Self-hosted coordination board for the ongoing AVRONE workflow: run longer, more robust edge-case rounds, keep the evidence, and fix along the way.

The board reads **this Avrone checkout**. It does not ship sample rounds.

## Start

From the repository root:

```bash
python apps/crow_os_workspace/run.py
```

Then open http://127.0.0.1:8765. Direct local development is unauthenticated by default; keep it on loopback.

From `apps/crow_os_workspace/` the same command is `python run.py`.

Optional environment:

| Variable | Role |
|---|---|
| `GITHUB_TOKEN` or `GH_TOKEN` | Load pull requests and GitHub check runs. Without it, the UI says the token is missing and still shows local git data. |
| `OLLAMA_HOST` | Ollama HTTP API. Default `http://localhost:11434`. The server also looks for an `ollama` binary on `PATH`. If the API does not answer, the UI says **Ollama unreachable** and names whether the binary was found. |
| `OLLAMA_MODEL` | Model name for an advisory round. Chat agents use the model saved on the agent, otherwise the first model from `/api/tags`. |
| `CURSOR_API_KEY` | Optional Cursor cloud-agent backend (`POST /v1/agents`). Without it, that backend says the key is missing and does not call the API. |
| `XAI_API_KEY` | Optional Grok backend (`POST https://api.x.ai/v1/chat/completions`). Without it, Grok says the key is missing. |
| `XAI_MODEL` | Grok model id. Default `grok-4.7`. |
| `CROW_REPO` | Checkout to ingest. Defaults to the nearest parent Git checkout. In Docker this is `/repo`. |
| `CROW_PORT` | Listen port. Default `8765`. |
| `CROW_HOST` | Listen address. Default `127.0.0.1`. |
| `CROW_OPEN_BROWSER` | Set to `0` to skip opening a browser. Default opens `http://127.0.0.1:8765`. |
| `CROW_AUTH_REQUIRED` | Enable HTTP Basic authentication. Docker enables it; direct loopback development defaults off. |
| `CROW_AUTH_USERNAME` / `CROW_AUTH_PASSWORD` | Workspace login. The password is mandatory for the Docker profile and must be stored only in `.env`/deployment secrets. |
| `CROW_HOME` | Persistent app state root. Docker uses `/opt/crow-os`. |
| `CROW_WORKTREES` | Detached chat/test worktree root. Docker keeps it under the persistent `var/` mount. |

On startup the server reads host toolchain versions, repo-local git config, `.env` / `pyproject.toml` when present, and Ollama's `/api/tags`. Token-like values are masked before they are stored or shown. Runs inherit the host process environment; the System tab shows the masked view.

## Chat

The Chat tab is the front door: agents and threads on the left, the transcript in the center, and tool activity plus a live log on the right. Coordination views (branches, pull requests, runs, gates, and the rest) stay available as tabs.

A local Ollama agent gets a tool loop confined to a detached git worktree on a `crow-os/chat-…` branch. It can list, read, and write files, commit a patch, and run pytest with no harness timeout. The patch is registered as a change that stays **unmerged** and **unverified** until the same acceptance gates pass. Cancel stops the turn. Settings lists installed models, sets a default model per agent, and can pull a model through `POST /api/pull`.

Cursor and xAI agents use the same chat. They are skipped, with the reason on screen, when their keys are absent.

## Docker deployment

The Docker profile is the recommended self-hosted deployment. It fixes the repository mount at `/repo`, persists the SQLite database, event log and detached worktrees in `var/`, passes optional connector credentials into the container, restarts unless stopped, and exposes an authenticated health-checked service.

From this directory (`apps/crow_os_workspace/`):

```bash
cp .env.example .env
mkdir -p var
```

Edit `.env` before starting:

- Set `CROW_AUTH_PASSWORD` to a strong unique password. Generate one with `openssl rand -hex 32`. The Docker profile refuses to start without it.
- Set `CROW_AUTH_USERNAME` to the username shown in the browser's Basic Auth prompt.
- Set `CROW_UID` and `CROW_GID` to the owner of the checked-out repository (`id -u` and `id -g`). This lets the container update Git refs without changing host file ownership.
- Add a read-only `GITHUB_TOKEN` or `GH_TOKEN` to load pull requests and Actions checks. Add `CURSOR_API_KEY` and/or `XAI_API_KEY` only when those backends are enabled.
- Keep `CROW_PUBLISH_ADDR=127.0.0.1`. Docker binds only to loopback; remote access must go through an authenticated HTTPS reverse proxy or private tunnel.

Then validate and launch:

```bash
docker compose config
docker compose up -d --build
docker compose ps
docker compose logs -f crow-os
```

Open `http://127.0.0.1:8765`. The browser asks for the Basic Auth username/password. The health endpoint `GET /api/health` is intentionally minimal and unauthenticated for Docker healthchecks; every other route, including static assets, API calls and SSE streams, requires authentication. API responses send basic hardening headers and are not cached.

The container runs with a read-only root filesystem, a temporary `/tmp`, dropped Linux capabilities and `no-new-privileges`. Only the repository bind mount and the app's `var/` directory are writable. The repository is mounted from `../..` (the actual Git root), not the `apps/` directory. Do not mount the Docker socket into this workspace service.

### Remote access

Do not publish port 8765 directly to the Internet or set `CROW_PUBLISH_ADDR=0.0.0.0`. Put a reverse proxy or private access tunnel in front of the loopback port, terminate TLS there, and preserve the HTTP `Authorization` header. If the proxy strips that header, browser login and API requests will fail closed.

### Persistence and backups

SQLite, the append-only event log and detached worktrees live in `apps/crow_os_workspace/var/`. The directory is gitignored and survives container replacement. Stop the service before taking a consistent file-level backup:

```bash
docker compose down
tar -czf crow-os-workspace-var-$(date +%Y%m%d-%H%M%S).tar.gz var
docker compose up -d
```

Host Ollama is reached at `host.docker.internal:11434` by default. The workspace does not install or start Ollama for you. Pull at least one model on the host and confirm `GET /api/ollama` reports it reachable before expecting local chat agents or advisory rounds to work.

## What is real

- Branches, commit subjects, and ahead/behind counts come from `git for-each-ref` and `git log`.
- Test files and evidence/JSON artifacts are git blobs. The SHA-256 is computed here from those bytes. Syntax errors are real `compile()` failures, including `avrone-a12lr/tests/test_avrone_abyss_edge.py` on `origin/avrone-a12lr-daemon` when that ref is present.
- Deep ingest covers the current branch, `main`, `origin/main`, `origin/avrone-a12lr-daemon`, `origin/edge/*`, and `origin/squad/round*`. Other branches are listed from git but their trees are not hashed until a refresh that includes them (open PR heads are included once a token is set and the ref exists locally).
- A Python round checks out the chosen commit with `git worktree add --detach` under `/tmp/crow-os-worktrees` and runs `pytest` or the script. The main worktree is not checked out. There is no harness timeout. **Cancel round** signals the process group.
- Round inputs, environment, commit SHA, logs, and hashes stay on the round row. Resume only continues files that did not finish. A sealed pass/fail round cannot be resumed; launch another round to re-verify.
- Ollama rounds call `GET /api/tags` and, when a model is present, `POST /api/generate`. The text is stored as advisory. It cannot satisfy the suite gate.

## What stays dark without extra services

- **Pull requests and CI checks** need `GITHUB_TOKEN` or `GH_TOKEN`. The app will not read credentials out of the git remote URL.
- **Ollama** needs a reachable `OLLAMA_HOST` (default `http://localhost:11434`) and a pulled model. A missing binary or a refused connection is shown as unreachable. Advisory text and chat replies are not invented.
- **Cursor cloud agents** need `CURSOR_API_KEY`. **Grok** needs `XAI_API_KEY`.
- Sibling services (CycleKernal, capability-authority, the fusion bridge, avrone-daemon) are outside this repository and are not shown as live status.

## Status rule

Chips used everywhere: `verified`, `unverified`, `blocked`, `draft`, `open`, `failed`, `unmerged`, `merged`.

`verified` is computed. It appears only when every acceptance gate passes:

1. Every acceptance file compiles.
2. Recorded failures are cleared by a later passing Python run. Linking a commit or PR does not clear one.
3. One local Python acceptance round on the current commit passes every acceptance file.
4. That round's log and per-file logs have SHA-256 hashes.
5. When GitHub checks were loaded for a pull request, those checks are green.
6. The operator has attested the evidence.

The **Record verification** button calls the same check and returns HTTP 409 with the blocking reasons otherwise. Automatic gates have no toggle. A change that GitHub has not merged stays `unmerged` even after it is verified. A commit already contained in `main` shows `merged` for that git fact, and still stays `unverified` until the gates pass.

A SyntaxError or a crashing test is stored with its trace, timestamped in the anomaly log, and opened as a fix item. The next required gate stays on that failure until a re-run passes.

## Tests

```bash
cd crow-os-workspace
python -m pytest
```

Covers ingestion hashes, the verification gate, resume, comparison, cancel, and the fix loop.

SQLite and logs live in `apps/crow_os_workspace/var/` (gitignored). The append-only event log is the `events` table. The Docker profile persists this directory across container replacements.
