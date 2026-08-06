# Qoochie

Flask web UI for downloading music from Qobuz. Token-based auth, native Qobuz API integration. Background worker pool (8 threads), beets library organization, Navidrome integration.

## Stack
Python 3.12 (Docker image and current local venv), Flask 3.1, HTMX (vendored at `app/static/js/htmx.min.js`, not from CDN), SQLite (WAL), Gunicorn (1 worker × 8 threads).

## Key commands
```bash
# Local dev
source .venv/bin/activate
python app/main.py            # debug server on FLASK_PORT (19290)

# Tests — all mocked; no network, Qobuz token, or live services needed
pytest -q                     # 94 tests, ~6s
pytest tests/test_search.py::test_name -q   # single test

# Docker (prod-like)
docker compose up -d --build
```

## Project structure
```
app/
├── main.py               # Entrypoint: create_app() + debug server (0.0.0.0:FLASK_PORT)
├── factory.py            # App factory, blueprint registration, SeaSurf CSRF init
├── config.py             # Env > qoochie.yaml > default; ★ host vs container path split
├── models.py             # SQLite job store (dataclasses + module-level conn, WAL)
├── qobuz/                # session.py (token auth/signing), downloader.py, routes.py
├── download/             # controller.py (LIFO worker pool), routes.py, discography.py, beets_integration.py
├── search/ playlist/ library/ collection/ stats/ import_upload/ audit/
│                         # One blueprint package per feature; routes.py holds the bp
└── templates/ static/    # Jinja2 + HTMX partials, CSS/JS/PWA
config/qobuz/             # qobuz_token.json — contents gitignored, never commit
data/                     # jobs.db (WAL) + queue_order.json — gitignored
```

## Gotchas
- **Path resolution is environment-dependent.** `config.py` sets `_IS_CONTAINER` = `/app/config/qoochie.yaml` exists. On the host it **ignores** qoochie.yaml's container paths and hardcodes project dirs (`downloads/`, `music/`, `data/`); container paths (`/app/downloads`, `/music`, `/app/data`) apply only inside Docker.
- **Config order**: env var > qoochie.yaml > default (via `_cfg()` in config.py). Quality names map to ordered Qobuz `format_id` fallback lists (`QUALITY_FORMAT_MAP`: HIGH→[6,5], LOSSLESS→[7,6,5], HIRES→[27,7,6,5], MP3→[5]; YAML default is FLAC→[7,6,5]).
- **Download queue is LIFO** (newest first), `MAX_CONCURRENT` (default 8) `threading` workers. SSE progress flows through a `queue.Queue`.
- **CSRF is on (flask-seasurf)** for all POST/PUT/DELETE. Bypass with `CSRF_DISABLE=1` env. In tests it's disabled by patching `flask_seasurf.SeaSurf._before_request` in conftest — setting `app.config["CSRF_DISABLE"]` after `create_app()` is too late.
- **Adding tests**: conftest `_patch_paths` monkeypatches module-level path constants (`app.download.controller.DOWNLOAD_DIR`, `app.qobuz.session.TOKEN_FILE`, …) — any new module reading paths from `app.config` needs the same treatment. It also resets `app.models._conn`/`_db_initialized` (else the first test's DB connection leaks into later tests) and disables background workers (`start_worker_pool`, `recover_interrupted_jobs`, `start_worker`).
- **Secrets**: `.env` is gitignored, host-only; compose passes `SECRET_KEY`/`QOBUZ_TOKEN` from it. Token file: `config/qobuz/qobuz_token.json` (host) → `/app/.config/qobuz/` (container). `entrypoint.sh` auto-restores the token from `/opt/backups/qoochie-auth` when missing.
- **Beets** is a pip package (`beets>=1.6`, provides the `beet` binary in the venv); config at `config/beets/config.yaml`, DB via `BEETSDIR`. `beet import` runs as a subprocess.
- **Navidrome is external** — not defined in compose.yaml. Expected at `http://navidrome:4533`; import_upload triggers `/api/scan?full=false`.
- **Compose quirks**: binds `10.8.0.10:19295:19290` (UI on host port 19295, app on 19290); `./app` is bind-mounted live; gunicorn `--timeout 120 --max-requests 2000`.

## Environment variables
| Variable | Required | Default | Notes |
|---|---|---|---|
| `SECRET_KEY` | prod | random per restart | Flask session secret |
| `QOBUZ_TOKEN` | yes (API) | — | bootstrapped into the token file at startup |
| `QOBUZ_USER_ID` / `QOBUZ_APP_ID` | no | 0 | parsed as int |
| `QOBUZ_APP_SECRET` | no | — | request signing |
| `FLASK_PORT` | no | 19290 | debug server + gunicorn bind |
| `QOBUZ_QUALITY` | no | FLAC (via YAML) | see QUALITY_FORMAT_MAP |
| `CSRF_DISABLE` | no | — | `1`/`true` disables SeaSurf |
