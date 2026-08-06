"""Search routes — search Qobuz for albums, tracks, and artists."""

from __future__ import annotations

import logging
import time

from flask import Blueprint, jsonify, render_template, request

from app.qobuz.session import get_session
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


# ── Qobuz CDN cover helper ──────────────────────────────────────
_QOBUZ_CDN = "https://static.qobuz.com/images"


def _cover_url(item: dict, kind: str = "album") -> str:
    """Build a cover URL from a Qobuz dict's cover info."""
    if kind == "artist":
        pic = item.get("ART_PICTURE", "") or item.get("artist", {}).get("picture", "")
    else:
        pic = item.get("ALB_PICTURE", "") or item.get("image", {}).get("large", "")
    if not pic:
        return ""
    # If it's already a full URL, return as-is
    if pic.startswith("http"):
        return pic
    return f"{_QOBUZ_CDN}/{pic}/600x600.jpg"


@bp.route("/")
def index():
    """Main search page."""
    return render_template("index.html",
        query=request.args.get("q", ""),
        type=request.args.get("type", "all"),
    )


def _parse_search_results(raw: dict, kind: str) -> dict:
    """Parse raw search results into a structured dict for templates."""
    artists, albums = _get_library_badge_data()
    results: dict[str, list] = {"tracks": [], "albums": [], "artists": [], "playlists": []}
    if not raw:
        return results

    def _parse_track(item: dict) -> dict:
        title = item.get("SNG_TITLE", item.get("title", ""))
        artist = item.get("ART_NAME", item.get("artist", {}).get("name", ""))
        album = item.get("ALB_TITLE", item.get("album", {}).get("title", ""))
        track_id = str(item.get("SNG_ID", item.get("id", "")))
        duration = item.get("DURATION", item.get("duration", 0))
        in_lib = artist in artists
        return {
            "id": track_id, "title": title, "artist": artist,
            "album": album, "duration": duration, "in_library": in_lib,
            "type": "track",
        }

    def _parse_album(item: dict) -> dict:
        title = item.get("ALB_TITLE", item.get("title", ""))
        artist = item.get("ART_NAME", item.get("artist", {}).get("name", ""))
        album_id = str(item.get("ALB_ID", item.get("id", "")))
        year = item.get("DIGITAL_RELEASE_DATE", item.get("release_date", ""))
        if year and len(year) >= 4:
            year = year[:4]
        num_tracks = item.get("NUMBER_TRACKS", item.get("tracks_count", 0))
        in_lib = (artist, title) in albums
        cover = _cover_url(item)
        return {
            "id": album_id, "title": title, "artist": artist,
            "year": year, "num_tracks": num_tracks,
            "cover": cover, "in_library": in_lib, "type": "album",
        }

    def _parse_artist(item: dict) -> dict:
        name = item.get("ART_NAME", item.get("name", ""))
        artist_id = str(item.get("ART_ID", item.get("id", "")))
        in_lib = name in artists
        pic = item.get("ART_PICTURE", "") or item.get("picture", "")
        if pic and not pic.startswith("http"):
            pic = f"{_QOBUZ_CDN}/{pic}/250x250.jpg" if pic else ""
        return {"id": artist_id, "name": name, "in_library": in_lib, "picture": pic}

    if kind == "all":
        for track in raw.get("TRACK", {}).get("data", []):
            results["tracks"].append(_parse_track(track))
        for album in raw.get("ALBUM", {}).get("data", []):
            results["albums"].append(_parse_album(album))
        for artist in raw.get("ARTIST", {}).get("data", []):
            results["artists"].append(_parse_artist(artist))
        for pl in raw.get("PLAYLIST", {}).get("data", []):
            results["playlists"].append({
                "id": str(pl.get("PLAYLIST_ID", pl.get("id", ""))),
                "title": pl.get("PLAYLIST_TITLE", pl.get("title", "")),
                "nb_tracks": pl.get("NB_TRACKS", pl.get("nb_tracks", 0)),
            })
    elif kind == "track":
        for track in raw.get("data", raw.get("TRACK", {}).get("data", [])):
            results["tracks"].append(_parse_track(track))
    elif kind == "album":
        for album in raw.get("data", raw.get("ALBUM", {}).get("data", [])):
            results["albums"].append(_parse_album(album))
    elif kind == "artist":
        for artist in raw.get("data", raw.get("ARTIST", {}).get("data", [])):
            results["artists"].append(_parse_artist(artist))

    return results


# ── JSON search endpoint ────────────────────────────────────────

_ALLOWED_JSON_TYPES = {"track", "album", "artist", "playlist"}


