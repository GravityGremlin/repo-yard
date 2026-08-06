"""Search routes — search Spotify for tracks, albums, and playlists."""

from __future__ import annotations

import logging
import re

from flask import Blueprint, render_template, request, jsonify

from app.spotify.resolver import search_spotify
from app.spotify.session import is_authenticated

logger = logging.getLogger(__name__)

search_bp = Blueprint("search", __name__)

_MAX_QUERY_LEN = 100


def _sanitize_query(raw: str) -> str:
    """Strip control characters and HTML-dangerous chars, enforce length limit."""
    q = re.sub(r'[<>\"\']', '', raw)
    return q[:_MAX_QUERY_LEN]


def _require_auth():
    """Return None if authenticated, otherwise a 401 JSON response."""
    if not is_authenticated():
        return jsonify({"error": "not_authenticated"}), 401
    return None


@search_bp.route("/")
def index():
    """Main search page."""
    return render_template("index.html",
        query=request.args.get("q", ""),
        type=request.args.get("type", "track"),
        authenticated=is_authenticated(),
    )


@search_bp.route("/search/api")
def search_api():
    """Search Spotify and return results as JSON."""
    auth_err = _require_auth()
    if auth_err:
        return auth_err

    query = _sanitize_query(request.args.get("q", "").strip())
    if not query:
        return jsonify([])

    kind = request.args.get("type", "track")
    limit = request.args.get("limit", 10, type=int)
    limit = max(1, min(limit, 50))

    try:
        results = search_spotify(query, kind=kind, limit=limit)
    except Exception as exc:
        logger.error("Spotify search failed: %s", exc)
        return jsonify({"error": str(exc)}), 502

    return jsonify(results)


@search_bp.route("/search")
def search_html():
    """Search Spotify and return results as HTML partial (for HTMX)."""
    auth_err = _require_auth()
    if auth_err:
        return auth_err

    query = _sanitize_query(request.args.get("q", "").strip())
    kind = request.args.get("type", "track")
    limit = request.args.get("limit", 10, type=int)
    limit = max(1, min(limit, 50))

    if not query:
        return render_template("partials/search_results.html",
            results=[], query=query, type=kind)

    try:
        results = search_spotify(query, kind=kind, limit=limit)
    except Exception as exc:
        logger.error("Spotify search failed: %s", exc)
        return render_template("partials/error.html", message=str(exc)), 502

    return render_template("partials/search_results.html",
        results=results, query=query, type=kind)


# ── Lyrics ────────────────────────────────────────────────────────────────
@search_bp.route("/track/<track_id>/lyrics")
def track_lyrics(track_id: str):
    """Fetch lyrics for a track via Lrclib. Returns HTML partial."""
    from app.lyrics import fetch_lyrics
    from app.spotify.resolver import fetch_track

    try:
        meta = fetch_track(track_id)
    except Exception:
        return render_template("partials/lyrics.html", lyrics=None)

    lyrics = fetch_lyrics(
        artist=meta.get("artist", ""),
        title=meta.get("title", ""),
        album=meta.get("album", ""),
        duration=meta.get("duration_ms", 0) // 1000,  # Lrclib expects seconds
    )
    return render_template("partials/lyrics.html", lyrics=lyrics)


# ── Canonical JSON endpoint ──────────────────────────────────────

def _map_to_canonical(item: dict, kind: str) -> dict:
    """Map a search_spotify result dict onto the canonical JSON contract."""
    return {
        "type": kind,
        "id": item.get("spotify_id", ""),
        "title": item.get("title", ""),
        "artist": item.get("artist", ""),
        "album": item.get("album", ""),
        "cover_url": item.get("cover_url") or None,
        "duration_ms": item.get("duration_ms", 0) or 0,
        "isrc": item.get("isrc") or None,
        "url": item.get("url", "") or "",
        "year": item.get("year"),
    }


@search_bp.route("/search/json")
def search_json():
    """Canonical JSON search endpoint.

    Returns a consistent envelope: {provider, query, error, results}.
    Always responds HTTP 200 — errors are encoded in the payload.
    """
    if not is_authenticated():
        return jsonify({
            "provider": "spotifryer",
            "query": request.args.get("q", ""),
            "error": "auth_expired",
            "results": [],
        })

    query = _sanitize_query(request.args.get("q", "").strip())
    kind = request.args.get("type", "track")

    if not query:
        return jsonify({
            "provider": "spotifryer",
            "query": query,
            "error": None,
            "results": [],
        })

    try:
        raw_results = search_spotify(query, kind=kind, limit=10)
    except Exception as exc:
        logger.error("Spotify search failed: %s", exc, exc_info=True)
        return jsonify({
            "provider": "spotifryer",
            "query": query,
            "error": "provider_error",
            "results": [],
        })

    results = [_map_to_canonical(item, kind) for item in raw_results]

    return jsonify({
        "provider": "spotifryer",
        "query": query,
        "error": None,
        "results": results,
    })
