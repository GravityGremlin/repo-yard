"""Search routes — search Tidal for albums, tracks, and artists."""

from __future__ import annotations

import logging
import time

from flask import Blueprint, jsonify, render_template, request

from app.tidal.session import get_session
from app.config import LIBRARY_DIR

logger = logging.getLogger(__name__)

# ── Library badge cache (avoids walking LIBRARY_DIR on every search) ──
_badge_cache: dict = {"timestamp": 0, "artists": set(), "albums": set()}
_BADGE_CACHE_TTL = 60  # seconds


def _get_library_badge_data():
    """Return (artist_names, album_tuples) from LIBRARY_DIR, cached for 60s."""
    now = time.time()
    if now - _badge_cache["timestamp"] < _BADGE_CACHE_TTL:
        return _badge_cache["artists"], _badge_cache["albums"]
    artists: set[str] = set()
    albums: set[tuple[str, str]] = set()
    if LIBRARY_DIR.is_dir():
        for artist_dir in LIBRARY_DIR.iterdir():
            if artist_dir.is_dir():
                artists.add(artist_dir.name)
                for album_dir in artist_dir.iterdir():
                    if album_dir.is_dir():
                        albums.add((artist_dir.name, album_dir.name))
    _badge_cache["timestamp"] = now
    _badge_cache["artists"] = artists
    _badge_cache["albums"] = albums
    return artists, albums

bp = Blueprint("search", __name__)


@bp.route("/")
def index():
    """Main search page."""
    return render_template("index.html",
        query=request.args.get("q", ""),
        type=request.args.get("type", "all"),
    )


@bp.route("/search")
def search():
    """Search Tidal and return results as HTML partial (for HTMX)."""
    query = request.args.get("q", "").strip()
    kind = request.args.get("type", "all")  # all | album | track | artist

    if not query:
        return render_template("partials/search_results.html", results=None, query="")

    session = get_session()
    if not session:
        return render_template("partials/error.html",
                               message="Not connected to Tidal. <a href='/tidal/auth'>Connect here</a>."), 503

    try:
        # tidalapi search returns (tracks, artists, albums, playlists)
        raw = session.search(query)
    except Exception as exc:
        logger.error("Tidal search failed: %s", exc)
        return render_template("partials/error.html", message=f"Search failed: {exc}"), 500

    results = _parse_search_results(raw, kind)


    return render_template("partials/search_results.html", results=results, query=query)


@bp.route("/search/album/<album_id>/tracks")
def album_tracks(album_id: str):
    """Get tracks for a specific album (HTMX partial for expand)."""
    session = get_session()
    if not session:
        return render_template("partials/error.html",
                               message="Not connected to Tidal."), 503

    try:
        album = session.album(int(album_id))
        tracks = album.tracks()
    except Exception as exc:
        logger.error("Failed to get album tracks: %s", exc)
        return render_template("partials/error.html", message=str(exc)), 500

    return render_template("partials/album_tracks.html", album=album, tracks=tracks)


def _cover_url(obj):
    """Get cover image URL from a tidalapi Album object."""
    if not hasattr(obj, "image"):
        return ""
    for size in (320, 300, 480):
        try:
            url = obj.image(size)
            if url:
                return url
        except Exception:
            continue
    return ""


def _parse_search_results(raw, kind: str) -> dict:
    """Parse tidalapi search results into a serializable dict.

    session.search() returns a dict with keys: artists, albums, tracks,
    videos, playlists, top_hit. We access by key, not attribute.
    """
    results = {"albums": [], "tracks": [], "artists": [], "playlists": []}

    if not isinstance(raw, dict):
        return results

    artist_names, album_tuples = _get_library_badge_data()
    _sanitize = str.maketrans({
        "/": "-", "\\": "-", ":": "-",
        "?": "-", "*": "-", "\"": "-",
        "<": "-", ">": "-", "|": "-",
    })

    albums = raw.get("albums")
    if albums and kind in ("all", "album"):
        for a in albums:
            artist_name = a.artist.name if a.artist else "Unknown"
            album_title = a.name
            in_library = (artist_name.translate(_sanitize), album_title.translate(_sanitize)) in album_tuples
            results["albums"].append({
                "id": a.id,
                "title": album_title,
                "artist": artist_name,
                "year": getattr(a, "year", None),
                "num_tracks": getattr(a, "num_tracks", 0),
                "cover": _cover_url(a),                "in_library": in_library,
            })

    tracks = raw.get("tracks")
    if tracks and kind in ("all", "track"):
        for t in tracks:
            results["tracks"].append({
                "id": t.id,
                "title": t.name,
                "artist": t.artist.name if t.artist else "Unknown",
                "album": t.album.name if t.album else "",
                "duration": getattr(t, "duration", 0),
                "isrc": getattr(t, "isrc", ""),
            })

    artists = raw.get("artists")
    if artists and kind in ("all", "artist"):
        for a in artists:
            artist_name = a.name
            in_library = artist_name.translate(_sanitize) in artist_names
            results["artists"].append({
                "id": a.id,
                "name": artist_name,
                "in_library": in_library,
            })

    playlists = raw.get("playlists")
    if playlists and kind in ("all",):
        for p in playlists:
            results["playlists"].append({
                "id": getattr(p, "id", ""),
                "title": p.name if hasattr(p, "name") else str(getattr(p, "title", "Unknown")),
                "num_tracks": getattr(p, "num_tracks", 0),
            })

    return results