def _to_json_result(item: dict, kind: str) -> dict:
    """Map a raw Qobuz item to the canonical JSON result envelope."""
    if kind == "artist":
        title = item.get("ART_NAME", item.get("name", ""))
        artist = title
        album = ""
        item_id = str(item.get("ART_ID", item.get("id", "")))
        cover = _cover_url(item, "artist") or None
        duration_ms = 0
        year = None
    elif kind == "album":
        title = item.get("ALB_TITLE", item.get("title", ""))
        artist = item.get("ART_NAME", item.get("artist", {}).get("name", ""))
        album = title
        item_id = str(item.get("ALB_ID", item.get("id", "")))
        cover = _cover_url(item) or None
        duration_ms = 0
        yr = item.get("DIGITAL_RELEASE_DATE", item.get("release_date", ""))
        year = int(yr[:4]) if yr and len(yr) >= 4 else None
    elif kind == "playlist":
        title = item.get("PLAYLIST_TITLE", item.get("title", ""))
        artist = ""
        album = ""
        item_id = str(item.get("PLAYLIST_ID", item.get("id", "")))
        cover = None
        duration_ms = 0
        year = None
    else:  # track (default)
        title = item.get("SNG_TITLE", item.get("title", ""))
        artist = item.get("ART_NAME", item.get("artist", {}).get("name", ""))
        album = item.get("ALB_TITLE", item.get("album", {}).get("title", ""))
        item_id = str(item.get("SNG_ID", item.get("id", "")))
        cover = _cover_url(item) or None
        dur = item.get("DURATION", item.get("duration", 0))
        duration_ms = (dur * 1000) if dur else 0
        yr = item.get("release_date", "")
        year = int(yr[:4]) if yr and len(yr) >= 4 else None

    return {
        "type": kind,
        "id": item_id,
        "title": title,
        "artist": artist,
        "album": album,
        "cover_url": cover,
        "duration_ms": duration_ms,
        "isrc": item.get("isrc") or None,
        "url": "",
        "year": year,
    }


@bp.route("/search/json")
def search_json():
    """JSON API for search — returns canonical JSON envelope."""
    query = request.args.get("q", "").strip()
    kind = request.args.get("type", "track")
    if kind not in _ALLOWED_JSON_TYPES:
        kind = "track"

    envelope: dict = {
        "provider": "qoochie",
        "query": query,
        "error": None,
        "results": [],
    }

    if not query:
        return jsonify(envelope)

    session = get_session()
    if not session:
        envelope["error"] = "auth_expired"
        return jsonify(envelope)

    try:
        if kind == "track":
            raw = session.search_music(query, type="TRACK")
            items = raw.get("data", raw.get("TRACK", {}).get("data", []))
        elif kind == "album":
            raw = session.search_music(query, type="ALBUM")
            items = raw.get("data", raw.get("ALBUM", {}).get("data", []))
        elif kind == "artist":
            raw = session.search_music(query, type="ARTIST")
            items = raw.get("data", raw.get("ARTIST", {}).get("data", []))
        else:  # playlist
            raw = session.search(query)
            items = raw.get("PLAYLIST", {}).get("data", [])
    except Exception as exc:
        logger.error("Qobuz JSON search failed: %s", exc, exc_info=True)
        envelope["error"] = "provider_error"
        return jsonify(envelope)

    envelope["results"] = [_to_json_result(item, kind) for item in items]
    return jsonify(envelope)


@bp.route("/search")
def search():
    """Search Qobuz and return results as an HTMX partial."""
    query = request.args.get("q", "").strip()
    kind = request.args.get("type", "all")

    if not query:
        return ""

    session = get_session()
    if not session:
        return render_template("partials/error.html",
                               message="Not connected to Qobuz."), 503

    try:
        if kind == "all":
            raw = session.search(query)
        else:
            type_map = {
                "album": "ALBUM",
                "track": "TRACK",
                "artist": "ARTIST",
            }
            qobuz_type = type_map.get(kind, "TRACK")
            raw = session.search_music(query, type=qobuz_type)
    except Exception as exc:
        logger.error("Qobuz search failed: %s", exc)
        return render_template("partials/error.html", message=f"Search failed: {exc}"), 500

    results = _parse_search_results(raw, kind)

    return render_template("partials/search_results.html", results=results, query=query)


@bp.route("/search/album/<album_id>/tracks")
def album_tracks(album_id: str):
    """Get tracks for a specific album (HTMX partial for expand)."""
    session = get_session()
    if not session:
        return render_template("partials/error.html",
                               message="Not connected to Qobuz."), 503

    try:
        # Album IDs are alphanumeric (e.g. "lxsldzwb4izfa") — pass raw id;
        # get_album_tracks stringifies and no longer needs int().
        tracks = session.get_album_tracks(album_id)
        # Get album metadata for the template header
        album_info = {"id": album_id, "ALB_TITLE": "", "ART_NAME": ""}
        if tracks:
            # Use first track's album info
            album_info["ALB_TITLE"] = tracks[0].get("ALB_TITLE", "")
            album_info["ART_NAME"] = tracks[0].get("ART_NAME", "")
    except NotImplementedError:
        return render_template("partials/error.html",
                               message="Album tracks not yet implemented for Qobuz."), 501
    except Exception as exc:
        logger.error("Failed to get album tracks: %s", exc)
        return render_template("partials/error.html", message=str(exc)), 500

    return render_template("partials/album_tracks.html",
                           tracks=tracks, album=album_info)
