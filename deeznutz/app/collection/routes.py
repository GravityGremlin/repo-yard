"""Collection overview — scan the music library and report summary stats.

Full Deezer-vs-library ISRC/UPC matching remains on the roadmap; this view
gives a live, bounded snapshot of what is actually on disk.
"""

from __future__ import annotations

import logging
import threading
import time
from urllib.parse import quote

from flask import Blueprint, render_template, request

from app.config import LIBRARY_DIR
from app.library.scan_cache import get_cached, refresh_now

logger = logging.getLogger(__name__)

bp = Blueprint("collection", __name__, url_prefix="/collection")


# ─── Catalog scan (parallel to scan_cache — does NOT touch it) ─────────────
# Catalog cache holds the full artist→album tree; the route paginates from it.
# Keeps the same TTL discipline as scan_cache so the heavy walk stays off the
# request thread but lives in its own module-level state so scan_cache is
# untouched per the task constraint.
_CATALOG_TTL = 300  # 5 minutes — mirrors scan_cache._SCAN_TTL
_catalog_cache: dict | None = None
_catalog_mtime: float = 0.0
_catalog_running = threading.Event()

_AUDIO_EXTS = {".flac", ".m4a", ".mp3", ".wav", ".ogg", ".opus", ".aac"}
_COVER_NAMES = ("cover.jpg", "cover.jpeg", "cover.png", "folder.jpg", "folder.png")
_CATALOG_LIMIT = 50  # initial artists per page


def _build_catalog() -> dict:
    """Walk LIBRARY_DIR once, building the artist→album→(cover, tracks) tree.

    Library structure: ``<Artist>/<Album>/<tracks + cover.jpg>``.
    Cover URL is the served path so the browser streams via /library/serve.
    """
    root = LIBRARY_DIR
    if not root.exists():
        return {"artists": [], "scanning": False, "exists": False, "path": str(root)}

    artists: list[dict] = []
    try:
        for artist_dir in sorted(root.iterdir(), key=lambda e: e.name.lower()):
            if not artist_dir.is_dir() or artist_dir.name.startswith("."):
                continue
            albums: list[dict] = []
            try:
                children = sorted(artist_dir.iterdir(), key=lambda e: e.name.lower())
            except OSError:
                continue
            for album_dir in children:
                if not album_dir.is_dir() or album_dir.name.startswith("."):
                    continue
                try:
                    entries = list(album_dir.iterdir())
                except OSError:
                    continue
                track_count = sum(
                    1 for e in entries
                    if e.is_file() and e.suffix.lower() in _AUDIO_EXTS
                )
                cover_name = next(
                    (e.name for e in entries
                     if e.is_file() and e.name.lower() in _COVER_NAMES),
                    None,
                )
                # URL-encode each path segment so spaces / parens survive the
                # round-trip through <path:subpath> and safe_resolve().
                segs = "/".join(
                    quote(s) for s in (artist_dir.name, album_dir.name, cover_name or "")
                )
                albums.append({
                    "name": album_dir.name,
                    "cover_url": f"/library/serve/{segs}" if cover_name else "",
                    "track_count": track_count,
                })
            if albums:
                artists.append({"name": artist_dir.name, "albums": albums})
    except OSError as exc:
        logger.warning("Catalog walk failed: %s", exc)
    return {"artists": artists, "scanning": False, "exists": True, "path": str(root)}


def _refresh_catalog() -> None:
    """Background refresh of the catalog cache. Runs off the request thread."""
    global _catalog_cache, _catalog_mtime
    try:
        data = _build_catalog()
        _catalog_cache = data
        _catalog_mtime = time.time()
    except Exception:  # pragma: no cover — defensive; logger keeps the stack
        logger.warning("Catalog refresh failed", exc_info=True)


def _get_catalog() -> dict | None:
    """Return cached catalog; kick a background refresh if stale or absent."""
    now = time.time()
    stale = (now - _catalog_mtime) > _CATALOG_TTL
    if stale and not _catalog_running.is_set():
        _catalog_running.set()
        threading.Thread(target=lambda: (_refresh_catalog(), _catalog_running.clear()),
                         daemon=True).start()
        logger.debug("Background catalog scan started")
    return _catalog_cache


@bp.route("/")
def index():
    """Library summary statistics from cached scan."""
    stats = get_cached("collection")
    if stats is None:
        stats = {
            "path": str(LIBRARY_DIR),
            "exists": LIBRARY_DIR.exists(),
            "artists": 0, "albums": 0, "tracks": 0,
            "total_bytes": 0, "scanned": 0, "capped": False, "by_ext": {},
            "scanning": True,
        }
    else:
        # Copy, don't mutate the shared cached dict (scan_cache returns the
        # live object; concurrent requests would race on this write).
        stats = {**stats, "scanning": False}
    return render_template("collection.html", stats=stats)


@bp.route("/catalog")
def catalog():
    """Artist→album catalog as an HTMX partial. Paginates 50 artists per call.

    Returns a scanning placeholder on the very first call (cache cold) so the
    browser shows progress while the background walk runs.
    """
    offset = max(0, request.args.get("offset", default=0, type=int))
    data = _get_catalog()
    if data is None:
        return render_template("partials/catalog.html", artists=[], offset=offset,
                               limit=_CATALOG_LIMIT, total=0, scanning=True, exists=True)
    full = data["artists"]
    window = full[offset:offset + _CATALOG_LIMIT]
    return render_template("partials/catalog.html", artists=window, offset=offset,
                           limit=_CATALOG_LIMIT, total=len(full), scanning=False,
                           exists=data["exists"])


@bp.route("/refresh", methods=["POST"])
def refresh():
    """Force a background cache refresh."""
    threading.Thread(
        target=lambda: (refresh_now("collection"), refresh_now("recent")),
        daemon=True,
    ).start()
    return {"refreshing": True}
