"""Search routes — search Deezer for albums, tracks, and artists."""

from __future__ import annotations

import logging
import threading
import time

from flask import Blueprint, jsonify, render_template, request

from app.deezer.session import get_session
from app.config import LIBRARY_DIR

logger = logging.getLogger(__name__)

# ── Library badge cache (avoids walking LIBRARY_DIR on every search) ──
_badge_cache: dict = {"timestamp": 0, "artists": set(), "albums": set()}
_BADGE_CACHE_TTL = 60  # seconds
_badge_lock = threading.Lock()


def _get_library_badge_data():
    """Return (artist_names, album_tuples) from LIBRARY_DIR, cached for 60s."""
    with _badge_lock:
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


# ── Deezer CDN cover helper ──────────────────────────────────────────
_DEEZER_CDN = "https://e-cdns-images.dzcdn.net/images"


@bp.route("/")
def index():
    """Main search page."""
    return render_template("index.html",
        query=request.args.get("q", ""),
        type=request.args.get("type", "all"),
    )


@bp.route("/search")
def search():
    """Search Deezer and return results as HTML partial (for HTMX)."""
    query = request.args.get("q", "").strip()
    kind = request.args.get("type", "all")  # all | album | track | artist

    if not query:
        return render_template("partials/search_results.html", results=None, query="")

    session = get_session()
    if not session:
        return render_template("partials/error.html",
                               message="Not connected to Deezer. <a href='/deezer/auth'>Connect here</a>."), 503

    try:
        # gw.search() returns pageSearch results (flat list with __TYPE__ fields)
        # gw.search_music() returns type-specific results
        if kind == "all":
            raw = session.gw.search(query)
        else:
            type_map = {
                "album": "ALBUM",
                "track": "TRACK",
                "artist": "ARTIST",
            }
            deezer_type = type_map.get(kind, "TRACK")
            raw = session.gw.search_music(query, type=deezer_type)
    except Exception as exc:
        logger.error("Deezer search failed: %s", exc)
        return render_template("partials/error.html", message=f"Search failed: {exc}"), 500

    results = _parse_search_results(raw, kind)

    return render_template("partials/search_results.html", results=results, query=query)


@bp.route("/search/album/<album_id>/tracks")
def album_tracks(album_id: str):
    """Get tracks for a specific album (HTMX partial for expand)."""
    session = get_session()
    if not session:
        return render_template("partials/error.html",
                               message="Not connected to Deezer."), 503

    try:
        tracks = session.gw.get_album_tracks(int(album_id))
        # Get album metadata for the template header
        album_info = {"id": album_id, "ALB_TITLE": "", "ART_NAME": ""}
        if tracks:
            # Use first track's album info
            album_info["ALB_TITLE"] = tracks[0].get("ALB_TITLE", "")
            album_info["ART_NAME"] = tracks[0].get("ART_NAME", "")
    except Exception as exc:
        logger.error("Failed to get album tracks: %s", exc)
        return render_template("partials/error.html", message=str(exc)), 500

    return render_template("partials/album_tracks.html", album=album_info, tracks=tracks)


def _parse_search_results(raw, kind: str) -> dict:
    """Parse deezer-py search results into a serializable dict.

    gw.search() returns a dict with category keys: ARTIST, ALBUM, TRACK,
    PLAYLIST — each is a dict with a ``data`` list.
    """
    results: dict[str, list] = {"albums": [], "tracks": [], "artists": [], "playlists": []}

    if not raw or not isinstance(raw, dict):
        return results

    artist_names, album_tuples = _get_library_badge_data()
    _sanitize = str.maketrans({"/": "-", "\\": "-", ":": "-"})

    if kind in ("all", "album"):
        for item in _extract_category(raw, "ALBUM"):
            a_name = item.get("ART_NAME", "Unknown")
            a_title = item.get("ALB_TITLE", "Unknown")
            in_lib = (a_name.translate(_sanitize), a_title.translate(_sanitize)) in album_tuples
            results["albums"].append({
                "id": str(item.get("ALB_ID", "")),
                "title": a_title,
                "artist": a_name,
                "year": _parse_release_year(item),
                "num_tracks": item.get("NUMBER_TRACK", 0) or 0,
                "cover": _cover_url(item, "album"),
                "in_library": in_lib,
            })

    if kind in ("all", "track"):
        for item in _extract_category(raw, "TRACK"):
            results["tracks"].append({
                "id": str(item.get("SNG_ID", "")),
                "title": item.get("SNG_TITLE", "Unknown"),
                "artist": item.get("ART_NAME", "Unknown"),
                "album": item.get("ALB_TITLE", ""),
                "duration": item.get("DURATION", 0) or 0,
                "isrc": item.get("ISRC", ""),
            })

    if kind in ("all", "artist"):
        for item in _extract_category(raw, "ARTIST"):
            a_name = item.get("ART_NAME", "Unknown")
            results["artists"].append({
                "id": str(item.get("ART_ID", "")),
                "name": a_name,
                "picture": _cover_url(item, "artist"),
                "nb_fan": item.get("NB_FAN", 0),
                "in_library": a_name in artist_names,
            })

    if kind in ("all", "playlist"):
        for item in _extract_category(raw, "PLAYLIST"):
            results["playlists"].append({
                "id": str(item.get("PLAYLIST_ID", "")),
                "title": item.get("TITLE", "Unknown"),
                "description": item.get("DESCRIPTION", ""),
                "num_tracks": item.get("NB_TRACK", 0) or 0,
                "picture": _cover_url(item, "playlist"),
            })

    return results


