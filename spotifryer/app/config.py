"""Configuration — paths, env, and version."""

import logging
import os
from pathlib import Path

import yaml

_GIT_COMMIT = os.environ.get("GIT_COMMIT", "")
_BUILD_TIME = os.environ.get("BUILD_TIME", "")
__version__ = f"0.1.0+{_GIT_COMMIT}" if _GIT_COMMIT else "0.1.0"

# Container detection: env var SPOTIFRYER_CONTAINER=1 overrides filesystem check.
# Without this, accidentally creating /app/config/ on a dev host switches all paths.
_IS_CONTAINER = (
    os.environ.get("SPOTIFRYER_CONTAINER", "").lower() in ("1", "true")
    or os.path.exists("/app/config/spotifryer.yaml")
)
_proj = Path(__file__).resolve().parent.parent

CONFIG_PATH = os.environ.get("CONFIG_PATH",
    "/app/config/spotifryer.yaml" if _IS_CONTAINER else str(_proj / "spotifryer.yaml"))


def _load_config() -> dict:
    p = Path(CONFIG_PATH)
    if p.exists():
        with open(p) as f:
            return yaml.safe_load(f) or {}
    return {}


_config = _load_config()


def _cfg(key: str, default, env_var: str | None = None):
    """Get config value: env override > yaml key > default."""
    if env_var and os.environ.get(env_var):
        return os.environ[env_var]
    parts = key.split(".")
    val = _config
    for p in parts:
        if isinstance(val, dict):
            val = val.get(p)
        else:
            return default
    return val if val is not None else default


def _get_path(key: str, env_var: str, default: str) -> Path:
    value = _cfg(key, default, env_var)
    path = Path(str(value))
    if str(path).startswith("~"):
        path = path.expanduser()
    return path


# ── Paths ───────────────────────────────────────────────────────
# When running on the host (not in container), always use local project dirs
# regardless of what spotifryer.yaml says (it has container paths).
_local = not _IS_CONTAINER

if _local:
    DOWNLOAD_DIR = _proj / "downloads"
    LIBRARY_DIR = _proj / "music"
    JOBS_DIR = _proj / "data"
    SPOTIFY_CONFIG_DIR = _proj / ".config" / "spotify"
    STREAMRIP_CONFIG_DIR = _proj / ".config" / "streamrip"
    BEETS_DIR = _proj / ".config" / "beets"
else:
    DOWNLOAD_DIR = _get_path("paths.download_dir", "DOWNLOAD_DIR", "/app/downloads")
    LIBRARY_DIR = _get_path("paths.library_dir", "LIBRARY_DIR", "/music")
    JOBS_DIR = Path(str(_cfg("paths.jobs_dir", "/app/data", "JOBS_DIR")))
    SPOTIFY_CONFIG_DIR = _get_path("paths.spotify_config_dir", "SPOTIFY_CONFIG_DIR",
        "/app/.config/spotify")
    STREAMRIP_CONFIG_DIR = _get_path("paths.streamrip_config_dir", "STREAMRIP_CONFIG_DIR",
        "/app/.config/streamrip")
    BEETS_DIR = _get_path("paths.beets_dir", "BEETSDIR", "/app/.config/beets")

# ── Helpers ─────────────────────────────────────────────────────
def _as_bool(val) -> bool:
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return bool(val)
    if isinstance(val, str):
        return val.strip().lower() not in ("0", "false", "no", "off", "")
    return bool(val)


# ── Server ──────────────────────────────────────────────────────
FLASK_PORT = int(_cfg("server.port", 19291, "FLASK_PORT"))
GUNICORN_WORKERS = int(_cfg("server.gunicorn_workers", 1, "GUNICORN_WORKERS"))
GUNICORN_THREADS = int(_cfg("server.gunicorn_threads", 8, "GUNICORN_THREADS"))

# ── Spotify ─────────────────────────────────────────────────────
SPOTIFY_CLIENT_ID = str(_cfg("spotify.client_id", "", "SPOTIFY_CLIENT_ID"))
SPOTIFY_CLIENT_SECRET = str(_cfg("spotify.client_secret", "", "SPOTIFY_CLIENT_SECRET"))
SPOTIFY_REDIRECT_URI = str(_cfg("spotify.redirect_uri", "http://localhost:19291/spotify/auth/callback", "SPOTIFY_REDIRECT_URI"))

# Rate-limit guardrails: a global minimum interval between Spotify API calls
# (shared by all worker threads) and a TTL for the read-path result cache, so
# discography/search bursts can't trip the 429 Retry-After ban hammer.
SPOTIFY_MIN_REQUEST_INTERVAL = float(_cfg("spotify.min_request_interval", 0.5, "SPOTIFY_MIN_REQUEST_INTERVAL"))
SPOTIFY_RESOLVER_CACHE_TTL = int(_cfg("spotify.resolver_cache_ttl", 600, "SPOTIFY_RESOLVER_CACHE_TTL"))

