"""Playlist import — paste Qobuz playlist URL, resolve tracks, batch download."""

from __future__ import annotations

import logging
import re

from flask import Blueprint, render_template, request, jsonify

from app.models import Job, save_job
from app.download import controller
from app.qobuz.session import get_session

logger = logging.getLogger(__name__)

bp = Blueprint("playlist", __name__, url_prefix="/playlist")

_PLAYLIST_RE = re.compile(r"qobuz\.com/(?:[a-z]{2}(?:-[a-z]{2})?/)?playlist/(\d+)", re.IGNORECASE)


@bp.route("/")
def playlist_page():
    """Show playlist import page."""
    return render_template("playlist.html")


@bp.route("/resolve", methods=["POST"])
def resolve():
    """Resolve a Qobuz playlist URL and show its tracks."""
    url = (request.json or {}).get("url", "").strip() if request.is_json else request.form.get("url", "").strip()

    json_mode = request.args.get("format") == "json" or request.headers.get("Accept") == "application/json"

    def _error_envelope(error: str, url_val: str = "") -> dict:
        """Build a canonical envelope with an error and null playlist/tracks."""
        return {
            "provider": "qoochie",
            "url": url_val,
            "error": error,
            "playlist": None,
            "tracks": [],
        }

    if not url:
        if json_mode:
            return jsonify(_error_envelope("invalid_url", "")), 200
        return render_template("partials/error.html", message="URL is required"), 400

    m = _PLAYLIST_RE.search(url)
    if not m:
        if json_mode:
            return jsonify(_error_envelope("invalid_url", url)), 200
        return render_template("partials/error.html", message="Not a valid Qobuz playlist URL"), 400

    playlist_id = m.group(1)
    session = get_session()
    if not session:
        if json_mode:
            return jsonify(_error_envelope("auth_expired", url)), 200
        return render_template("partials/error.html", message="Not connected to Qobuz"), 503

    try:
        data = session.get_playlist(playlist_id)
        # Flatten into track list
        tracks_data = data.get("tracks", {})
        if isinstance(tracks_data, dict):
            tracks = tracks_data.get("items", [])
        elif isinstance(tracks_data, list):
            tracks = tracks_data
        else:
            tracks = []
    except NotImplementedError:
        if json_mode:
            return jsonify(_error_envelope("provider_error", url)), 200
        return render_template("partials/error.html",
                               message="Playlist resolution not yet implemented for Qobuz."), 501
    except Exception as exc:
        logger.error("Playlist resolve failed: %s", exc, exc_info=True)
        if json_mode:
            return jsonify(_error_envelope("provider_error", url)), 200
        return render_template("partials/error.html", message=f"Playlist resolve failed: {exc}"), 500

    track_list = []
    for t in tracks:
        artist_info = t.get("performer", t.get("artist", {}))
        if isinstance(artist_info, dict):
            artist_name = artist_info.get("name", "")
        else:
            artist_name = str(artist_info)
        track_list.append({
            "id": t.get("id", t.get("track_id", "")),
            "title": t.get("title", t.get("name", "")),
            "artist": artist_name,
            "duration": t.get("duration", 0),
        })

    if json_mode:
        # ── JSON canonical envelope ──
        playlist_title = data.get("name") or data.get("title") or url
        playlist_owner = data.get("owner", {}).get("name", "") if isinstance(data.get("owner"), dict) else ""
        cover_url = data.get("image", {}).get("large", None) if isinstance(data.get("image"), dict) else None

        json_tracks = []
        for tl in track_list:
            duration_sec = tl.get("duration", 0) or 0
            json_tracks.append({
                "type": "track",
                "id": str(tl["id"]),
                "title": tl["title"],
                "artist": tl["artist"],
                "album": "",
                "cover_url": cover_url,
                "duration_ms": duration_sec * 1000 if duration_sec else None,
                "isrc": None,
                "url": f"https://www.qobuz.com/track/{tl['id']}" if tl["id"] else "",
                "year": None,
            })

        envelope = {
            "provider": "qoochie",
            "url": url,
            "error": None,
            "playlist": {
                "id": playlist_id,
                "title": playlist_title,
                "owner": playlist_owner,
                "cover_url": cover_url,
                "track_count": len(json_tracks),
            },
            "tracks": json_tracks,
        }
        return jsonify(envelope)

    return render_template("partials/playlist_tracks.html",
                           playlist={"id": playlist_id, "title": url},
                           tracks=track_list)


@bp.route("/download-all", methods=["POST"])
def download_all():
    """Queue all tracks from a resolved playlist."""
    data = request.get_json(silent=True) or request.form
    tracks = data.get("tracks", [])
    if not tracks:
        return jsonify({"error": "No tracks provided"}), 400

    job_ids = []
    for track in tracks:
        track_url = f"https://www.qobuz.com/track/{track.get('id', '')}"
        job = Job(
            url=track_url,
            title=track.get("title", ""),
            artist=track.get("artist", ""),
            kind="track",
        )
        save_job(job)
        controller.enqueue(job.id)
        job_ids.append(job.id)

    return jsonify({"status": "ok", "jobs": len(job_ids), "job_ids": job_ids})
