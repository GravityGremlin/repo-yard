"""Download routes — enqueue downloads, list jobs, SSE progress stream."""

from __future__ import annotations

import json
import logging
import queue
import uuid

from flask import Blueprint, Response, render_template, request, jsonify, stream_with_context

from app.models import Job, JobStatus, save_job, get_job, list_jobs, delete_job
from app.download import controller
from app.download.discography import resolve_discography
from app.qobuz.session import get_session

logger = logging.getLogger(__name__)

bp = Blueprint("download", __name__, url_prefix="/download")


@bp.route("/jobs")
def jobs_page():
    """Show all download jobs."""
    jobs = list_jobs(limit=100)
    return render_template("jobs.html", jobs=jobs)


@bp.route("/enqueue", methods=["POST"])
def enqueue():
    """Queue a download job. Accepts JSON or form data."""
    if request.is_json:
        data = request.get_json()
    else:
        data = request.form

    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"error": "URL is required"}), 400

    title = data.get("title", "")
    artist = data.get("artist", "")
    kind = data.get("type", "track")  # track | album | playlist
    try:
        proxy_index = int(data.get("proxy_index", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid proxy_index"}), 400

    override_existing = str(data.get("override_existing", "")).strip().lower() in ("1", "true", "on", "yes")

    job = Job(url=url, title=title, artist=artist, kind=kind, proxy_index=proxy_index,
              override_existing=override_existing)
    save_job(job)
    controller.enqueue(job.id)

    return jsonify({"job_id": job.id, "status": "queued"})


@bp.route("/discography", methods=["POST"])
def enqueue_discography():
    """Queue a full artist discography — one Job per album with shared group_id.

    Accepts either ``artist_id`` (Qobuz ID) or ``artist_name`` (resolved to an
    ID server-side). Options ``include_singles``, ``prefer_explicit``, and
    ``override_existing`` come from JSON body or query params.
    """
    session = get_session()
    if not session:
        return jsonify({"error": "Qobuz not connected"}), 503

    if request.is_json:
        data = request.get_json()
    else:
        data = request.form

    artist_id = (data.get("artist_id") or "").strip()
    artist_name = (data.get("artist_name") or "").strip()

    # Resolve artist_name → Qobuz artist ID when no ID was supplied.
    if not artist_id:
        if not artist_name:
            return jsonify({"error": "artist_id or artist_name is required"}), 400
        try:
            search = session.search_music(artist_name, "ARTIST", limit=5)
            items = search.get("data") or []
        except Exception as exc:
            logger.error("Artist search failed for %s: %s", artist_name, exc)
            return jsonify({"error": f"Artist search failed: {exc}"}), 502
        if not items:
            return jsonify({"error": f"Artist not found: {artist_name}"}), 404
        artist_id = str(items[0].get("ART_ID") or items[0].get("id") or "")

    include_singles = str(data.get("include_singles", "")).strip().lower() in ("1", "true", "on", "yes")
    prefer_explicit = str(data.get("prefer_explicit", "true")).strip().lower() in ("1", "true", "on", "yes")
    override_existing = str(data.get("override_existing", "")).strip().lower() in ("1", "true", "on", "yes")

    try:
        albums = resolve_discography(
            session=session,
            artist_id=artist_id,
            include_singles=include_singles,
            prefer_explicit=prefer_explicit,
        )
    except Exception as exc:
        logger.error("Discography resolution failed for artist_id=%s: %s", artist_id, exc)
        return jsonify({"error": f"Discography resolution failed: {exc}"}), 500

    if not albums:
        logger.info("No albums found for artist_id=%s (include_singles=%s)", artist_id, include_singles)
        return jsonify({"message": "No albums found (may all be singles)", "count": 0}), 200

    group_id = uuid.uuid4().hex[:8]
    queued = 0
    # qoochie's downloader resolves TRACK URLs only, so a discography is
    # queued one track job per album track (same pattern as /playlist/download-all).
    for album in albums:
        try:
            tracks = session.get_album_tracks(album.album_id)
        except Exception as exc:
            logger.warning("Album tracks fetch failed for %s: %s", album.album_id, exc)
            tracks = []
        for track in tracks:
            # Embedded album tracks carry a numeric "id" (SNG_ID is absent).
            track_id = track.get("id") or track.get("SNG_ID") or ""
            if not track_id:
                continue
            job = Job(
                url=f"https://www.qobuz.com/track/{track_id}",
                title=track.get("title") or track.get("SNG_TITLE") or album.title,
                artist=track.get("artist", {}).get("name") if isinstance(track.get("artist"), dict) else (track.get("ART_NAME") or album.artist_name),
                kind="track",
                proxy_index=0,
                override_existing=override_existing,
                group_id=group_id,
            )
            save_job(job)
            controller.enqueue(job.id)
            queued += 1

    logger.info("Queued discography: artist=%s tracks=%d group_id=%s", artist_name or artist_id, queued, group_id)

    if request.headers.get("HX-Request"):
        return render_template("partials/discography_queued.html",
                               artist_name=artist_name or artist_id,
                               count=queued, group_id=group_id)

    return jsonify({"queued": queued, "group_id": group_id, "artist": artist_name or artist_id})


@bp.route("/jobs/list")
def jobs_list():
    """HTMX partial — list of all jobs."""
    jobs = list_jobs(limit=100)
    return render_template("partials/jobs_list.html", jobs=jobs)


@bp.route("/jobs/<job_id>")
def job_detail(job_id: str):
    """Single job detail."""
    job = get_job(job_id)
    if not job:
        return render_template("partials/error.html", message="Job not found"), 404
    return render_template("partials/job_row.html", job=job)


@bp.route("/jobs/<job_id>/cancel", methods=["POST"])
def cancel(job_id: str):
    """Cancel a job."""
    if controller.cancel_job(job_id):
        return jsonify({"status": "ok"})
    return jsonify({"error": "Cannot cancel job"}), 400


@bp.route("/jobs/<job_id>/retry", methods=["POST"])
def retry(job_id: str):
    """Retry a failed or cancelled job."""
    job = get_job(job_id)
    if not job:
        return jsonify({"error": "job not found"}), 404
    if job.status not in (JobStatus.ERROR, JobStatus.CANCELLED, JobStatus.PAUSED):
        return jsonify({"error": "job cannot be retried"}), 400
    controller.retry_job(job_id)
    job = get_job(job_id)
    if request.headers.get("HX-Request"):
        return render_template("partials/job_row.html", job=job)
    return jsonify({"retried": True})


@bp.route("/jobs/<job_id>/pause", methods=["POST"])
def pause(job_id: str):
    """Pause a queued or running job."""
    job = get_job(job_id)
    if not job:
        return jsonify({"error": "job not found"}), 404
    if job.status not in (JobStatus.QUEUED, JobStatus.RUNNING):
        return jsonify({"error": "job cannot be paused"}), 400
    controller.pause_job(job_id)
    job = get_job(job_id)
    if request.headers.get("HX-Request"):
        return render_template("partials/job_row.html", job=job)
    return jsonify({"paused": True})


@bp.route("/jobs/<job_id>/resume", methods=["POST"])
def resume(job_id: str):
    """Resume a paused job."""
    job = get_job(job_id)
    if not job:
        return jsonify({"error": "job not found"}), 404
    if job.status != JobStatus.PAUSED:
        return jsonify({"error": "job is not paused"}), 400
    controller.resume_job(job_id)
    job = get_job(job_id)
    if request.headers.get("HX-Request"):
        return render_template("partials/job_row.html", job=job)
    return jsonify({"resumed": True})


@bp.route("/jobs/<job_id>/delete", methods=["POST"])
def delete(job_id: str):
    """Delete a single job."""
    delete_job(job_id)
    return jsonify({"status": "ok"})


@bp.route("/jobs/delete-selected", methods=["POST"])
def delete_selected():
    """Delete multiple selected jobs."""
    data = request.get_json(silent=True) or {}
    ids = data.get("ids", [])
    for jid in ids:
        delete_job(jid)
    return jsonify({"status": "ok", "deleted": len(ids)})


@bp.route("/jobs/purge", methods=["POST"])
def purge():
    """Delete all terminal jobs."""
    deleted = controller.purge_terminal()
    if request.headers.get("HX-Request"):
        return render_template("partials/jobs_list.html", jobs=list_jobs(limit=100))
    return jsonify({"deleted": deleted})


@bp.route("/jobs/retry-all-errored", methods=["POST"])
def retry_all():
    """Retry all errored/cancelled jobs."""
    retried = controller.retry_all_errored()
    if request.headers.get("HX-Request"):
        return render_template("partials/jobs_list.html", jobs=list_jobs(limit=100))
    return jsonify({"retried": retried})


@bp.route("/events/<job_id>")
def stream(job_id: str):
    """SSE stream for real-time job progress."""
    q = controller.subscribe(job_id)

    def generate():
        try:
            while True:
                try:
                    event = q.get(timeout=30)
                except queue.Empty:
                    # Send heartbeat
                    yield ": heartbeat\n\n"
                    continue
                if event is None:
                    break
                yield f"data: {json.dumps(event)}\n\n"
                if event.get("type") == "status" and event.get("status") in (
                    JobStatus.COMPLETED, JobStatus.ERROR, JobStatus.CANCELLED
                ):
                    break
        finally:
            controller.unsubscribe(job_id, q)

    return Response(stream_with_context(generate()),
                    mimetype="text/event-stream")


@bp.route("/discography/resolve", methods=["POST"])
def discography_resolve():
    """Resolve an artist's discography."""
    data = request.get_json(silent=True) or request.form
    artist_id = (data.get("artist_id") or "").strip()
    if not artist_id:
        return jsonify({"error": "artist_id is required"}), 400

    session = get_session()
    if not session:
        return jsonify({"error": "Not connected to Qobuz"}), 503

    try:
        include_singles = str(data.get("include_singles", "")).lower() in ("1", "true", "on")
        albums = resolve_discography(session, artist_id, include_singles=include_singles)
        return jsonify({"albums": [a.__dict__ for a in albums]})
    except NotImplementedError as exc:
        return jsonify({"error": str(exc)}), 501
    except Exception as exc:
        logger.error("Discography resolve failed: %s", exc)
        return jsonify({"error": str(exc)}), 500