def _extract_category(raw: dict, key: str) -> list[dict]:
    """Safely extract the ``data`` list from a category-keyed dict."""
    obj = raw.get(key)
    if isinstance(obj, dict):
        return obj.get("data", []) or []
    if isinstance(obj, list):
        return obj
    return []


def _parse_release_year(item: dict) -> int | None:
    """Extract a release year from various Deezer date fields."""
    raw_date = item.get("DIGITAL_RELEASE_DATE") or item.get("PHYSICAL_RELEASE_DATE") or ""
    if raw_date and len(str(raw_date)) >= 4:
        try:
            return int(str(raw_date)[:4])
        except (ValueError, TypeError):
            pass
    return None


def _cover_url(item: dict, kind: str = "album") -> str:
    """Build a Deezer CDN cover-art URL from an item dict."""
    size_map = {"album": "500x500", "artist": "200x200", "playlist": "500x500"}
    size = size_map.get(kind, "500x500")
    field_map = {"album": "ALB_PICTURE", "artist": "ART_PICTURE", "playlist": "PICTURE"}
    hash_val = item.get(field_map.get(kind, "ALB_PICTURE"), "")
    if not hash_val:
        hash_val = item.get("MD5_ORIGIN", "") or item.get("PLAYLIST_PICTURE", "")
    if hash_val:
        return f"https://e-cdns-images.dzcdn.net/images/{kind}/{hash_val}/{size}-000000-80-0-0.jpg"
    return ""


@bp.route("/track/<track_id>/lyrics")
def track_lyrics(track_id: str):
    """Get lyrics for a track (HTMX partial)."""
    from app.lyrics import fetch_lyrics
    data = fetch_lyrics(track_id)
    if not data:
        return "<p class='hint'>Lyrics not available.</p>"
    return render_template("partials/lyrics.html", lyrics=data)


# ── JSON search endpoint ──────────────────────────────────────────────

_DEEZER_LINK_BASE = "https://www.deezer.com"


def _to_json_results(raw: dict, kind: str) -> list[dict]:
    """Map raw deezer-py search results onto the canonical JSON contract.

    Each result dict has: type, id, title, artist, album, cover_url,
    duration_ms, isrc, url, year.
    """
    results: list[dict] = []

    if kind == "track":
        for item in _extract_category(raw, "TRACK"):
            results.append({
                "type": "track",
                "id": str(item.get("SNG_ID", "")),
                "title": item.get("SNG_TITLE", ""),
                "artist": item.get("ART_NAME", ""),
                "album": item.get("ALB_TITLE", ""),
                "cover_url": _cover_url(item, "album") or None,
                "duration_ms": (int(item.get("DURATION") or 0)) * 1000,
                "isrc": item.get("ISRC") or None,
                "url": f"{_DEEZER_LINK_BASE}/track/{item.get('SNG_ID', '')}",
                "year": _parse_release_year(item),
            })
    elif kind == "album":
        for item in _extract_category(raw, "ALBUM"):
            results.append({
                "type": "album",
                "id": str(item.get("ALB_ID", "")),
                "title": item.get("ALB_TITLE", ""),
                "artist": item.get("ART_NAME", ""),
                "album": "",
                "cover_url": _cover_url(item, "album") or None,
                "duration_ms": 0,
                "isrc": None,
                "url": f"{_DEEZER_LINK_BASE}/album/{item.get('ALB_ID', '')}",
                "year": _parse_release_year(item),
            })
    elif kind == "artist":
        for item in _extract_category(raw, "ARTIST"):
            results.append({
                "type": "artist",
                "id": str(item.get("ART_ID", "")),
                "title": item.get("ART_NAME", ""),
                "artist": "",
                "album": "",
                "cover_url": _cover_url(item, "artist") or None,
                "duration_ms": 0,
                "isrc": None,
                "url": f"{_DEEZER_LINK_BASE}/artist/{item.get('ART_ID', '')}",
                "year": None,
            })
    elif kind == "playlist":
        for item in _extract_category(raw, "PLAYLIST"):
            results.append({
                "type": "playlist",
                "id": str(item.get("PLAYLIST_ID", "")),
                "title": item.get("TITLE", ""),
                "artist": "",
                "album": "",
                "cover_url": _cover_url(item, "playlist") or None,
                "duration_ms": 0,
                "isrc": None,
                "url": f"{_DEEZER_LINK_BASE}/playlist/{item.get('PLAYLIST_ID', '')}",
                "year": None,
            })

    return results


@bp.route("/search/json")
def search_json():
    """JSON search endpoint — returns results in a canonical envelope."""
    query = request.args.get("q", "").strip()
    kind = request.args.get("type", "track")  # track | album | artist | playlist

    if not query:
        return jsonify({
            "provider": "deeznutz",
            "query": "",
            "error": None,
            "results": [],
        })

    session = get_session()
    if not session:
        return jsonify({
            "provider": "deeznutz",
            "query": query,
            "error": "auth_expired",
            "results": [],
        })

    try:
        # gw.search() returns category keys (TRACK/ALBUM/ARTIST/PLAYLIST) that
        # _extract_category() expects; gw.search_music() returns a lowercase
        # data/count shape the parser does not understand.
        raw = session.gw.search(query)
    except Exception as exc:
        logger.error("Deezer search failed: %s", exc, exc_info=True)
        return jsonify({
            "provider": "deeznutz",
            "query": query,
            "error": "provider_error",
            "results": [],
        })

    return jsonify({
        "provider": "deeznutz",
        "query": query,
        "error": None,
        "results": _to_json_results(raw, kind),
    })
