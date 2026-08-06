"""Import upload routes — file upload, job list, SSE progress stream."""

from __future__ import annotations

import json
import logging
import queue
import uuid
from pathlib import Path

from flask import Blueprint, Response, render_template, request, jsonify, stream_with_context

from app.config import (
    IMPORT_MAX_UPLOAD_MB,
    IMPORT_ALLOWED_EXTS,
    IMPORT_ARCHIVE_EXTS,
    IMPORT_STAGING_DIR,
    __version__,
)
from app.models import Job, JobStatus, list_jobs, save_job, delete_job
from app.import_upload.controller import (
    enqueue,
    subscribe,
    unsubscribe,
    cancel_job,
    retry_job,
    delete_import_job,
)
from app.tidal.session import token_exists

logger = logging.getLogger(__name__)

bp = Blueprint("import_upload", __name__, url_prefix="/import")


# ── Helpers ─────────────────────────────────────────────────────────


def _extension_matches(filename: str, extensions: list[str]) -> bool:
    """Check if *filename* ends with any of the given extensions (case-insensitive).

    Extensions are tested longest-first so that multi-dot suffixes like
    ``.tar.gz`` are matched before shorter suffixes like ``.gz``.
    """
    lower = filename.lower()
    for ext in sorted(extensions, key=len, reverse=True):
        if lower.endswith(ext.lower()):
            return True
    return False


def _classify_uploads(
    files: list,
) -> tuple[list, list]:
    """Separate *files* into (archives, audio_files).

    Each element is a Werkzeug ``FileStorage``.
    """
    archives: list = []
    audio_files: list = []
    for f in files:
        if _extension_matches(f.filename or "", IMPORT_ARCHIVE_EXTS):
            archives.append(f)
        elif _extension_matches(f.filename or "", IMPORT_ALLOWED_EXTS):
            audio_files.append(f)
    return archives, audio_files


# ── Page routes ─────────────────────────────────────────────────────


@bp.route("/")
def upload_page():
    """Render the upload form."""
    return render_template(
        "upload.html",
        connected=token_exists(),
        version=__version__,
    )


@bp.route("/upload", methods=["POST"])
def upload():
    """Accept uploaded files, create an import job, and enqueue it.

    1. Validate content length against ``IMPORT_MAX_UPLOAD_MB``.
    2. Classify files as archives or audio — reject mixed or unsupported.
    3. Persist the job and files to the staging directory.
    4. Enqueue the job for background processing.
    """
    # ── Size check ────────────────────────────────────────────────
    cl = request.content_length
    max_bytes = IMPORT_MAX_UPLOAD_MB * 1024 * 1024
    if cl is not None and cl > max_bytes:
        return jsonify({"error": f"Upload too large ({cl} bytes)"}), 400

    # ── Gather files ──────────────────────────────────────────────
    uploaded = request.files.getlist("files")
    if not uploaded:
        return jsonify({"error": "No files uploaded"}), 400

    archives, audio_files = _classify_uploads(uploaded)
    if archives and audio_files:
        return jsonify({"error": "Upload either audio files or archives, not both"}), 400
    if not archives and not audio_files:
        return jsonify({"error": "No supported files found"}), 400

    # ── Create job record ─────────────────────────────────────────
    title: str
    if len(uploaded) == 1:
        title = (uploaded[0].filename or "untitled")[:100]
    else:
        title = f"{len(uploaded)} files"

    job = Job(
        kind="import",
        status=JobStatus.QUEUED,
        title=title,
        artist="",
        url="",
        files=[],
    )
    save_job(job)

    # ── Persist uploaded files ────────────────────────────────────
    staging = IMPORT_STAGING_DIR / job.id / "uploaded"
    file_count = 0
    total_size = 0
    try:
        staging.mkdir(parents=True, exist_ok=True)
        for f in uploaded:
            name = f.filename or uuid.uuid4().hex[:12]
            dest = staging / name
            f.save(str(dest))
            file_count += 1
            total_size += dest.stat().st_size
    except Exception:
        logger.exception("Failed to save uploaded files for job %s", job.id)
        # Cleanup on failure
        _cleanup_upload(job.id, staging.parent)
        return jsonify({"error": "Failed to save uploaded files"}), 500

    # ── Enqueue ───────────────────────────────────────────────────
    try:
        enqueue(job.id)
    except Exception:
        logger.exception("Failed to enqueue import job %s", job.id)
        _cleanup_upload(job.id, staging.parent)
        return jsonify({"error": "Failed to enqueue job"}), 500

    logger.info(
        "Import job %s created: %d files, %d bytes",
        job.id,
        file_count,
        total_size,
    )
    return jsonify({"job_id": job.id, "files": file_count})


def _cleanup_upload(job_id: str, staging_dir: Path) -> None:
    """Remove staging directory and job record after a failed upload."""
    import shutil

    shutil.rmtree(staging_dir, ignore_errors=True)
    try:
        delete_job(job_id)
    except Exception:
        logger.warning("Could not delete job %s during cleanup", job_id)


# ── Job listing routes ─────────────────────────────────────────────


@bp.route("/jobs")
def jobs_page():
    """Import jobs list page."""
    jobs = list_jobs(limit=100)
    return render_template(
        "import_jobs.html",
        jobs=jobs,
        connected=token_exists(),
        version=__version__,
    )


@bp.route("/jobs/list")
def jobs_list_partial():
    """HTMX partial — returns HTML fragment of the jobs table.

    Intended for auto-refresh every 10 s on the import jobs page.
    """
    jobs = list_jobs(limit=100)
    return render_template("partials/import_jobs_list.html", jobs=jobs)


# ── Job action routes ──────────────────────────────────────────────


@bp.route("/jobs/<job_id>/cancel", methods=["POST"])
def cancel(job_id: str):
    """Cancel a queued or running import job."""
    ok = cancel_job(job_id)
    if not ok:
        return jsonify({"error": "Job not found or already terminal"}), 404
    return jsonify({"status": "cancelled"})


@bp.route("/jobs/<job_id>/retry", methods=["POST"])
def retry(job_id: str):
    """Retry a failed or cancelled import job."""
    ok = retry_job(job_id)
    if not ok:
        return jsonify({"error": "Job cannot be retried"}), 400
    return jsonify({"status": "queued"})


@bp.route("/jobs/<job_id>/delete", methods=["POST"])
def delete(job_id: str):
    """Delete an import job and its staging directory."""
    ok = delete_import_job(job_id)
    if not ok:
        return jsonify({"error": "Job not found"}), 404
    return jsonify({"status": "deleted"})


# ── SSE progress stream ────────────────────────────────────────────


@bp.route("/stream/<job_id>")
def sse_stream(job_id: str):
    """SSE endpoint for real-time import job progress.

    Returns a ``text/event-stream`` response.  The client receives JSON
    events as ``data: {...}\n\n``.  A heartbeat is sent every 30 s to
    keep the connection alive.
    """
    from app.models import get_job

    job = get_job(job_id)
    if not job:
        return jsonify({"error": "job not found"}), 404

    # If the job is already terminal, send final state and close.
    if job.status in JobStatus.TERMINAL:
        return _terminal_sse_response(job)

    q = subscribe(job_id)

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
            unsubscribe(job_id, q)

    return Response(
        event_stream,
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _terminal_sse_response(job) -> Response:
    """Build a short-lived SSE response for a job already in terminal state."""

    data = json.dumps({
        "type": "status",
        "status": job.status,
        "progress": job.progress,
    })

    def generate():
        yield f"data: {data}\n\n"

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
