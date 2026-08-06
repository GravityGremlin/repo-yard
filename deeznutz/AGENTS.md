# Deeznutz

Flask web UI for downloading music from Deezer. ARL cookie auth (no OAuth), native Blowfish/CBC decrypt via `deezer-py` + `pycryptodome`. Background worker pool (8 threads), beets library organization, Navidrome integration.

**Status**: Live at `https://dz.n0g.xyz` — deployed on cthulhu.

## Stack

Python 3.12 (Docker) / 3.14 (local venv), Flask 3.1, HTMX, SQLite (WAL), Gunicorn (1 worker × 8 threads), Docker.

## Project structure

```
deeznutz/
├── deeznutz.yaml              # Runtime configuration
├── compose.yaml                # Docker Compose
├── Dockerfile                  # Container image (python:3.12-slim + ffmpeg)
├── entrypoint.sh               # Prod startup — ARL restore + gunicorn
├── requirements.txt            # Python deps
├── pytest.ini                  # Test config
├── README.md                   # Human-facing docs
├── AGENTS.md                   # This file
├── .env.example                # Env template
├── .env                        # Secrets (gitignored, on cthulhu only)
├── app/
│   ├── main.py                 # Flask entrypoint (debug server, FLASK_PORT)
│   ├── factory.py              # App factory, blueprint registration, startup
│   ├── config.py               # Env > YAML > default config resolution
│   ├── models.py               # SQLite job store (dataclass-based)
│   ├── security.py             # Path traversal prevention
│   ├── proxy.py                # Per-job proxy support
│   ├── logging_config.py       # Structured logging
│   ├── lyrics.py               # Deezer track lyrics via gw.get_track_lyrics()
│   ├── deezer/                 # ★ Deezer-specific
│   │   ├── session.py          # ARL auth, login_via_arl(), token persistence
│   │   ├── downloader.py       # DeezerDownloader — full download pipeline
│   │   └── routes.py           # /deezer/auth UI
│   ├── search/routes.py        # Search tracks/albums/artists/playlists
│   ├── download/               # Worker pool, job control, beets import, discography
│   │   ├── controller.py       # LIFO queue + worker threads + SSE subscribers
│   │   ├── beets_integration.py# `beet import` subprocess wrapper
│   │   ├── discography.py      # Artist discography expansion
│   │   └── routes.py           # /download enqueue, SSE progress, job control
│   ├── playlist/routes.py      # Playlist URL resolve + batch download
│   ├── library/                # Browse/serve from music library
│   ├── collection/             # Collection catalog view
│   ├── audit/routes.py         # Audit log
│   ├── stats/routes.py         # Stats dashboard
│   ├── import_upload/          # File upload archive import (own worker thread)
│   ├── templates/              # Jinja2 HTMX (13 full pages + 16 partials)
│   └── static/                 # PWA assets, CSS, JS
├── config/
│   ├── deezer/                 # ARL token storage (gitignored)
│   └── beets/                  # Beets config
├── data/                       # SQLite DB + queue state (gitignored)
├── downloads/                  # Download staging
├── import_staging/             # Upload extraction temp
└── tests/                      # 88 tests across 9 files — all green
```

## Architecture

- **Auth**: ARL cookie — user pastes from browser DevTools or sets `DEEZER_ARL` env.
  `login_via_arl()` validates via `deezer.Deezer().login_via_arl()`, persists to
  `config/deezer/deezer_token.json`. `bootstrap_env_arl()` auto-logins at startup
  when `DEEZER_ARL` is set (container deployments).
- **Session lifecycle**: `get_session()` caches per-request via Flask `g`;
  `init_session()` for worker threads. Both return `deezer.Deezer` instances.
- **Search**: `gw.search(query)` → category-keyed response (ARTIST/ALBUM/TRACK/PLAYLIST).
  `_parse_search_results()` groups by type, builds template-friendly dicts.
- **Download**: `DeezerDownloader` class — `download_track/album/playlist`.
  Pipeline: metadata → TRACK_TOKEN + TrackFormat → encrypted CDN URL → HTTP stream
  → Blowfish/CBC decrypt (pycryptodome) → mutagen tag embedding (ID3/Vorbis/MP4).
  Free tier: MP3_128 only. Premium: FLAC/MP3_320/MP3_128.