# ── Sources ─────────────────────────────────────────────────────
SOURCES_PRIORITY: list[str] = _cfg("sources.priority", ["streamrip", "ytdlp"], None)
if not isinstance(SOURCES_PRIORITY, list):
    SOURCES_PRIORITY = ["streamrip", "ytdlp"]

STREAMRIP_ENABLED = _as_bool(_cfg("sources.streamrip.enabled", True, "STREAMRIP_ENABLED"))
STREAMRIP_BINARY = str(_cfg("sources.streamrip.binary", "rip", "STREAMRIP_BINARY"))
STREAMRIP_TIMEOUT = int(_cfg("sources.streamrip.timeout", 300, "STREAMRIP_TIMEOUT"))
STREAMRIP_QUALITY = str(_cfg("sources.streamrip.quality", "lossless", "STREAMRIP_QUALITY"))

YTDLP_ENABLED = _as_bool(_cfg("sources.ytdlp.enabled", True, "YTDLP_ENABLED"))
YTDLP_FORMAT = str(_cfg("sources.ytdlp.format", "m4a/bestaudio/best", "YTDLP_FORMAT"))
YTDLP_RATELIMIT = int(_cfg("sources.ytdlp.ratelimit", 1000000, "YTDLP_RATELIMIT"))
YTDLP_SLEEP_INTERVAL = int(_cfg("sources.ytdlp.sleep_interval_requests", 3, "YTDLP_SLEEP_INTERVAL"))
YTDLP_MAX_CONCURRENT = int(_cfg("sources.ytdlp.max_concurrent", 2, "YTDLP_MAX_CONCURRENT"))

# ── Downloads ───────────────────────────────────────────────────
MAX_CONCURRENT = int(_cfg("downloads.max_concurrent", 8, "MAX_CONCURRENT"))
PROMOTE_TO_LIBRARY = _as_bool(_cfg("downloads.promote_to_library", True, "PROMOTE_TO_LIBRARY"))

PROMOTE_EXISTS = str(_cfg("downloads.promote_exists", "skip", "PROMOTE_EXISTS"))
if PROMOTE_EXISTS not in ("overwrite", "skip"):
    _log = logging.getLogger(__name__)
    _log.warning("Invalid promote_exists=%r, falling back to 'skip'. Use 'overwrite' or 'skip'.", PROMOTE_EXISTS)
    PROMOTE_EXISTS = "skip"

ORGANIZE_WITH_BEETS = _as_bool(_cfg("downloads.organize_with_beets", True, "ORGANIZE_WITH_BEETS"))
DEFAULT_OVERRIDE_EXISTING = _as_bool(_cfg("downloads.override_existing", False, "DEFAULT_OVERRIDE_EXISTING"))
FILENAME_TEMPLATE = str(_cfg("downloads.filename_template", "{artist} - {title}", "FILENAME_TEMPLATE"))
MAX_QUEUE_SIZE = int(_cfg("downloads.max_queue_size", 500, "MAX_QUEUE_SIZE"))

# ── Network ─────────────────────────────────────────────────────
_proxy_list_raw = _cfg("network.proxy_list", [], "PROXY_LIST")
PROXY_LIST: list[str] = _proxy_list_raw if isinstance(_proxy_list_raw, list) else [str(_proxy_list_raw)]
if not PROXY_LIST:
    PROXY_LIST = [""]

PROXY_LABELS: list[str] = [f"Proxy {i}" if i > 0 else "None" for i in range(len(PROXY_LIST))]

# ── Navidrome ─────────────────────────────────────────────────
NAVIDROME_URL = str(_cfg("navidrome.url", "http://navidrome:4533", "NAVIDROME_URL"))
NAVIDROME_AUTO_SCAN = _as_bool(_cfg("navidrome.auto_scan", True, "NAVIDROME_AUTO_SCAN"))

# ── Import / Upload ────────────────────────────────────────────
IMPORT_MAX_UPLOAD_MB = int(_cfg("import_upload.max_upload_mb", 512, "IMPORT_MAX_UPLOAD_MB"))
IMPORT_ALLOWED_EXTS: list[str] = _cfg("import_upload.allowed_exts",
    [".flac", ".wav", ".mp3", ".m4a", ".opus"], "IMPORT_ALLOWED_EXTS")
IMPORT_ARCHIVE_EXTS: list[str] = _cfg("import_upload.archive_exts",
    [".zip", ".tar", ".tar.gz"], "IMPORT_ARCHIVE_EXTS")
IMPORT_CLEANUP_ARCHIVE = _as_bool(_cfg("import_upload.cleanup_archive", False, "IMPORT_CLEANUP_ARCHIVE"))

if _local:
    IMPORT_STAGING_DIR = _proj / "data" / "import_staging"
    PLAYLIST_DIR = _proj / "playlists"
else:
    IMPORT_STAGING_DIR = _get_path("import_upload.staging_dir", "IMPORT_STAGING_DIR", "/app/data/import_staging")
    PLAYLIST_DIR = _get_path("paths.playlist_dir", "PLAYLIST_DIR", "/app/playlists")
