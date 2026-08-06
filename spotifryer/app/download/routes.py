"""Download routes — enqueue downloads, list jobs, SSE progress stream."""

from __future__ import annotations

import json
import logging
import queue

from flask import Blueprint, Response, render_template, request, jsonify, stream_with_context

from app.models import (
    Job,
    JobStatus,
    get_job,
    list_jobs,
    list_active_jobs,
    save_job,
    delete_job,
)
from app.spotify.session import is_authenticated
from app.spotify.resolver import resolve_url
from app.download import controller

logger = logging.getLogger(__name__)

bp = Blueprint("download", __name__, url_prefix="/download")


def _require_auth():
    """Return None if authenticated, otherwise a 401 JSON response."""
    if not is_authenticated():
        return jsonify({"error": "not_authenticated"}), 401
    return None


# ── Enqueue ───────────────────────────────────────────────────────────────
@bp.route("/enqueue", methods=["POST"])
def enqueue():
    """Queue a download job from a Spotify URL."""
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

    kind = data.get("kind") or None  # track | album | playlist | None (auto-detect)

    try:
        job_ids = controller.enqueue_download(url, kind=kind)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 502

    # Determine kind for response.
    if kind is None:
        try:
            detected_kind, _ = resolve_url(url)
        except ValueError:
            logger.warning("Failed to detect kind for URL, defaulting to track", exc_info=True)
            detected_kind = "track"
    else:
        detected_kind = kind

    return jsonify({"job_ids": job_ids, "kind": detected_kind}), 202


# ── Job page ─────────────────────────────────────────────────────────────
@bp.route("/jobs")
def jobs_page():
    """Render full download jobs page."""
    jobs = list_jobs(limit=100)
    return render_template("jobs.html", jobs=jobs)


# ── Job list (JSON) ──────────────────────────────────────────────────────
@bp.route("/jobs/data")
def jobs_json():
    """Return all jobs as JSON (status summary)."""
    auth_err = _require_auth()
    if auth_err:
        return auth_err

    limit = request.args.get("limit", 100, type=int)
    limit = max(1, min(limit, 500))
    jobs_list = list_jobs(limit=limit)
    return jsonify([j.to_dict() for j in jobs_list])


# ── Job list (HTML partial for HTMX) ────────────────────────────────────
@bp.route("/jobs/html")
def jobs_html():
    """Render jobs.html partial with job list (for HTMX polling)."""
    limit = request.args.get("limit", 20, type=int)
    limit = max(1, min(limit, 500))
    jobs_list = list_jobs(limit=limit)
    active_jobs = list_active_jobs()
    has_more = limit < 100 and len(jobs_list) >= limit
    return render_template(
        "partials/jobs_list.html",
        jobs=jobs_list,
        limit=limit,
        has_more=has_more,
        active_count=len(active_jobs),
    )


