# Deeznutz — Deezer music downloader web UI

Self-hosted Flask web UI for downloading music from Deezer. Native `deezer-py` integration — no CLI wrappers. Authenticate via ARL cookie, search for tracks/albums/artists/playlists, download with real-time progress, and organise with beets + Navidrome.

## Quick start

### Docker (recommended)

```bash
# Clone
git clone https://fj.n0g.xyz/gravitas/Deeznutz.git
cd Deeznutz

# Set your Deezer ARL cookie (from browser DevTools after logging into deezer.com)
# Premium account required for lossless/high-bitrate
export DEEZER_ARL="your_arl_here"
export SECRET_KEY="your_random_secret"

# Build & run
docker compose up -d --build
```

Open `http://localhost:19290`.

### Local development

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python app/main.py
```

### Auth (ARL cookie)

Deezer uses an ARL (Authentication Remember Login) cookie — no OAuth flow.

1. Log into `deezer.com` in a browser (Premium account required for FLAC).
2. Open DevTools → Application → Cookies → `www.deezer.com`.
3. Copy the `arl` cookie value.
4. Either set the `DEEZER_ARL` environment variable (Docker) or paste it at `/deezer/auth` in the web UI.

The ARL is long-lived and stored in `config/deezer/deezer_token.json`.

## Features

- **Search** — tracks, albums, artists, playlists
- **Download** — single tracks, full albums, entire discographies, and playlists
- **Quality** — FLAC, MP3 320kbps, MP3 128kbps
- **Background worker pool** — LIFO queue with pause/resume/cancel, SSE progress
- **Blowfish/CBC decrypt** — native pycryptodome decryption of Deezer's encrypted audio
- **Metadata tagging** — mutagen (ID3/Vorbis/MP4) with cover art
- **Library organisation** — beets import with dedup and overrides
- **Navidrome integration** — auto-scan trigger after downloads
- **PWA** — installable as a standalone app (manifest + service worker)
- **Import/upload** — extract archives and add to library

## Configuration

All settings via `deeznutz.yaml` or environment variables:

| Env | Default | Description |
|-----|---------|-------------|
| `DEEZER_ARL` | — | ARL cookie (set in env for headless deployments) |
| `DEEZER_QUALITY` | `FLAC` | `FLAC` / `MP3_320` / `MP3_128` |
| `SECRET_KEY` | random | Flask session secret (set in production) |
| `DOWNLOAD_DIR` | `/opt/data/deeznutz` | Download staging directory |
| `LIBRARY_DIR` | `/music` | Music library root |
| `NAVIDROME_URL` | `http://navidrome:4533` | Navidrome instance |
| `BEETSDIR` | `/app/.config/beets` | Beets config directory |

See `deeznutz.yaml` and `compose.yaml` for the full list.

## Dependencies

- **deezer-py** ≥ 1.3 — API client and ARL auth
- **pycryptodome** — Blowfish/CBC decryption
- **mutagen** — audio metadata tagging
- **beets** — music library organisation
- Flask, HTMX, SQLite (WAL), Gunicorn, Docker

## Architecture

Same codebase pattern as the sibling projects [Tidalwave](https://fj.n0g.xyz/gravitas/tidalwave) and [Spotifryer](https://fj.n0g.xyz/gravitas/spotifryer):

- **app/deezer/** — ARL auth, download engine, routes
- **app/search/** — Deezer search with library badge overlay
- **app/download/** — background worker pool, job store, beets integration, discography resolver
- **app/library/** — on-disk library browsing
- **app/collection/** — collection catalogue view
- **app/import_upload/** — archive extraction and import

Detailed architecture: see `AGENTS.md`.

## License

None yet. Personal-use tool.
