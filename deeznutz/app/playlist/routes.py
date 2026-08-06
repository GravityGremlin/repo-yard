"""Playlist import — paste Deezer playlist URL, resolve tracks, batch download."""

from __future__ import annotations

import logging
import re
import uuid

from flask import Blueprint, render_template, request, jsonify

from app.models import Job, save_job
from app.download import controller
from app.deezer.session import get_session

logger = logging.getLogger(__name__)

bp = Blueprint("playlist", __name__, url_prefix="/playlist")

_PLAYLIST_RE = re.compile(r"deezer\.com/(?:[a-z]{2}(?:-[a-z]{2})?/)?playlist/(\d+)", re.IGNORECASE)


@bp.route("/")
def playlist_page():
    """Show playlist import page."""
    return render_template("playlist.html")


def _wants_json() -> bool:
    """Return True if the client asked for a JSON response."""
    return (
        request.args.get("format") == "json"
        or request.headers.get("Accept") == "application/json"
    )


def _json_envelope(url: str, error: str | None, playlist: dict | None,
                   tracks: list[dict]) -> dict:
    """Build the canonical JSON envelope for resolve responses."""
    return {
        "provider": "deeznutz",
        "url": url,
        "error": error,
        "playlist": playlist,
        "tracks": tracks,
    }


def _track_to_json(t: dict) -> dict:
    """Map a track dict (from Deezer API) onto the canonical JSON track dict."""
    track_id = t.get("id", "")

    raw_duration = t.get("duration", 0)
    try:
        duration_ms = int(raw_duration) * 1000
    except (TypeError, ValueError):
        duration_ms = 0

    isrc = t.get("isrc") or None

    # Cover: cascade through album picture fields.
    album = t.get("album") or {}
    cover_url = (
        album.get("cover_xl")
        or album.get("cover_big")
        or album.get("cover_medium")
        or album.get("cover_small")
        or album.get("cover")
        or None
    )

    artist = t.get("artist") or {}
    artist_name = artist.get("name") or "Unknown"
    album_name = album.get("title") or ""

    # Prefer the link from the API; fall back to building it.
    url = t.get("link") or f"https://www.deezer.com/track/{track_id}"

    return {
        "type": "track",
        "id": str(track_id),
        "title": t.get("title") or t.get("title_short") or "",
        "artist": artist_name,
        "album": album_name,
        "cover_url": cover_url,
        "duration_ms": duration_ms,
        "isrc": isrc,
        "url": url,
        "year": None,
    }


def _track_to_html(t: dict) -> dict:
    """Map a track dict (from Deezer API) into the shape the HTML template expects."""
    return {
        "id": t.get("id", ""),
        "title": t.get("title") or t.get("title_short") or "Unknown",
        "artist": (t.get("artist") or {}).get("name", "Unknown"),
        "album": (t.get("album") or {}).get("title", ""),
        "duration": t.get("duration", 0),
        "isrc": t.get("isrc", ""),
    }


@bp.route("/resolve", methods=["POST"])
def resolve():
    """Resolve a Deezer playlist URL and show its tracks."""
    url = (request.json or {}).get("url", "").strip() if request.is_json else request.form.get("url", "").strip()
    wants_json = _wants_json()

    if not url:
        if wants_json:
            return jsonify(_json_envelope("", "invalid_url", None, []))
        return render_template("partials/error.html", message="URL is required"), 400

    m = _PLAYLIST_RE.search(url)
    if not m:
        if wants_json:
            return jsonify(_json_envelope(url, "invalid_url", None, []))
        return render_template("partials/error.html", message="Not a valid Deezer playlist URL"), 400

    playlist_id = m.group(1)
    session = get_session()
    if not session:
        if wants_json:
            return jsonify(_json_envelope(url, "auth_expired", None, []))
        return render_template("partials/error.html", message="Not connected to Deezer"), 503

    try:
        playlist_data = session.api.get_playlist(playlist_id)
    except Exception as exc:
        logger.error("Playlist resolve failed: %s", exc, exc_info=True)
        if wants_json:
            return jsonify(_json_envelope(url, "provider_error", None, []))
        return render_template("partials/error.html", message=f"Playlist resolve failed: {exc}"), 500

    raw_tracks = (playlist_data.get("tracks") or {}).get("data") or []

    track_list = [_track_to_html(t) for t in raw_tracks]

    if wants_json:
        json_tracks = [_track_to_json(t) for t in raw_tracks]

        # Playlist metadata from the API dict.
        playlist_cover = (
            playlist_data.get("picture_xl")
            or playlist_data.get("picture_big")
            or playlist_data.get("picture_medium")
            or playlist_data.get("picture_small")
            or playlist_data.get("picture")
            or None
        )

        owner = ""
        creator = playlist_data.get("creator") or playlist_data.get("user") or {}
        if isinstance(creator, dict):
            owner = creator.get("name") or ""

        playlist_meta = {
            "id": str(playlist_data.get("id", playlist_id)),
            "title": playlist_data.get("title") or url,
            "owner": owner,
            "cover_url": playlist_cover,
            "track_count": len(json_tracks),
        }

        return jsonify(_json_envelope(url, None, playlist_meta, json_tracks))

    total_duration = sum(int(t["duration"]) for t in track_list if t["duration"])

    return render_template("partials/playlist_tracks.html",
                           playlist_name=playlist_data.get("title", "Playlist"),
                           playlist_id=playlist_id,
                           total_duration=total_duration,
                           tracks=track_list)


@bp.route("/download", methods=["POST"])
def download():
    """Queue all tracks from a resolved playlist for download."""
    if request.is_json:
        data = request.get_json()
    else:
        data = request.form

    playlist_id = data.get("playlist_id", "")
    track_ids = data.get("track_ids", [])

    if not track_ids:
        return jsonify({"error": "No tracks selected"}), 400

    group_id = uuid.uuid4().hex[:8]
    playlist_name = data.get("playlist_name", "Playlist")
    playlist_id = data.get("playlist_id", "")

    jobs = []
    for tid in track_ids:
        job = Job(
            url=f"https://deezer.com/track/{tid}",
            title=playlist_name,
            kind="track",
            group_id=group_id,
        )
        save_job(job)
        controller.enqueue(job.id)
        jobs.append(job.id)

    return jsonify({"queued": len(jobs), "job_ids": jobs, "group_id": group_id,
                    "playlist_id": playlist_id})
