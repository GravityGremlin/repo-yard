"""Collection catalog — browse artists and albums in the music library."""

from __future__ import annotations

import logging
import threading
import time
from urllib.parse import quote

from flask import Blueprint, render_template, request

from app.config import LIBRARY_DIR

logger = logging.getLogger(__name__)

collection_bp = Blueprint("collection", __name__, url_prefix="/collection")

# ─── Catalog scan (parallel to scan_cache — does NOT touch it) ─────────────
_CATALOG_TTL = 300  # 5 minutes
_catalog_cache: dict | None = None
_catalog_mtime: float = 0.0
_catalog_lock = threading.Lock()

_AUDIO_EXTS = {".flac", ".m4a", ".mp3", ".wav", ".ogg", ".opus", ".aac"}
_COVER_NAMES = ("cover.jpg", "cover.jpeg", "cover.png", "folder.jpg", "folder.png")
_CATALOG_LIMIT = 20  # artists per page


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
    """Background refresh of the catalog cache."""
    global _catalog_cache, _catalog_mtime
    try:
        data = _build_catalog()
        with _catalog_lock:
            _catalog_cache = data
            _catalog_mtime = time.time()
    except Exception:
        logger.warning("Catalog refresh failed", exc_info=True)


def _get_catalog() -> dict | None:
    """Return cached catalog; kick a background refresh if stale or absent."""
    now = time.time()
    with _catalog_lock:
        stale = (now - _catalog_mtime) > _CATALOG_TTL
        cache = _catalog_cache
    if stale and not _catalog_lock.locked():
        threading.Thread(target=_refresh_catalog, daemon=True).start()
        logger.debug("Background catalog scan started")
    return cache


@collection_bp.route("/")
def index():
    """Render full collection page with catalog grid."""
    catalog = _get_catalog()
    if catalog is None:
        catalog = {
            "artists": [], "scanning": True, "exists": True,
            "path": str(LIBRARY_DIR),
        }

    page = max(1, request.args.get("page", default=1, type=int))
    artists = catalog.get("artists", [])
    total_artists = len(artists)
    total_pages = max(1, (total_artists + _CATALOG_LIMIT - 1) // _CATALOG_LIMIT)
    page = min(page, total_pages)
    offset = (page - 1) * _CATALOG_LIMIT
    page_artists = artists[offset:offset + _CATALOG_LIMIT]

    return render_template(
        "collection.html",
        artists=page_artists,
        page=page,
        total_pages=total_pages,
        total_artists=total_artists,
        scanning=catalog.get("scanning", False),
        exists=catalog.get("exists", True),
    )


@collection_bp.route("/catalog")
def catalog():
    """Artist→album catalog as an HTMX partial. Paginated with ?page= param."""
    catalog_data = _get_catalog()
    if catalog_data is None:
        catalog_data = {
            "artists": [], "scanning": True, "exists": True,
            "path": str(LIBRARY_DIR),
        }

    page = max(1, request.args.get("page", default=1, type=int))
    artists = catalog_data.get("artists", [])
    total_artists = len(artists)
    total_pages = max(1, (total_artists + _CATALOG_LIMIT - 1) // _CATALOG_LIMIT)
    page = min(page, total_pages)
    offset = (page - 1) * _CATALOG_LIMIT
    page_artists = artists[offset:offset + _CATALOG_LIMIT]

    return render_template(
        "collection_catalog.html",
        artists=page_artists,
        page=page,
        total_pages=total_pages,
        total_artists=total_artists,
        scanning=catalog_data.get("scanning", False),
        exists=catalog_data.get("exists", True),
    )
