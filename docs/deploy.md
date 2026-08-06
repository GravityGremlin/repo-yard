# Repo Yard — Deployment

Repo-yard is the ops control plane for the quad + music-hub. The tool repos on
`fj.n0g.xyz/gravitas/` stay the **source of truth for code**; `deploy/` here owns
the orchestration: one deploy script, one status report, and a rollback ledger.

## Topology

| Machine | Role |
|---|---|
| **cthulhu** (10.8.0.10) | Docker host — all five containers run here (`~/Projects/<service>`) |
| **singularity** (10.8.0.1) | Traefik — routes `*.n0g.xyz` → cthulhu host ports |
| **fj.n0g.xyz** | Git remote (`gravitas/<tool>`) — source of truth for tool code |

## Deploy script

```bash
./deploy/deploy.sh [--dry-run] <spotifryer|qoochie|tidalwave|deeznutz|music-hub|repo-yard|all>
```

Per service it: pulls the repo (or syncs files for music-hub) → **smart rebuild**
only when `requirements.txt` / `Dockerfile` / `entrypoint.sh` / `compose.yaml`
changed → `docker compose up -d [--build]` → **health gate** (host-port route
answers 2xx/3xx within 120s, no traceback in logs) → **auto-rollback** to the
previous SHA/files if the gate fails. Every run is appended to
`deploy/log/deploy.log`.

The script runs on cthulhu. From anywhere else it self-forwards:
`ssh cthulhu` (alias via `CTHULHU_SSH`), repo-yard expected at the same path
there (override `REPO_YARD_REMOTE_DIR`). To run it as the Docker host directly,
set `REPO_YARD_ON_CTHULHU=1`.

`deploy.sh repo-yard` deploys the **unified search aggregator** itself (its own
`compose.yaml` at the repo root builds the `app/` search service on port
19297). Because the aggregator reaches the four tools over HTTP
(`REPO_YARD_<TOOL>_URL` env vars, see `compose.yaml`), deploy the tool
endpoints **before** the aggregator: each tool's `/search/json` ships with the
tool, and `repo-yard` consumes it.

## Standard update workflow

1. **Edit code in the live repo** — `Projects/<tool>`; the repo-yard tool dirs
   are symlinks to those repos, so they're always in sync. For music-hub, edit
   the repo-yard copy (it has no git; it's the deployable artifact — see AGENTS.md).
2. **Commit in repo-yard** if `app/`, `deploy/`, `docs/`, or `music-hub/`
   changed (never `git add -A`; the tool dirs are symlinks — git tracks only
   the link, and there is no root `.gitignore` beyond repo-yard's own artifacts;
   `deploy/log/` is ignored via `deploy/.gitignore`).
3. **Deploy**:
   ```bash
   ssh cthulhu "cd ~/Projects/repo-yard && ./deploy/deploy.sh <service>"
   # dry-run first:
   ssh cthulhu "cd ~/Projects/repo-yard && ./deploy/deploy.sh --dry-run <service>"
   ```
4. **Verify**:
   ```bash
   ssh cthulhu "cd ~/Projects/repo-yard && ./deploy/status.sh"
   ```
   For visual/UX changes, also open the live route and check the page
   (scripted gates can't see the UI).

## First-time bootstrap on cthulhu

```bash
# on cthulhu as agent:
git clone https://fj.n0g.xyz/gravitas/repo-yard.git ~/Projects/repo-yard
docker compose version          # compose v2 required
# per-tool .env files must exist (gitignored secrets); tokens are restored
# from /opt/backups/<tool>-* by each entrypoint.sh if missing
```

The four tools + music-hub are already deployed today (docker compose up -d
per tool). `deploy.sh` adopts that state: first run of a tool with no new
commits just re-ensures the container is up and health-checks it.

## Music hub specifics

- No git repo — `deploy.sh music-hub` rsyncs the **repo-yard copy**
  (`music-hub/`) over the live dir (`~/Projects/music-hub` on cthulhu,
  override `STATIC_TARGET`) and re-runs `compose up -d`.
- Because the deploy artifact is the repo-yard copy, keep it fresh from
  `Projects/music-hub` before deploying (AGENTS.md sync rule).
- Rollback restores the previous files from `deploy/log/music-hub.prev/`.

## Rollback (automatic and manual)

- **Automatic**: a failed health gate rolls the service back to the previous
  SHA (git) or file set (music-hub) and re-verifies. Check `deploy.log` for the
  outcome — if rollback also failed, intervene manually.
- **Manual**: `git -C ~/Projects/<tool> checkout <good_sha> && docker compose up -d`
  on cthulhu. Previous SHAs are in `deploy/log/deploy.log`.

## Guidelines

- `deploy.sh` is the **only** deploy path — no ad-hoc `docker compose up -d --build`
  except emergencies. It fails closed: clean pull, health gate, no unlogged deploys.
- Never commit `.env`, tokens, `data/`, `downloads/`, or `deploy/log/`.
- Deploy `all` in CI/automation is fine; for a broken single service, deploy
  just that one — the loop continues past a failed service but exits non-zero.
- Details, debugging, and the runbook: `docs/playbook.md`.
