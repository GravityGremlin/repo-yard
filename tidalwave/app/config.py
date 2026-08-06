"""Configuration — paths, env, and version."""

import logging
import os
from pathlib import Path

import yaml

_GIT_COMMIT = os.environ.get("GIT_COMMIT", "")
_BUILD_TIME = os.environ.get("BUILD_TIME", "")
__version__ = f"0.3.11+{_GIT_COMMIT}" if _GIT_COMMIT else "0.3.11"

_IS_CONTAINER = os.path.exists("/app/config/tidalwave.yaml")
_proj = Path(__file__).resolve().parent.parent

CONFIG_PATH = os.environ.get("CONFIG_PATH",
    "/app/config/tidalwave.yaml" if _IS_CONTAINER else str(_proj / "tidalwave.yaml"))


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
# regardless of what tidalwave.yaml says (it has container paths).
_local = not _IS_CONTAINER

if _local:
    DOWNLOAD_DIR = _proj / "downloads"
    LIBRARY_DIR = _proj / "music"
    JOBS_DIR = _proj / "data"
    PLAYLIST_DIR = _proj / "playlists"
else:
    DOWNLOAD_DIR = _get_path("paths.download_dir", "DOWNLOAD_DIR", "/opt/data/tidalwave")
    LIBRARY_DIR = _get_path("paths.library_dir", "LIBRARY_DIR", "/music")
    JOBS_DIR = Path(str(_cfg("paths.jobs_dir", "/app/data", "JOBS_DIR")))
    PLAYLIST_DIR = _get_path("paths.playlist_dir", "PLAYLIST_DIR",
        "/opt/data/tidalwave/playlists")

TIDAL_CONFIG_DIR = _get_path("paths.tidal_config_dir", "TIDAL_CONFIG_DIR",
    "~/.config/tidal_dl_ng")

# ── Server ──────────────────────────────────────────────────────
FLASK_PORT = int(_cfg("server.port", 19290, "FLASK_PORT"))
GUNICORN_WORKERS = int(_cfg("server.gunicorn_workers", 1, "GUNICORN_WORKERS"))
GUNICORN_THREADS = int(_cfg("server.gunicorn_threads", 8, "GUNICORN_THREADS"))

# ── Tidal ───────────────────────────────────────────────────────
TIDAL_QUALITY = str(_cfg("tidal.quality", "HIGH", "TIDAL_QUALITY"))
MAX_CONCURRENT = int(_cfg("tidal.max_concurrent", 8, "MAX_CONCURRENT"))

# ── Downloads ───────────────────────────────────────────────────
def _as_bool(val) -> bool:
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return bool(val)
    if isinstance(val, str):
        return val.strip().lower() not in ("0", "false", "no", "off", "")
    return bool(val)


PROMOTE_TO_LIBRARY = _as_bool(_cfg("downloads.promote_to_library", True, "PROMOTE_TO_LIBRARY"))

PROMOTE_EXISTS = str(_cfg("downloads.promote_exists", "overwrite", "PROMOTE_EXISTS"))
if PROMOTE_EXISTS not in ("overwrite", "skip"):
    _log = logging.getLogger(__name__)
    _log.warning("Invalid promote_exists=%r, falling back to 'overwrite'. Use 'overwrite' or 'skip'.", PROMOTE_EXISTS)
    PROMOTE_EXISTS = "overwrite"

BEETS_DIR = _get_path("paths.beets_dir", "BEETSDIR", "/app/.config/beets")
ORGANIZE_WITH_BEETS = _as_bool(_cfg("downloads.organize_with_beets", True, "ORGANIZE_WITH_BEETS"))
DEFAULT_OVERRIDE_EXISTING = _as_bool(_cfg("downloads.override_existing", False, "DEFAULT_OVERRIDE_EXISTING"))

# ── Network ─────────────────────────────────────────────────────
_proxy_list_raw = _cfg("network.proxy_list", [], "PROXY_LIST")
PROXY_LIST: list[str] = _proxy_list_raw if isinstance(_proxy_list_raw, list) else [str(_proxy_list_raw)]
if not PROXY_LIST:
    PROXY_LIST = [""]

PROXY_LABELS: list[str] = [f"Proxy {i}" if i > 0 else "None" for i in range(len(PROXY_LIST))]

# ── Import Upload ───────────────────────────────────────────────
_IMPORT_DEFAULT_EXTS = [".flac", ".wav", ".mp3", ".m4a", ".aac", ".ogg", ".dsf", ".aiff", ".wma"]
IMPORT_STAGING_DIR = _get_path(
    "import_upload.staging_dir", "IMPORT_STAGING_DIR",
    "/opt/data/tidalwave/import_staging" if _IS_CONTAINER else str(_proj / "import_staging"),
)
IMPORT_MAX_UPLOAD_MB = int(
    _cfg("import_upload.max_upload_mb", 512, "IMPORT_MAX_UPLOAD_MB")
)
_import_exts_raw = _cfg("import_upload.allowed_extensions", _IMPORT_DEFAULT_EXTS, "IMPORT_ALLOWED_EXTS")
IMPORT_ALLOWED_EXTS: list[str] = _import_exts_raw if isinstance(_import_exts_raw, list) else [
    s.strip() for s in str(_import_exts_raw).split()
]
_import_archives_raw = _cfg(
    "import_upload.accepted_archives", [".zip", ".tar", ".tar.gz"], "IMPORT_ARCHIVE_EXTS"
)
IMPORT_ARCHIVE_EXTS: list[str] = (
    _import_archives_raw if isinstance(_import_archives_raw, list)
    else [s.strip() for s in str(_import_archives_raw).split()]
)

# ── Navidrome ─────────────────────────────────────────────────
NAVIDROME_URL = str(_cfg("navidrome.url", "http://navidrome:4533", "NAVIDROME_URL"))
NAVIDROME_AUTO_SCAN = _as_bool(_cfg("navidrome.auto_scan", True, "NAVIDROME_AUTO_SCAN"))
