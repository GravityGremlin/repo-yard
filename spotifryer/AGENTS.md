# spotifryer

Spotify track/playlist downloader — Flask 3 + HTMX, streamrip (Tidal/Qobuz) + yt-dlp (YouTube Music).

## Commands

- Dev server: `python app/main.py` — runs Flask on port 19291
- Docker build: `docker compose build`
- Docker run: `docker compose up -d`
- No test, lint, or typecheck commands exist (`tests/` directory absent)

## Config

Config resolution: env var > YAML key > default. Yaml is at `spotifryer.yaml` (container: `/app/config/spotifryer.yaml`).

**Local vs container path switching** (`app/config.py:55-72`): when running on the host (`/app/config/spotifryer.yaml` doesn't exist), all paths use local project dirs (`./downloads`, `./music`, `./data`, `./.config/*`) regardless of yaml content. Container uses yaml paths. This is easy to miss if you override a path in the yaml and run locally.

Required env vars for Spotify: `SECRET_KEY`, `SPOTIFY_CLIENT_ID`, `SPOTIFY_CLIENT_SECRET` (see `.env.example`).

## Architecture

### Stack
| Layer | Detail |
|---|---|
| Web | Flask 3 + HTMX (htmx.org 1.9.10), Jinja2 templates |
| Auth | Spotify OAuth PKCE via spotipy, token persisted to `SPOTIFY_CONFIG_DIR/spotify_token.json` |
| DB | SQLite (WAL mode), single `jobs` table with JSON column for serialized dataclass, `audit_log` table |
| Worker | In-process thread pool (no Celery/RQ) — started in `factory.py:114-117` |
| Download | Provider chain: streamrip → ytdlp (configurable via `sources.priority`) |
| Metadata | mutagen (tags, cover art), Pillow |
| Proxy | Module in `app/proxy.py`, proxy list from config |
| Server | Gunicorn (in container), dev Flask |

### Blueprints (`url_prefix`)
- `/` (search_bp) — search page
- `/download` (download_bp) — enqueue, job list, per-job SSE progress, cancel
- `/library/` (library_bp) — browse/serve local library files
- `/collection` (collection_bp) — artist/album catalog grid
- `/spotify` (spotify_bp) — OAuth auth flow
- `/search` — search API/HTML partials
- `/playlist` — resolve playlist URL, download-all
- `/import/` (import_upload_bp) — file upload + import jobs
- `/stats/` (stats_bp) — dashboard stats
- `/audit/` (audit_bp) — audit log

### App entrypoints
- `app.main:app` — Flask app object (imported by gunicorn and `python app/main.py`)
- `app.factory.create_app()` — app factory, registers blueprints, starts workers, recovers jobs
- `app.config` — all config constants loaded at import time

### Download flow
1. User submits Spotify URL → `controller.enqueue_download()` resolves URL, creates `Job` dataclass per item
2. Worker pool picks up queued jobs, runs provider chain (streamrip → ytdlp fallback)
3. Downloaded file gets metadata embedded (mutagen), then promoted to library dir
4. SSE per-job progress: `GET /download/<job_id>/progress` (not global — there is no `/download/jobs/sse`)
5. HTMX polls `/download/jobs/html` every 5s for the job list view

## Database

`sqlite3` with `threading.Lock` for writes. Path: `JOBS_DIR / "jobs.db"` (local: `./data/jobs.db`). Schema:

```sql
CREATE TABLE jobs (
    id TEXT PRIMARY KEY,
    data TEXT NOT NULL,       -- JSON-serialized Job dataclass
    created_at TEXT,
    updated_at TEXT
);
CREATE TABLE audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event TEXT NOT NULL,
    job_id TEXT,
    data TEXT,
    created_at TEXT
);
```

Job model: `app/models.py:93` — `@dataclass Job` with fields like id, url, title, artist, kind, status, progress, provider, data.

## Startup behavior

On startup (`factory.py`):
1. `recover_interrupted_jobs()` — re-queues jobs that were running when the process exited
2. `start_worker_pool()` — starts download worker threads
3. `start_scan_cache()` — background library scan cache
4. `recover_import_jobs()` + `start_import_worker()` — for import/upload jobs

Container entrypoint (`entrypoint.sh`): restores streamrip credentials and Spotify token from `/opt/backups/spotifryer-config/` if config dirs are empty.

## Non-obvious

- **No CSRF exemption for HTMX**: `base.html` injects CSRF token via meta tag and adds `X-CSRFToken` header to all HTMX requests. Don't skip CSRF on HTMX routes.
- **`_require_auth()`**: many routes return 401 JSON if Spotify is not authenticated. The UI shows "○ Spotify" badge when disconnected.
- **`proxy.py`** manages a proxy list (`network.proxy_list`), index 0 = no proxy. Used per-download with `proxy_index` on the Job.
- **Collection catalog** (`app/collection/routes.py`) has a 5-minute TTL independent of `library.scan_cache`. Two separate scans of the music directory.
- **Filename template**: `{artist} - {title}` in config, not the default file naming from streamrip.
- **Single-file CSS**: `app/static/css/app.css` (~800 lines) — all styling in one file, no CSS framework.
- **No tests exist** — `tests/` directory is absent. No CI pipeline visible.
- **HTMX version pinned** to 1.9.10 via CDN in `base.html`.

## Library Version Gotchas

These are behavioral changes in dependencies that are easy to rediscover the hard way.

### spotipy v3

- **`sp.playlist()`** returns `items` (not `tracks`) as the key for the track paging object. Use `playlist.get("items", {}).get("total")` not `playlist.get("tracks", {}).get("total")`.
- **`sp.playlist_items()`** items use `item` key (not `track`) for the track object. Use `entry.get("item")` not `entry.get("track")`.
- **`fields` parameter on `playlist_items()`** returns empty item objects — omit it entirely and process the full response.
- **Artist search results**: `genres` and `followers` can be `None` (not `[]`/`{}`). Guard with `(item.get("genres") or [])` and `(item.get("followers") or {})`.

### streamrip CLI

- **`rip search --json`** was removed. Use `-o <tmpfile>` to write results to a temp file, then read and parse the JSON.
- **Qobuz searches** require login credentials. Ensure streamrip config is restored from `/opt/backups/spotifryer-config/` (handled by `entrypoint.sh`).

## Adding Routes / Templates

When adding or changing a route that renders a template:

1. Verify the route passes **every variable** the template (and its partials) reference.
2. Confirm every **Jinja2 filter** used in the template is registered in `factory.py` (`@app.template_filter`).
3. Test new template partials via **both** direct render (e.g. HTMX partial endpoint) **and** parent-page include (full page load), since `{% include %}` inherits the parent context.
4. After deploying, curl the route to confirm it returns 200, not 500. The `error.html` template exists but doesn't show the root cause — check container logs with `docker logs spotifryer`.
