# Repo Yard

**Repo Yard** is the shared control plane for **the quad** — four self-hosted music downloader tools that let you *repo* (recover/repossess) your music library from streaming platforms. Each tool started as its own project; this repo is where they unify.

| Tool | Platform | What it does | Live at |
|------|----------|--------------|---------|
| [**spotifryer**](spotifryer/) | Spotify | Downloads tracks/playlists via Spotify Web API metadata + streamrip (ISRC-matched FLAC from Tidal/Qobuz) | [sf.n0g.xyz](https://sf.n0g.xyz) |
| [**qoochie**](qoochie/) | Qobuz | Downloads from Qobuz via native API — search tracks/albums/artists/playlists, token auth | [qz.n0g.xyz](https://qz.n0g.xyz) |
| [**tidalwave**](tidalwave/) | Tidal | Downloads from Tidal via native API | [tw.n0g.xyz](https://tw.n0g.xyz) |
| [**deeznutz**](deeznutz/) | Deezer | Downloads from Deezer via native `deezer-py` — ARL cookie auth, no CLI wrappers | [dz.n0g.xyz](https://dz.n0g.xyz) |

All four share one architecture: **Flask 3 + HTMX** web UI, **SQLite (WAL)** storage, background worker pool for downloads, **beets** library organization, **opus 160k** conversion (MP3 passes through unconverted), and **Navidrome** integration for playback. Each tool has its own `AGENTS.md` in its folder with project-specific architecture and ops notes.

## Goals & objectives

- **Library ownership** — recover high-quality audio of music you have access to, so your collection lives on your own hardware and keeps working if a subscription or account goes away.
- **One platform, one pattern** — four downloaders for four streaming services, deliberately built on the same stack so fixes, features, and ops knowledge transfer between them.
- **Unified operations** — a single control plane (`deploy/` + `app/`) that deploys, health-checks, and rolls back all five services from one place, instead of five bespoke setups.
- **Self-hosted & self-contained** — no third-party download services; each tool talks to the streaming platform's own API with your own account credentials.
- **High-quality output** — downloads land in a beets-managed library and are converted to efficient opus 160k, ready for Navidrome.

## Repository structure

```
repo-yard/
├── app/                 # Control-plane web app (search aggregator across the quad)
├── deploy/              # One deploy script, status report, rollback ledger
│   └── deploy.sh        #   deploy.sh [--dry-run] <tool|all>
├── docs/                # deploy.md (workflow) + playbook.md (ops runbook)
├── music-hub/           # Static index page linking the four services
├── tests/               # Shared test suite
├── spotifryer/          # Spotify downloader (embedded copy)
├── qoochie/             # Qobuz downloader (embedded copy)
├── tidalwave/           # Tidal downloader (embedded copy)
└── deeznutz/            # Deezer downloader (embedded copy)
```

## Deployment & operations

- **Code source of truth:** the individual tool repos on Forgejo (`fj.n0g.xyz/gravitas/<tool>`). The folders in this repo are embedded copies.
- **Deploy:** `deploy/deploy.sh [--dry-run] <spotifryer|qoochie|tidalwave|deeznutz|music-hub|repo-yard|all>` — smart rebuild, health gate, auto-rollback. See [`docs/deploy.md`](docs/deploy.md).
- **Runbook:** anything-goes-wrong reference in [`docs/playbook.md`](docs/playbook.md).
- **Topology:** five containers on the Docker host (`cthulhu`), routed by Traefik on `singularity` as `*.n0g.xyz`.

## Status

> **Mirror notice:** this GitHub repository is a snapshot mirror. The canonical source of truth lives at [`fj.n0g.xyz/gravitas`](https://fj.n0g.xyz/gravitas) (Forgejo). The tool folders here are plain copies of their committed state, not live checkouts.
