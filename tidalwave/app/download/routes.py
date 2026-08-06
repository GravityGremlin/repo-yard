"""Download routes — enqueue downloads, list jobs, SSE progress stream."""

from __future__ import annotations

import json
import logging
import queue
import uuid

from flask import Blueprint, Response, render_template, request, jsonify, stream_with_context

from app.models import append_audit, Job, JobStatus, save_job, get_job, list_jobs, list_active_jobs, delete_job
from app.download import controller
from app.download.discography import resolve_discography
from app.tidal.session import get_session

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
    session = get_session()
    if not session:
        return jsonify({"error": "Not connected to Tidal"}), 503

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

    if request.headers.get("HX-Request"):
        return render_template("partials/job_row.html", job=job)
    return jsonify({"job_id": job.id, "status": job.status})


@bp.route("/discography", methods=["POST"])
def enqueue_discography():
    """Queue a full artist discography — one Job per album with shared group_id."""
    if request.is_json:
        data = request.get_json()
    else:
        data = request.form

    artist_id = (data.get("artist_id") or "").strip()
    if not artist_id:
        return jsonify({"error": "artist_id is required"}), 400

    artist_name = (data.get("artist_name") or "").strip()
    include_singles = str(data.get("include_singles", "")).strip().lower() in ("1", "true", "on", "yes")
    override_existing = str(data.get("override_existing", "")).strip().lower() in ("1", "true", "on", "yes")

    # Get Tidal session
    session = get_session()
    if not session:
        return jsonify({"error": "Tidal not connected"}), 503

    # Resolve discography
    try:
        albums = resolve_discography(
            session=session,
            artist_id=artist_id,
            include_singles=include_singles,
            prefer_explicit=True,  # always on per plan
        )
    except Exception as exc:
        logger.error("Discography resolution failed for artist_id=%s: %s", artist_id, exc)
        return jsonify({"error": f"Discography resolution failed: {exc}"}), 500

    if not albums:
        logger.info("No albums found for artist_id=%s (include_singles=%s)", artist_id, include_singles)
        return jsonify({"message": "No albums found (may all be singles)", "count": 0}), 200

    # Create jobs with shared group_id
    group_id = str(uuid.uuid4())[:8]
    queued = 0

    for album in albums:
        job = Job(
            url=f"https://tidal.com/album/{album.album_id}",
            title=album.title,
            artist=album.artist_name,
            kind="album",
            proxy_index=0,
            override_existing=override_existing,
            group_id=group_id,
        )
        save_job(job)
        controller.enqueue(job.id)
        queued += 1

    logger.info("Queued discography: artist=%s count=%d group_id=%s", artist_name or artist_id, queued, group_id)

    # HTMX response
    if request.headers.get("HX-Request"):
        return render_template(
            "partials/discography_queued.html",
            artist_name=artist_name or artist_id,
            count=queued,
            group_id=group_id,
        )

    return jsonify({
        "queued": queued,
        "group_id": group_id,
        "artist": artist_name or artist_id,
    })


@bp.route("/jobs/<job_id>/cancel", methods=["POST"])
def cancel(job_id: str):
    """Cancel a download job."""
    ok = controller.cancel_job(job_id)
    if request.headers.get("HX-Request"):
        job = get_job(job_id)
        if not job:
            return "", 200
        return render_template("partials/job_row.html", job=job)
    return jsonify({"cancelled": ok})


@bp.route("/jobs/<job_id>")
def job_detail(job_id: str):
    """Get job detail as HTML partial."""
    job = get_job(job_id)
    if not job:
        return "Not found", 404
    return render_template("partials/job_row.html", job=job)


@bp.route("/jobs/<job_id>/delete", methods=["POST"])
def remove(job_id: str):
    """Delete a job from the DB."""
    delete_job(job_id)
    append_audit("job.deleted", job_id, {})
    if request.headers.get("HX-Request"):
        return ""
    return jsonify({"deleted": True})


@bp.route("/jobs/list")
def jobs_list_partial():
    """Return jobs list as HTMX partial (for polling/refresh).

    Defaults to the most recent 20 jobs to keep payload small; client can
    request more via ?limit=100.
    """
    limit = request.args.get("limit", 20, type=int)
    # Clamp to a sane range to avoid unbounded queries.
    limit = max(1, min(int(limit), 500))
    jobs = list_jobs(limit=limit)
    active_jobs = list_active_jobs()
    has_more = limit < 100 and len(jobs) >= limit
    return render_template(
        "partials/jobs_list.html",
        jobs=jobs,
        limit=limit,
        has_more=has_more,
        active_count=len(active_jobs),
    )

@bp.route("/jobs/<job_id>/retry", methods=["POST"])
def retry_job(job_id: str):
    """Retry a failed or cancelled job."""
    job = get_job(job_id)
    if not job:
        return jsonify({"error": "job not found"}), 404
    if job.status not in (JobStatus.ERROR, JobStatus.CANCELLED, JobStatus.PAUSED):
        return jsonify({"error": "job cannot be retried"}), 400
    job.status = JobStatus.QUEUED
    job.progress = 0.0
    job.error = ""
    save_job(job)
    controller.enqueue(job.id)
    if request.headers.get("HX-Request"):
        return render_template("partials/job_row.html", job=job)
    return jsonify({"retried": True})