- **Decryption**: `_derive_blowfish_key()` (MD5 halves XOR), `_decrypt_chunk()` (Blowfish/CBC
  on first 2048 bytes per block). Static secret: `g4el58wc0zvf9na1`.
- **Worker pool**: In-process daemon threads (LIFO queue, `MAX_CONCURRENT`=8) + one
  separate import worker for uploads. No Celery/RQ. Progress via SSE.
- **CSRF**: flask-seasurf protects ALL POST/PUT/DELETE routes. Any new POST route
  automatically requires a CSRF token — including in tests via the test client.
- **Staging → library**: Files downloaded to `downloads/`, promoted to `LIBRARY_DIR`,
  optionally organized via `beet import`. Beets import polls briefly for staged audio
  (`_wait_for_staged_audio`) because bind-mounted/NFS dirs can lag behind writes.
- **Navidrome**: Auto-scan trigger after downloads (`http://navidrome:4533` or configured).

## Deployment

**Host**: cthulhu (10.8.0.10) — runs alongside tidalwave, spotifryer, butterchurn.
**Route**: Traefik on singularity (10.8.0.1) — `dz.n0g.xyz` → `http://10.8.0.10:19294`.
**Port**: Container 19290 → host 19294 (19290-19293 already taken by tidalwave/moidify/butterchurn/spotifryer).

**Startup flow**:
1. `entrypoint.sh` — checks for ARL token backup at `/opt/backups/deeznutz-auth/`;
   restores if found; warns if missing.
2. Gunicorn starts, Flask `create_app()` runs:
   - Directories created: `DOWNLOAD_DIR`, `JOBS_DIR`, `DEEZER_CONFIG_DIR`
   - `bootstrap_env_arl()` — if `DEEZER_ARL` env set, validates and persists token
   - `start_worker_pool()` — 8 download threads
   - `start_import_worker()` — 1 upload-processing thread
   - `recover_interrupted_jobs()` — resume any queued/running jobs from last shutdown

**Deploy command** (on cthulhu as `agent`):
```bash
cd ~/Projects/deeznutz
docker compose up -d --build
```

**Post-change deploy checklist** — run after any code or design changes:
1. `git add -A && git commit -m "<message>" && git push origin main`
2. `ssh cthulhu "cd ~/Projects/deeznutz && git pull origin main && docker compose up -d --build"`
3. `ssh cthulhu "docker ps --filter name=deeznutz --format '{{.Status}}'"` — confirm healthy

## Config

`deeznutz.yaml` — overridden by env vars at container level:

| Env | Default | Where |
|-----|---------|-------|
| `DEEZER_ARL` | — | ARL cookie (set in `.env`, read at startup) |
| `DEEZER_QUALITY` | `FLAC` | `FLAC` / `MP3_320` / `MP3_128` |
| `DEEZER_CONFIG_DIR` | `~/.config/deeznutz_dz` | Token file directory |
| `SECRET_KEY` | random | Flask session secret |
| `DOWNLOAD_DIR` | `/opt/data/deeznutz` | Download staging |
| `LIBRARY_DIR` | `/music` | Music library root |
| `BEETSDIR` | `/app/.config/beets` | Beets config dir |
| `NAVIDROME_URL` | `http://navidrome:4533` | Navidrome instance |
| `FLASK_PORT` | `19290` | Internal container port |
| `GUNICORN_WORKERS` | `1` | Workers (keep at 1 for SQLite) |
| `GUNICORN_THREADS` | `8` | Threads per worker |

The `.env` file on cthulhu (gitignored) contains `SECRET_KEY` and `DEEZER_ARL`.

## Traefik route

On singularity: `/opt/stacks/traefik/config/dynamic/dz.yaml`
```yaml
http:
  routers:
    deeznutz:
      rule: Host(`dz.n0g.xyz`)
      entryPoints: [websecure]
      service: deeznutz
      tls:
        certResolver: porkbun-resolver
  services:
    deeznutz:
      loadBalancer:
        servers:
          - url: http://10.8.0.10:19294
        passHostHeader: true
```

## Development

```bash
# One-time
python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt

# Run — debug server on FLASK_PORT (19290) with TEMPLATES_AUTO_RELOAD
.venv/bin/python app/main.py

# Tests
.venv/bin/python -m pytest
```

