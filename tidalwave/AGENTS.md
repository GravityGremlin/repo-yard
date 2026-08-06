# tidalwave — AGENTS.md

## Project
Flask web UI for native Tidal music downloads. Background worker pool downloads from Tidal API (no subprocess), beets organizes into a library, Navidrome scans and serves.

## Commands

```bash
# Dev server (Flask, port 19290)
.venv/bin/python app/main.py

# All tests
.venv/bin/python -m pytest tests/ -q

# Single test / single file
.venv/bin/python -m pytest tests/test_smoke.py
.venv/bin/python -m pytest tests/test_discography.py::TestResolveDiscography::test_returns_albums_from_artist -q

# Skip broken / slow tests
.venv/bin/python -m pytest tests/ --ignore=tests/test_search_filters.py --ignore=tests/test_import_upload.py -q
```

## Architecture

```
app/main.py → factory.create_app()
  └─ Flask app with blueprints: search, download, tidal, playlist, library, collection, audit, stats, import_upload
  └─ CSRF on all POST/PUT/DELETE (flask-seasurf)
  └─ Background download worker pool (daemon threads, MAX_CONCURRENT)
  └─ SQLite job store (data/jobs.db, WAL mode, threading.Lock)
  └─ Jinja2 templates in app/templates/, PWA static assets in app/static/
```

**Entrypoint**: `app/main.py` (dev), `entrypoint.sh` → gunicorn `app.main:app` (prod, 1 worker × 8 threads).

**Config**: `tidalwave.yaml` (root), env vars override. Container detection via `os.path.exists("/app/config/tidalwave.yaml")` — on host, local project dirs are used regardless of YAML.

## Key subsystems

| Directory | Purpose |
|---|---|
| `app/tidal/` | Tidal OAuth session (device login, token persistence, auto-refresh), native downloader |
| `app/download/` | Worker pool, job control (queue/cancel/subscribe), beets integration, discography resolver |
| `app/import_upload/` | File upload + background import (archive extraction, beets import) |
| `app/search/` | Tidal search with library-badge overlay |
| `app/library/` | Local library browsing via filesystem walk + scan cache |

## Test conventions

- **conftest.py is critical**: `app_client` fixture monkeypatches all config paths into `tmp_path`, disables worker pool (`start_worker_pool` → no-op), disables CSRF. Every test MUST use `app_client` — not a raw Flask client.
- **fake_tidalapi.py**: Stubs for `tidalapi` types. Do NOT import real `tidalapi` in tests.
- **Workaround injections** in conftest: `Path` into `app.library.routes`, `_cover_url` into `app.search.routes`. These paper over missing imports in production code.
- **Beets tests** mock `subprocess.run` — no real beets CLI is called.
- **Broken test**: `tests/test_search_filters.py` imports `_in_year_range` from `app.search.routes` which was deleted. Skip with `--ignore` until fixed.
- **Slow test**: `tests/test_import_upload.py` — runs import jobs with real file I/O. Skip for fast iteration.

## Gotchas

- **CSRF**: All POST/PUT/DELETE routes require CSRF token (flask-seasurf). HTML forms must include `{{ csrf_token() }}`. Tests disable it via monkeypatch in conftest.
- **Path config duality**: `_IS_CONTAINER` flag means path config behaves differently on host vs container. On host, `LIBRARY_DIR` is always `./music`, ignoring tidalwave.yaml. On container, it reads YAML.
- **Token lifecycle**: Tidal OAuth tokens stored in `~/.config/tidal_dl_ng/token.json` (config/tidal/). `entrypoint.sh` auto-restores from `/opt/backups/tidalwave-auth/token.json.latest` on startup. Tests don't need tokens (fake session fixture).
- **Beets NFS lag**: `beets_integration.py` polls for staged files to appear before invoking `beet import` — compensates for bind-mount/NFS latency.
- **Download worker pool**: Daemon threads, LIFO queue (newest first). Queue persisted to `data/queue_order.json`. Pool auto-recovers interrupted jobs on `create_app()`.
- **No CI**: There are no GitHub Actions or other CI workflows.
- **Secret key**: `flask.secret_key` from `SECRET_KEY` env var, falls back to `os.urandom(32).hex()` — never reuse sessions across restarts without setting `SECRET_KEY`.