@bp.route("/jobs/<job_id>/pause", methods=["POST"])
def pause_job(job_id: str):
    """Pause a queued or running job."""
    job = get_job(job_id)
    if not job:
        return jsonify({"error": "job not found"}), 404
    if job.status not in (JobStatus.QUEUED, JobStatus.RUNNING):
        return jsonify({"error": "job cannot be paused"}), 400
    if job.status == JobStatus.RUNNING:
        controller.cancel_job(job_id)
    job.status = JobStatus.PAUSED
    save_job(job)
    if request.headers.get("HX-Request"):
        return render_template("partials/job_row.html", job=job)
    return jsonify({"paused": True})

@bp.route("/jobs/<job_id>/resume", methods=["POST"])
def resume_job(job_id: str):
    """Resume a paused job."""
    job = get_job(job_id)
    if not job:
        return jsonify({"error": "job not found"}), 404
    if job.status != JobStatus.PAUSED:
        return jsonify({"error": "job is not paused"}), 400
    job.status = JobStatus.QUEUED
    job.progress = 0.0
    save_job(job)
    controller.enqueue(job.id)
    if request.headers.get("HX-Request"):
        return render_template("partials/job_row.html", job=job)
    return jsonify({"resumed": True})

@bp.route("/jobs/<job_id>/priority/up", methods=["POST"])
def priority_up(job_id: str):
    """Move a queued job up in priority (sooner)."""
    job = get_job(job_id)
    if not job or job.status != JobStatus.QUEUED:
        return jsonify({"error": "job not found or not queued"}), 404
    ok = controller.reorder_queue(job_id, "up")
    if request.headers.get("HX-Request"):
        job = get_job(job_id)
        return render_template("partials/job_row.html", job=job)
    return jsonify({"moved": ok})

@bp.route("/jobs/<job_id>/priority/down", methods=["POST"])
def priority_down(job_id: str):
    """Move a queued job down in priority (later)."""
    job = get_job(job_id)
    if not job or job.status != JobStatus.QUEUED:
        return jsonify({"error": "job not found or not queued"}), 404
    ok = controller.reorder_queue(job_id, "down")
    if request.headers.get("HX-Request"):
        job = get_job(job_id)
        return render_template("partials/job_row.html", job=job)
    return jsonify({"moved": ok})

@bp.route("/jobs/purge", methods=["POST"])
def purge_completed():
    """Delete all terminal jobs (paginated to cover all)."""
    from app.models import list_jobs, delete_job
    deleted = 0
    _BATCH = 1000
    _MAX = 20000
    total_seen = 0
    while total_seen < _MAX:
        jobs = list_jobs(limit=_BATCH)
        if not jobs:
            break
        total_seen += len(jobs)
        for job in jobs:
            if job.status in (JobStatus.COMPLETED, JobStatus.ERROR, JobStatus.CANCELLED):
                delete_job(job.id)
                deleted += 1
        if len(jobs) < _BATCH:
            break
    if request.headers.get("HX-Request"):
        return render_template("partials/jobs_list.html", jobs=list_jobs(limit=100))
    return jsonify({"deleted": deleted})

@bp.route("/jobs/delete-selected", methods=["POST"])
def delete_selected():
    """Delete multiple jobs by ID (batch delete).
    Accepts JSON `{ids: [...]}` or form `ids=id1,id2`. Only terminal jobs
    (completed/error/cancelled) are deleted; active jobs are skipped.
    """
    if request.is_json:
        payload = request.get_json() or {}
        raw = payload.get("ids") or []
        if isinstance(raw, str):
            ids = [s for s in raw.split(",") if s]
        else:
            ids = list(raw)
    else:
        ids = [s for s in (request.form.get("ids") or "").split(",") if s]
    ids = [s for s in ids if isinstance(s, str) and s]

    deleted = 0
    skipped_active = 0
    for job_id in ids:
        job = get_job(job_id)
        if not job:
            continue
        if job.status not in (JobStatus.COMPLETED, JobStatus.ERROR, JobStatus.CANCELLED):
            skipped_active += 1
            continue
        delete_job(job.id)
        append_audit("job.deleted", job.id, {"batch": True})
        deleted += 1

    if request.headers.get("HX-Request"):
        return render_template("partials/jobs_list.html", jobs=list_jobs(limit=50))
    return jsonify({"deleted": deleted, "skipped_active": skipped_active})


@bp.route("/jobs/retry-all-errored", methods=["POST"])
def retry_all_errored():
    """Retry every job with status ERROR or CANCELLED (paginated to cover all)."""
    retried = 0
    _BATCH = 1000
    _MAX = 20000
    total_seen = 0
    while total_seen < _MAX:
        jobs = list_jobs(limit=_BATCH)
        if not jobs:
            break
        total_seen += len(jobs)
        for job in jobs:
            if job.status in (JobStatus.ERROR, JobStatus.CANCELLED):
                job.status = JobStatus.QUEUED
                job.progress = 0.0
                job.error = ""
                save_job(job)
                controller.enqueue(job.id)
                retried += 1
        if len(jobs) < _BATCH:
            break
    if request.headers.get("HX-Request"):
        return render_template("partials/jobs_list.html", jobs=list_jobs(limit=100))
    return jsonify({"retried": retried})


@bp.route("/events/<job_id>")
def sse_events(job_id: str):
    """SSE endpoint for real-time job progress."""
    job = get_job(job_id)
    if not job:
        return jsonify({"error": "job not found"}), 404

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
                except Exception:
                    logger.error("SSE stream error for job %s", job_id, exc_info=True)
                    break
        finally:
            controller.unsubscribe(job_id, q)

    return Response(event_stream, mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
