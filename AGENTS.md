# Repo Yard — AGENTS.md

Cross-project context for the unified next phase of the quad (Repo Yard).

## The Quad (Repo Yard)

Four music downloader tools that form the **Repo Yard** — utilities to repo (recover/repossess) your music library from streaming platforms. Each started as a separate project; repo-yard is where they unify.

| Tool | Source (this repo) | Live repo (fj.n0g.xyz/gravitas/) | Dev port | Live at |
|------|--------------------|-----------------------------------|----------|---------|
| **spotifryer** | `spotifryer/` | `spotifryer.git` | 19291 | sf.n0g.xyz |
| **qoochie** | `qoochie/` | `qoochie.git` (README says `Qoochie` — case-tolerant) | 19290 | qz.n0g.xyz |
| **tidalwave** | `tidalwave/` | `tidalwave.git` | 19290 | tw.n0g.xyz |
| **deeznutz** | `deeznutz/` | `Deeznutz.git` | 19290 | dz.n0g.xyz |

All four share the same architecture: Flask 3 + HTMX, SQLite (WAL), background worker pool, beets library organization, opus 160k conversion, Navidrome integration. Each has its own `AGENTS.md` with project-specific commands, gotchas, and architecture notes in its root (`tidalwave/` is the only one without a `README.md`).

## Working here

- The four tool directories (`spotifryer/`, `qoochie/`, `tidalwave/`, `deeznutz/`) are **relative symlinks to the live repos** (`spotifryer -> ../spotifryer`, etc.) — no nested `.git`, no committed tool code in this repo. The live repos under `Projects/` are the single source of truth and the dirs are **always in sync** — there is no snapshot to refresh. **Fix a tool bug in `Projects/<tool>`; it shows up here automatically.** The symlinks require the sibling layout (`Projects/<tool>` next to `Projects/repo-yard`), which is how both dev and cthulhu are arranged; on a fresh clone without the siblings, the links dangle until the tool repos exist.
- `music-hub/` is the one **real committed directory** — it has no git repo upstream, and `deploy/deploy.sh` treats the repo-yard copy as the deployable artifact (static sync source). Keep it edited in place here (mirror to `Projects/music-hub` only if you use that copy).
- repo-yard's first app is the **unified search aggregator** (`app/`): queries all four tools' `/search/json` endpoints in parallel, normalizes to one schema, dedups by ISRC, serves an HTMX UI. Test: `python3 -m pytest tests/ -q` (stubbed, no network). It runs on port 19297 (`app/main.py`).
- Cluster ops context (hosts, SSH discipline, secrets, Traefik routing, remotes) lives in `~/agent-core/` — its `AGENTS.md` is the cluster hub. Read it before host-level or cluster-wide work.
- **Root `.gitignore` covers only repo-yard's own Python artifacts** (`__pycache__/`, `*.pyc`, `.venv/`). Because the tool dirs are symlinks, git tracks only the link itself — it never sees the tools' `data/`, `downloads/`, or config secrets, so those can't leak into this repo. `git add` on a symlinked dir adds the link, not its contents.

## Deployment

The ops layer lives in `deploy/` — repo-yard is the control plane, tool repos stay code-only:

- `deploy/deploy.sh <service|all>` — the **only** deploy path: pull → smart rebuild (only when `requirements.txt`/`Dockerfile`/`entrypoint.sh`/`compose.yaml` change) → `compose up -d` → health gate (HTTP 2xx/3xx + no traceback) → auto-rollback on failure. Runs on cthulhu; self-forwards via `ssh cthulhu` from anywhere (`--dry-run` to preview). Pulls via each repo's configured upstream — tool remotes are named `origin` **or** `forgejo`, don't hardcode `origin`.
- `deploy/status.sh` — deployed SHA, container status, and HTTP health per service.
- `deploy/services.conf` — the fleet registry (repos, host ports, health paths); update it if ports move.
- `deploy/log/deploy.log` — rollback ledger, gitignored via `deploy/.gitignore`.
- Workflow: edit in the live repo (`Projects/<tool>`) → it's already in sync here via symlink → commit here if `app/`, `deploy/`, `docs/`, or `music-hub/` changed → `ssh cthulhu "cd ~/Projects/repo-yard && ./deploy/deploy.sh <service>"`.
- Full workflow: `docs/deploy.md`; ops runbook: `docs/playbook.md`.

## Repo Yard — the vision

The individual tools are per-platform modules; Repo Yard is the **control plane** that unites them:

- One self-hosted web UI over all platforms, running as an always-on service
- One pipeline: download → beets organize → opus 160k → Navidrome
- One ops story: Docker + Traefik, worker pools, queued jobs, status dashboards
- Library-grade product layer: cross-platform dedup, "repo my whole library" migrations, credential + status management

## Edition policy

When an album has multiple editions (standard / deluxe / anniversary / remaster / reissue), the **default pick is the edition with the most tracks OR the most recent release** (whichever satisfies first when they conflict — most tracks wins ties on recency, most recent release wins when track counts are equal). This keeps the default library as complete as possible and matches what most users want from a "repo" of an album.

- Users can opt into a **"first release"** version via a checkbox — when set, the tool prefers the earliest original release (original mastering / original year) over track-count-maximizing editions.
- The policy applies at download/queue time (which edition URL gets selected) and at dedup time (which existing copy is kept when editions collide on ISRC/title).
- Not yet implemented in tool code — this documents the decision; implementation lands per-tool in the search/queue edition-selection path.

Direction from the 2026 landscape review: **don't reinvent downloader engines** — streamrip, Devine/Unshackle, Deemix, and yt-dlp tooling are commoditized; borrow them. The whitespace is the integrated suite (multi-platform coverage + self-hosted UI + organized pipeline). Legal posture: recover *your* library; prefer non-DRM / account-auth routes (streamrip-style) over Widevine-key territory.

## Music Hub

Landing page at `music-hub/` (static `index.html`, `style.css`, `compose.yaml`; served by nginx). Amber-phosphor CRT terminal theme: "Repo Yard" — four tools to repo your library.

Its source of truth is `Projects/music-hub/` — a plain directory with **no git repo**. The copy here drifts: keep the two in sync manually (rebrand/copy edits have to be applied in both places).

## What does NOT go here

- Anyone's `.git` history, secrets, `.env`, auth tokens, data/database files, or downloads
- Local state (`data/`, `downloads/`, `config/*/secret` files) — the tracked `queue_order.json` / `.gitkeep` placeholders are the only exceptions

## Workspace layout

```
Projects/
├── spotifryer/       # Live source of truth: gravitas/spotifryer
├── qoochie/          # Live source of truth: gravitas/qoochie (README: Qoochie)
├── tidalwave/        # Live source of truth: gravitas/tidalwave
├── deeznutz/         # Live source of truth: gravitas/Deeznutz
├── music-hub/        # Static landing, no git repo (plain copy source)
└── repo-yard/        # ← YOU ARE HERE: unification project (fj remote: gravitas/repo-yard)
    ├── spotifryer -> ../spotifryer   (symlink; same for qoochie, tidalwave, deeznutz)
    ├── music-hub/                    (real committed copy — deploy sync source)
    ├── app/  deploy/  docs/  tests/
```