# ── SSE progress stream ─────────────────────────────────────────────────
@bp.route("/<job_id>/progress")
def progress(job_id: str):
    """SSE endpoint for real-time job progress."""
    auth_err = _require_auth()
    if auth_err:
        return auth_err

    job = get_job(job_id)
    if not job:
        return jsonify({"error": "job not found"}), 404

    # If already terminal, send a single event and close.
    if job.status in JobStatus.TERMINAL:
        def _closing():
            yield f"data: {json.dumps({'type': 'status', 'status': job.status, 'progress': job.progress})}\n\n"
        return Response(_closing(), mimetype="text/event-stream",
                        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    q = controller.subscribe(job_id)

    @stream_with_context
    def event_stream():
        try:
            while True:
                try:
                    event = q.get(timeout=30)
                    yield f"data: {json.dumps(event)}\n\n"
                    if event.get("type") in ("status", "error"):
                        if event.get("status") in JobStatus.TERMINAL or event.get("type") == "error":
                            break
                except queue.Empty:
                    yield ": heartbeat\n\n"
                except Exception:  # Catch-all: client disconnect, serialization, queue errors
                    logger.error("SSE stream error for job %s", job_id, exc_info=True)
                    break
        finally:
            controller.unsubscribe(job_id, q)

    return Response(event_stream, mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ── Cancel ───────────────────────────────────────────────────────────────
@bp.route("/<job_id>/cancel", methods=["POST"])
def cancel(job_id: str):
    """Cancel a download job."""
    auth_err = _require_auth()
    if auth_err:
        return auth_err

    ok = controller.cancel_download(job_id)
    return jsonify({"ok": ok})


# ── Status (JSON) ───────────────────────────────────────────────────────
@bp.route("/<job_id>/status")
def status(job_id: str):
    """Return JSON info for a single job."""
    auth_err = _require_auth()
    if auth_err:
        return auth_err

    job = get_job(job_id)
    if not job:
        return jsonify({"error": "job not found"}), 404
    return jsonify(job.to_dict())


# ── Batch actions ─────────────────────────────────────────────────────────
@bp.route("/jobs/retry-all-errored", methods=["POST"])
def retry_all_errored():
    """Reset all errored and cancelled jobs to queued."""
    auth_err = _require_auth()
    if auth_err:
        return auth_err
    jobs = list_jobs(limit=1000)
    for j in jobs:
        if j.status in (JobStatus.ERROR, JobStatus.CANCELLED):
            j.status = JobStatus.QUEUED
            j.error = ""
            save_job(j)
            controller._enqueue_id(j.id)
    jobs_list = list_jobs(limit=20)
    active_jobs = list_active_jobs()
    return render_template("partials/jobs_list.html", jobs=jobs_list, active_count=len(active_jobs))


@bp.route("/jobs/purge", methods=["POST"])
def purge_jobs():
    """Delete all completed jobs."""
    auth_err = _require_auth()
    if auth_err:
        return auth_err
    jobs = list_jobs(limit=1000)
    for j in jobs:
        if j.status == JobStatus.COMPLETED:
            delete_job(j.id)
    jobs_list = list_jobs(limit=20)
    active_jobs = list_active_jobs()
    return render_template("partials/jobs_list.html", jobs=jobs_list, active_count=len(active_jobs))


@bp.route("/jobs/delete-selected", methods=["POST"])
def delete_selected():
    """Delete selected jobs by ID."""
    auth_err = _require_auth()
    if auth_err:
        return auth_err
    data = request.get_json(force=True)
    ids = data.get("ids", [])
    if not isinstance(ids, list):
        return jsonify({"error": "ids must be a list"}), 400
    for job_id in ids:
        delete_job(job_id)
    return jsonify({"ok": True, "deleted": len(ids)})


@bp.route("/<job_id>/retry", methods=["POST"])
def retry_job_route(job_id):
    """Reset an errored, cancelled, or paused job back to queued."""
    auth_err = _require_auth()
    if auth_err:
        return auth_err
    if controller.retry_job(job_id):
        job = get_job(job_id)
        if job:
            return render_template("partials/job_row.html", job=job)
        return jsonify({"status": "ok"})
    return jsonify({"error": "Cannot retry job — not in a retryable state"}), 400


# ── Pause ─────────────────────────────────────────────────────────────────
@bp.route("/<job_id>/pause", methods=["POST"])
def pause_job(job_id: str):
    """Pause a queued or running job."""
    auth_err = _require_auth()
    if auth_err:
        return auth_err
    if controller.pause_job(job_id):
        job = get_job(job_id)
        if job:
            return render_template("partials/job_row.html", job=job)
        return jsonify({"status": "ok"})
    return jsonify({"error": "Cannot pause job"}), 400


# ── Resume ────────────────────────────────────────────────────────────────
@bp.route("/<job_id>/resume", methods=["POST"])
def resume_job(job_id: str):
    """Resume a paused job."""
    auth_err = _require_auth()
    if auth_err:
        return auth_err
    if controller.resume_job(job_id):
        job = get_job(job_id)
        if job:
            return render_template("partials/job_row.html", job=job)
        return jsonify({"status": "ok"})
    return jsonify({"error": "Cannot resume job"}), 400


# ── Priority up/down ─────────────────────────────────────────────────────
@bp.route("/<job_id>/priority/up", methods=["POST"])
def priority_up(job_id: str):
    """Move a queued job up one position."""
    auth_err = _require_auth()
    if auth_err:
        return auth_err
    if controller.priority_up(job_id):
        return jsonify({"status": "ok"})
    return jsonify({"error": "Cannot move job up — not in queue"}), 400


@bp.route("/<job_id>/priority/down", methods=["POST"])
def priority_down(job_id: str):
    """Move a queued job down one position."""
    auth_err = _require_auth()
    if auth_err:
        return auth_err
    if controller.priority_down(job_id):
        return jsonify({"status": "ok"})
    return jsonify({"error": "Cannot move job down — not in queue"}), 400


# ── Delete single ─────────────────────────────────────────────────────────
@bp.route("/<job_id>/delete", methods=["POST"])
def delete_job_route(job_id: str):
    """Delete a single job."""
    auth_err = _require_auth()
    if auth_err:
        return auth_err
    delete_job(job_id)
    return jsonify({"status": "ok"})


# ── Discography ───────────────────────────────────────────────────────────
@bp.route("/discography/resolve", methods=["POST"])
def discography_resolve():
    """Resolve an artist's discography from Spotify."""
    auth_err = _require_auth()
    if auth_err:
        return auth_err

    data = request.get_json(silent=True) or request.form
    artist_id = (data.get("artist_id") or "").strip()
    if not artist_id:
        return jsonify({"error": "artist_id is required"}), 400

    try:
        from app.download.discography import resolve_discography
        albums = resolve_discography(artist_id)
        return jsonify({"albums": [a.__dict__ for a in albums]})
    except Exception as exc:
        logger.error("Discography resolve failed: %s", exc)
        return jsonify({"error": str(exc)}), 500


@bp.route("/discography", methods=["POST"])
def discography_enqueue():
    """Enqueue all albums from a resolved discography.

    Accepts either ``artist_id`` (Spotify URI) or ``artist_name`` (plain text
    resolved server-side).  Options ``include_singles``, ``prefer_explicit``,
    and ``override_existing`` are accepted from both JSON body and query params.
    """
    auth_err = _require_auth()
    if auth_err:
        return auth_err

    data = request.get_json(silent=True) or request.form
    artist_id = (data.get("artist_id") or "").strip()
    artist_name = (data.get("artist_name") or "").strip()

    # Resolve artist_name → artist_id when no URI is provided.
    if not artist_id:
        if not artist_name:
            return jsonify({"error": "artist_id or artist_name is required"}), 400
        try:
            from app.spotify.resolver import resolve_artist_name_to_id
            artist_id = resolve_artist_name_to_id(artist_name)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 404
        except Exception as exc:
            logger.error("Artist name resolution failed: %s", exc)
            return jsonify({"error": f"Failed to resolve artist: {exc}"}), 500

    # Options: JSON body takes priority, then query params, then defaults.
    def _opt(key: str, default: bool) -> bool:
        if key in data:
            val = data.get(key)
            if isinstance(val, bool):
                return val
            return str(val).strip().lower() in ("1", "true", "yes")
        if request.args.get(key):
            return request.args.get(key).strip().lower() in ("1", "true", "yes")
        return default

    include_singles = _opt("include_singles", False)
    prefer_explicit = _opt("prefer_explicit", True)
    override_existing = _opt("override_existing", False)

    from app.download.discography import resolve_discography
    try:
        albums = resolve_discography(
            artist_id,
            include_singles=include_singles,
            prefer_explicit=prefer_explicit,
        )
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

    if not albums:
        return jsonify({"error": "No albums found"}), 404

    group_id = controller._make_group_id()
    resolved_artist = albums[0].artist_name if albums else artist_name or "Unknown"
    job_ids: list[str] = []

    for album in albums:
        album_url = f"https://open.spotify.com/album/{album.album_id}"
        try:
            from app.spotify.resolver import fetch_album_tracks
            tracks = fetch_album_tracks(album.album_id)
        except Exception:
            tracks = []

        if tracks:
            for t in tracks:
                track_url = f"https://open.spotify.com/track/{t['spotify_id']}"
                job = controller._create_track_job(track_url, t["spotify_id"], group_id)
                if override_existing:
                    job.override_existing = True
                    save_job(job)
                job_ids.append(job.id)
        else:
            # Enqueue the album as a single download
            job = Job(
                url=album_url,
                title=album.title,
                artist=resolved_artist,
                kind="album",
                group_id=group_id,
                override_existing=override_existing,
            )
            job.data["spotify_id"] = album.album_id
            save_job(job)
            job_ids.append(job.id)

    for jid in job_ids:
        controller._enqueue_id(jid)

    return jsonify({
        "job_ids": job_ids,
        "count": len(job_ids),
        "artist_name": resolved_artist,
        "artist_id": artist_id,
        "group_id": group_id,
    }), 202