def _map_result(obj, item_type: str) -> dict:
    """Map a tidalapi result object to the canonical JSON shape."""
    if item_type == "track":
        return {
            "type": "track",
            "id": str(obj.id),
            "title": getattr(obj, "title", "") or getattr(obj, "name", ""),
            "artist": obj.artist.name if getattr(obj, "artist", None) else None,
            "album": obj.album.name if getattr(obj, "album", None) else None,
            "cover_url": _cover_url(obj.album) if getattr(obj, "album", None) else None,
            "duration_ms": (getattr(obj, "duration", 0) or 0) * 1000,
            "isrc": getattr(obj, "isrc", None) or None,
            "url": getattr(obj, "url", "") or "",
            "year": getattr(obj.album, "year", None) if getattr(obj, "album", None) else None,
        }
    if item_type == "album":
        return {
            "type": "album",
            "id": str(obj.id),
            "title": getattr(obj, "title", "") or getattr(obj, "name", ""),
            "artist": obj.artist.name if getattr(obj, "artist", None) else None,
            "album": getattr(obj, "title", "") or getattr(obj, "name", ""),
            "cover_url": _cover_url(obj) or None,
            "duration_ms": (getattr(obj, "duration", 0) or 0) * 1000,
            "isrc": getattr(obj, "isrc", None) or None,
            "url": getattr(obj, "share_url", "") or "",
            "year": getattr(obj, "year", None),
        }
    if item_type == "artist":
        return {
            "type": "artist",
            "id": str(obj.id),
            "title": getattr(obj, "name", ""),
            "artist": getattr(obj, "name", ""),
            "album": None,
            "cover_url": None,
            "duration_ms": 0,
            "isrc": None,
            "url": getattr(obj, "share_url", "") or "",
            "year": None,
        }
    if item_type == "playlist":
        return {
            "type": "playlist",
            "id": str(getattr(obj, "id", "")),
            "title": getattr(obj, "title", "") or getattr(obj, "name", ""),
            "artist": None,
            "album": None,
            "cover_url": None,
            "duration_ms": (getattr(obj, "duration", 0) or 0) * 1000,
            "isrc": None,
            "url": getattr(obj, "share_url", "") or "",
            "year": None,
        }
    return {}


_TYPE_KEY_MAP = {"track": "tracks", "album": "albums", "artist": "artists", "playlist": "playlists"}


@bp.route("/search/json")
def search_json():
    """JSON search endpoint — returns structured results for API consumers."""
    query = request.args.get("q", "").strip()
    kind = request.args.get("type", "track")  # track | album | artist | playlist

    if not query:
        return jsonify({
            "provider": "tidalwave",
            "query": "",
            "error": None,
            "results": [],
        })

    session = get_session()
    if not session:
        return jsonify({
            "provider": "tidalwave",
            "query": query,
            "error": "auth_expired",
            "results": [],
        })

    try:
        raw = session.search(query)
    except Exception as exc:
        logger.error("Tidal search failed (json): %s", exc, exc_info=True)
        return jsonify({
            "provider": "tidalwave",
            "query": query,
            "error": "provider_error",
            "results": [],
        })

    raw_key = _TYPE_KEY_MAP.get(kind, "tracks")
    raw_items = raw.get(raw_key, []) if isinstance(raw, dict) else []

    results = [_map_result(item, kind) for item in raw_items]

    return jsonify({
        "provider": "tidalwave",
        "query": query,
        "error": None,
        "results": results,
    })


@bp.route("/track/<track_id>/lyrics")
def track_lyrics(track_id: str):
    """Get lyrics for a track (HTMX partial)."""
    from app.lyrics import get_lyrics
    data = get_lyrics(track_id)
    if not data:
        return "<p class='hint'>Lyrics not available.</p>"
    return render_template("partials/lyrics.html", lyrics=data)
