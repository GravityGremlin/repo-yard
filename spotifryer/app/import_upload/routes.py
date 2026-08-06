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
    enqueue_import,
    subscribe_import_job,
    unsubscribe_import_job,
    cancel_import_job,
)
from app.security import safe_resolve
from app.spotify.session import is_authenticated

logger = logging.getLogger(__name__)

import_upload_bp = Blueprint("import_upload", __name__, url_prefix="/import")


def _require_auth():
    """Return None if authenticated, otherwise a 401 JSON response."""
    if not is_authenticated():
        return jsonify({"error": "not_authenticated"}), 401
    return None


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


@import_upload_bp.route("/")
def upload_page():
    """Render the upload form."""
    auth_err = _require_auth()
    if auth_err:
        return auth_err
    return render_template(
        "upload.html",
        version=__version__,
        max_upload_mb=IMPORT_MAX_UPLOAD_MB,
        allowed_exts=IMPORT_ALLOWED_EXTS,
        archive_exts=IMPORT_ARCHIVE_EXTS,
    )


@import_upload_bp.route("/upload", methods=["POST"])
def upload():
    """Accept uploaded files, create an import job, and enqueue it.

    1. Validate content length against ``IMPORT_MAX_UPLOAD_MB``.
    2. Classify files as archives or audio — reject mixed or unsupported.
    3. Persist the job and files to the staging directory.
    4. Enqueue the job for background processing.
    """
    auth_err = _require_auth()
    if auth_err:
        return auth_err
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
            # Sanitize filename via security helper
            safe_name = safe_resolve(staging, name)
            if safe_name is None:
                logger.warning("Rejected unsafe filename: %s", name)
                name = uuid.uuid4().hex[:12] + Path(name).suffix
                safe_name = staging / name
            f.save(str(safe_name))
            file_count += 1
            total_size += safe_name.stat().st_size
    except Exception:
        logger.exception("Failed to save uploaded files for job %s", job.id)
        _cleanup_upload(job.id, staging.parent)
        return jsonify({"error": "Failed to save uploaded files"}), 500

    # ── Enqueue ───────────────────────────────────────────────────
    try:
        enqueue_import(job.id)
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
    return jsonify({"job_id": job.id, "filename": title})


def _cleanup_upload(job_id: str, staging_dir: Path) -> None:
    """Remove staging directory and job record after a failed upload."""
    import shutil
    shutil.rmtree(staging_dir, ignore_errors=True)
    try:
        delete_job(job_id)
    except Exception:
        logger.warning("Could not delete job %s during cleanup", job_id)


# ── Job listing routes ─────────────────────────────────────────────


@import_upload_bp.route("/jobs")
def jobs_json():
    """Return JSON list of import jobs."""
    auth_err = _require_auth()
    if auth_err:
        return auth_err
    jobs = list_jobs(limit=100)
    return jsonify([j.to_dict() for j in jobs])


@import_upload_bp.route("/jobs/html")
def jobs_html():
    """HTMX partial — returns HTML fragment of the import jobs table."""
    auth_err = _require_auth()
    if auth_err:
        return auth_err
    jobs = list_jobs(limit=100)
    return render_template("partials/import_jobs_list.html", jobs=jobs)


# ── SSE progress stream ─────────────────────────────────────────────


@import_upload_bp.route("/<job_id>/progress")
def import_progress(job_id: str):
    """SSE endpoint for import progress events.

    Streams JSON events until the job reaches a terminal state or the
    client disconnects.
    """
    auth_err = _require_auth()
    if auth_err:
        return auth_err
    sub_q = subscribe_import_job(job_id)

    def generate():
        try:
            while True:
                try:
                    event = sub_q.get(timeout=30)
                except queue.Empty:
                    # Send keepalive
                    yield ": keepalive\n\n"
                    continue

                event_type = event.get("type", "message")
                yield f"event: {event_type}\ndata: {json.dumps(event)}\n\n"

                # Stop streaming on terminal events
                if event_type in ("result", "error") or event.get("status") in JobStatus.TERMINAL:
                    break
        finally:
            unsubscribe_import_job(job_id, sub_q)

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ── Job action routes ──────────────────────────────────────────────


@import_upload_bp.route("/<job_id>/cancel", methods=["POST"])
def cancel(job_id: str):
    """Cancel a queued or running import job."""
    auth_err = _require_auth()
    if auth_err:
        return auth_err
    ok = cancel_import_job(job_id)
    if not ok:
        return jsonify({"error": "Job not found or already terminal"}), 404
    return jsonify({"ok": True})
