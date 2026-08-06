"""Playlist import — paste Tidal playlist URL, resolve tracks, batch download."""

from __future__ import annotations

import logging
import re
import uuid

from flask import Blueprint, render_template, request, jsonify

from app.models import Job, save_job
from app.download import controller
from app.tidal.session import get_session

logger = logging.getLogger(__name__)

bp = Blueprint("playlist", __name__, url_prefix="/playlist")

_PLAYLIST_RE = re.compile(r"tidal\.com/playlist/([a-f0-9-]+)", re.IGNORECASE)


def _is_json_request() -> bool:
    """Return True if the client wants a JSON response."""
    return request.args.get("format") == "json" or request.headers.get("Accept") == "application/json"


def _track_to_json(track) -> dict:
    """Map a tidalapi Track object to the canonical JSON track shape."""
    return {
        "type": "track",
        "id": str(track.id),
        "title": getattr(track, "title", "") or getattr(track, "name", ""),
        "artist": track.artist.name if getattr(track, "artist", None) else None,
        "album": track.album.name if getattr(track, "album", None) else None,
        "cover_url": getattr(track.album, "image", lambda s: None)(320) if getattr(track, "album", None) else None,
        "duration_ms": (getattr(track, "duration", 0) or 0) * 1000,
        "isrc": getattr(track, "isrc", None) or None,
        "url": getattr(track, "url", "") or "",
        "year": getattr(track.album, "year", None) if getattr(track, "album", None) else None,
    }


@bp.route("/")
def playlist_page():
    """Show playlist import page."""
    return render_template("playlist.html")


@bp.route("/resolve", methods=["POST"])
def resolve():
    """Resolve a Tidal playlist URL and show its tracks."""
    url = (request.json or {}).get("url", "").strip() if request.is_json else request.form.get("url", "").strip()

    wants_json = _is_json_request()

    if not url:
        if wants_json:
            return jsonify({
                "provider": "tidalwave",
                "url": url or "",
                "error": "invalid_url",
                "playlist": None,
                "tracks": [],
            })
        return render_template("partials/error.html", message="URL is required"), 400

    m = _PLAYLIST_RE.search(url)
    if not m:
        if wants_json:
            return jsonify({
                "provider": "tidalwave",
                "url": url,
                "error": "invalid_url",
                "playlist": None,
                "tracks": [],
            })
        return render_template("partials/error.html", message="Not a valid Tidal playlist URL"), 400

    playlist_id = m.group(1)
    session = get_session()
    if not session:
        if wants_json:
            return jsonify({
                "provider": "tidalwave",
                "url": url,
                "error": "auth_expired",
                "playlist": None,
                "tracks": [],
            })
        return render_template("partials/error.html", message="Not connected to Tidal"), 503

    try:
        playlist = session.playlist(playlist_id)
        tracks = playlist.tracks()
    except Exception as exc:
        logger.error("Playlist resolve failed: %s", exc, exc_info=True)
        if wants_json:
            return jsonify({
                "provider": "tidalwave",
                "url": url,
                "error": "provider_error",
                "playlist": None,
                "tracks": [],
            })
        return render_template("partials/error.html", message=f"Playlist resolve failed: {exc}"), 500

    # Build track list — shared by both paths
    track_list = []
    for t in tracks:
        track_list.append({
            "id": t.id,
            "title": t.name,
            "artist": t.artist.name if t.artist else "Unknown",
            "album": t.album.name if t.album else "",
            "duration": getattr(t, "duration", 0),
            "isrc": getattr(t, "isrc", ""),
        })

    total_duration = sum(int(t["duration"]) for t in track_list if t["duration"])

    if wants_json:
        json_tracks = [_track_to_json(t) for t in tracks]
        cover_url = None
        if hasattr(playlist, "image"):
            try:
                cover_url = playlist.image(320)
            except Exception:
                cover_url = None
        return jsonify({
            "provider": "tidalwave",
            "url": url,
            "error": None,
            "playlist": {
                "id": playlist_id,
                "title": getattr(playlist, "name", None) or url,
                "owner": getattr(playlist.owner, "displayName", "") if getattr(playlist, "owner", None) else "",
                "cover_url": cover_url,
                "track_count": len(json_tracks),
            },
            "tracks": json_tracks,
        })

    return render_template("partials/playlist_tracks.html",
                           playlist_name=playlist.name,
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
            url=f"https://tidal.com/track/{tid}",
            title=playlist_name,
            kind="track",
            group_id=group_id,
        )
        save_job(job)
        controller.enqueue(job.id)
        jobs.append(job.id)

    return jsonify({"queued": len(jobs), "job_ids": jobs, "group_id": group_id,
                    "playlist_id": playlist_id})