- Use `.venv/bin/python` (or activate the venv) — `python` is not on PATH and the
  system interpreter is Python 3.14 vs the container's 3.12.
- **Host mode path override**: when running on the host (not in container), `config.py`
  forces project-local dirs (`./downloads`, `./music`, `./data`) and ignores the
  container paths in `deeznutz.yaml`. Only `DEEZER_CONFIG_DIR` honors the YAML on host.

## Testing

88 tests across 9 files (`test_smoke`, `test_search`, `test_deezer_auth`,
`test_deezer_download`, `test_discography`, `test_beets_integration`,
`test_import_upload`, `test_job_management`, `test_library_access`).
**Full suite is green** — run `.venv/bin/python -m pytest`.

Historical infra issues (now fixed, keep them fixed):
- `tests/conftest.py` must mock background threads at the source module:
  `app.download.controller.start_worker_pool` / `recover_interrupted_jobs`,
  `app.import_upload.controller.start_worker`, and `app.deezer.session.bootstrap_env_arl`
  — otherwise `create_app()` starts real worker threads that grab test jobs.
- Conftest resets BOTH `app.models._db_initialized` and `app.models._conn` per test
  (a cached module-level connection leaks the first test's DB into later tests).
- Conftest patches module-level path bindings, not just `app.config`: `LIBRARY_DIR`
  is bound at import time in `library/routes.py`, `library/scan_cache.py`,
  `search/routes.py`, `collection/routes.py` (patch each or tests are order-dependent).
- POST-based tests need CSRF handled: conftest disables it via
  `flask_seasurf.SeaSurf._before_request` patch (CSRF_DISABLE alone is not enough —
  SeaSurf caches the flag at init_app time).

To verify focused work, run a single file rather than the full suite:
```bash
.venv/bin/python -m pytest tests/test_search.py
.venv/bin/python -m pytest tests/test_deezer_download.py -k album
```

Test isolation is via `tests/conftest.py` — monkeypatches paths across
`app.config` and each consumer module, and resets `_db_initialized`/`_conn`. New tests
should follow the same pattern: patch the source module, not the factory.

## Gotchas

- **Free vs Premium**: Free accounts get MP3_128 only. FLAC/MP3_320 require Premium ARL.
- **TrackFormats are strings (not ints)**: `dz.get_track_url(token, "MP3_128")` — the enum
  values are ints but the internal comparison in deezer-py 1.3.7 uses string comparisons.
- **ARL is sensitive**: `.gitignore` protects `config/deezer/` and `.env`.
- **Single Gunicorn worker**: Required for SQLite WAL mode correctness.
- **No `traefik.*` Docker labels**: Routing is file-based on singularity.
- **Port conflict on cthulhu**: Do not use 19290-19293 on host — reserved for tidalwave,
  moidify, butterchurn, spotifryer.
- **Backup dir** (`/opt/backups/deeznutz-auth`): May need root to create. Non-fatal —
  entrypoint skips restore on missing backup and falls back to `DEEZER_ARL` env.
- **Shell on singularity is fish**: Use `bash -c` for multi-line SSH commands.
- **`DEEZER_ARL` vs `DEEZER_CONFIG_DIR` env vars**: The `DEEZER_ARL` is a runtime secret for auth; the `DEEZER_CONFIG_DIR` specifies where to store the token file. These are separate concerns and should not be confused. The token file is read and written by the session module.
- **CSRF on all POST routes**: flask-seasurf wraps every POST/PUT/DELETE. When adding
  a route or writing tests, expect 403s without a token.
- **`app/models._lock` must stay an `RLock`**: `save_job()`/`delete_job()` hold `_lock`
  while calling `_connect()`, which re-acquires it on the first write (`_conn is None`).
  A plain `threading.Lock()` deadlocks there — this is a real prod bug that was caught
  by tests hanging after the conftest `_conn` reset.

## Remote

- Repo: `https://fj.n0g.xyz/gravitas/Deeznutz`
- Forgejo. Credentials via `FORGEJO_TOKEN` in `~/.secrets` (git credential helper).
- Parent projects: Tidalwave, Spotifryer (same architecture, different sources).
