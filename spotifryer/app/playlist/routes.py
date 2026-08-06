"""Playlist import — paste Spotify playlist URL, resolve tracks, batch download."""

from __future__ import annotations

import logging

from flask import Blueprint, request, jsonify, render_template

from app.spotify.resolver import resolve_url, fetch_playlist_tracks, _largest_image
from app.spotify.session import get_spotify_client, is_authenticated
from app.download.controller import enqueue_download

logger = logging.getLogger(__name__)

playlist_bp = Blueprint("playlist", __name__, url_prefix="/playlist")


def _require_auth():
    """Return None if authenticated, otherwise a 401 JSON response."""
    if not is_authenticated():
        return jsonify({"error": "not_authenticated"}), 401
    return None


@playlist_bp.route("/resolve", methods=["POST"])
def resolve():
    """Resolve a Spotify playlist URL and return its tracks.
    Requires Spotify user auth — playlist tracks are not accessible via app-only credentials.
    """
    _is_json = (
        request.headers.get("Accept") == "application/json"
        or request.args.get("format") == "json"
    )

    auth_err = _require_auth()
    if auth_err:
        if _is_json:
            return jsonify({
                "provider": "spotifryer",
                "url": "",
                "error": "auth_expired",
                "playlist": None,
                "tracks": [],
            }), 401
        return jsonify({
            "error": "not_authenticated",
            "message": "Connect your Spotify account first. Click '○ Spotify' in the nav bar to authorize.",
        }), 401

    if request.is_json:
        data = request.get_json()
    else:
        data = request.form

    url = (data.get("url") or "").strip()
    if not url:
        if _is_json:
            return jsonify({
                "provider": "spotifryer",
                "url": "",
                "error": "invalid_url",
                "playlist": None,
                "tracks": [],
            }), 200
        return jsonify({"error": "URL is required"}), 400

    try:
        kind, spotify_id = resolve_url(url)
    except ValueError as exc:
        if _is_json:
            return jsonify({
                "provider": "spotifryer",
                "url": url,
                "error": "invalid_url",
                "playlist": None,
                "tracks": [],
            }), 200
        return jsonify({"error": str(exc)}), 400

    if kind != "playlist":
        if _is_json:
            return jsonify({
                "provider": "spotifryer",
                "url": url,
                "error": "invalid_url",
                "playlist": None,
                "tracks": [],
            }), 200
        return jsonify({"error": f"Not a playlist URL (detected: {kind})"}), 400

    try:
        tracks, playlist_name = fetch_playlist_tracks(spotify_id)
    except Exception as exc:
        logger.error("Playlist resolve failed: %s", exc, exc_info=True)
        if _is_json:
            return jsonify({
                "provider": "spotifryer",
                "url": url,
                "error": "provider_error",
                "playlist": None,
                "tracks": [],
            }), 200
        return render_template("partials/playlist_results.html",
            playlist={}, tracks=[], error=f"Playlist resolve failed: {exc}"), 502

    # Fetch additional playlist metadata (owner, cover, description)
    try:
        sp = get_spotify_client()
        # spotipy v3 uses 'items' not 'tracks' for playlist track metadata
        playlist_meta = sp.playlist(spotify_id, fields="name,description,owner(display_name),images,items(total)")
    except Exception:
        logger.warning("Failed to fetch playlist metadata for %s", spotify_id)
        playlist_meta = {}

    playlist_obj = {
        "title": playlist_meta.get("name", playlist_name),
        "artist": playlist_meta.get("owner", {}).get("display_name", ""),
        "spotify_id": spotify_id,
        "cover_url": _largest_image(playlist_meta.get("images", [])),
        "description": playlist_meta.get("description", ""),
        # sp.playlist() returns 'items' not 'tracks' (spotipy maps the tracks paging object)
        "track_count": playlist_meta.get("items", {}).get("total", len(tracks)),
    }

    # JSON fallback for API consumers
    if _is_json:
        return jsonify({
            "provider": "spotifryer",
            "url": url,
            "error": None,
            "playlist": {
                "id": playlist_obj["spotify_id"],
                "title": playlist_obj["title"],
                "owner": playlist_obj["artist"],
                "cover_url": playlist_obj["cover_url"],
                "track_count": playlist_obj["track_count"],
            },
            "tracks": [
                {
                    "type": "track",
                    "id": t.get("spotify_id", ""),
                    "title": t.get("title", ""),
                    "artist": t.get("artist", ""),
                    "album": "",
                    "cover_url": None,
                    "duration_ms": None,
                    "isrc": None,
                    "url": "",
                    "year": None,
                }
                for t in tracks
            ],
        })

    return render_template("partials/playlist_results.html",
        playlist=playlist_obj, tracks=tracks)


@playlist_bp.route("/download-all", methods=["POST"])
def download_all():
    """Enqueue downloads for all tracks in a Spotify playlist."""
    auth_err = _require_auth()
    if auth_err:
        return auth_err

    if request.is_json:
        data = request.get_json()
    else:
        data = request.form

    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"error": "URL is required"}), 400

    try:
        job_ids = enqueue_download(url, kind="playlist")
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 502

    # JSON fallback for API consumers
    if request.headers.get("Accept") == "application/json" or request.args.get("format") == "json":
        return jsonify({
            "job_ids": job_ids,
            "track_count": len(job_ids),
        })

    return render_template("partials/playlist_download_started.html",
        track_count=len(job_ids), job_ids=job_ids)
