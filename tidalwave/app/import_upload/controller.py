"""Import upload controller — background worker with SSE progress tracking."""

from __future__ import annotations

import logging
import queue
import shutil
import threading
from pathlib import Path

from app.config import IMPORT_STAGING_DIR, IMPORT_ARCHIVE_EXTS, NAVIDROME_URL, NAVIDROME_AUTO_SCAN
from app.models import JobStatus, append_audit, get_job, list_active_jobs, save_job, delete_job
from app.import_upload.extract import extract_archive
from app.download.beets_integration import beets_import_upload

logger = logging.getLogger(__name__)

# FIFO queue via list + lock
_queued_order: list[str] = []
_priority_lock = threading.Lock()

_queue_event = threading.Event()

_subscribers: dict[str, list[queue.Queue]] = {}
_sub_lock = threading.Lock()

_cancel_events: dict[str, threading.Event] = {}
_cancel_lock = threading.Lock()

_started = False

_MAX_CONCURRENT_IMPORTS = 1  # only 1 import at a time (memory constraint)


# ── Public API ─────────────────────────────────────────────────────


def start_worker() -> None:
    """Start the import worker thread (idempotent). Called from factory.py."""
    global _started
    if _started:
        return
    _started = True
    for _ in range(_MAX_CONCURRENT_IMPORTS):
        t = threading.Thread(target=_worker_loop, daemon=True, name="import-worker")
        t.start()
    logger.info("Started %d import worker", _MAX_CONCURRENT_IMPORTS)


def enqueue(job_id: str) -> None:
    """Add a job to the import queue (FIFO), create cancel event."""
    with _cancel_lock:
        _cancel_events[job_id] = threading.Event()
    with _priority_lock:
        _queued_order.append(job_id)
    logger.info("Import job %s enqueued (FIFO)", job_id)
    _queue_event.set()


def subscribe(job_id: str) -> queue.Queue:
    """Subscribe to SSE events for a job. Returns a Queue to read from."""
    q: queue.Queue = queue.Queue()
    with _sub_lock:
        _subscribers.setdefault(job_id, []).append(q)
    return q


def unsubscribe(job_id: str, q: queue.Queue) -> None:
    """Unsubscribe from SSE events."""
    with _sub_lock:
        subs = _subscribers.get(job_id, [])
        if q in subs:
            subs.remove(q)
        if not subs:
            _subscribers.pop(job_id, None)


def cancel_job(job_id: str) -> bool:
    """Signal cancel for a job. Returns True if job was found and cancellable."""
    job = get_job(job_id)
    if not job or job.status in JobStatus.TERMINAL:
        return False
    # Remove from queue if still queued
    with _priority_lock:
        try:
            _queued_order.remove(job_id)
        except ValueError:
            pass  # not in queue (running or already processed)
    with _cancel_lock:
        evt = _cancel_events.setdefault(job_id, threading.Event())
    evt.set()
    job.status = JobStatus.CANCELLED
    save_job(job)
    _emit(job_id, {"type": "status", "status": job.status})
    append_audit("import.cancelled", job_id, {})
    return True


def retry_job(job_id: str) -> bool:
    """Reset job to QUEUED and re-enqueue. Returns True if successful."""
    job = get_job(job_id)
    if not job:
        return False
    if job.status not in (JobStatus.ERROR, JobStatus.CANCELLED):
        return False
    job.status = JobStatus.QUEUED
    job.error = ""
    save_job(job)
    enqueue(job_id)
    return True


def delete_import_job(job_id: str) -> bool:
    """Delete job and cleanup staging dir. Returns True if successful."""
    job = get_job(job_id)
    if not job:
        return False
    # Cleanup staging directory
    staging_dir = IMPORT_STAGING_DIR / job_id
    shutil.rmtree(staging_dir, ignore_errors=True)
    delete_job(job_id)
    logger.info("Import job %s deleted and staging cleaned", job_id)
    return True


def recover_interrupted_jobs() -> None:
    """On startup, re-queue any RUNNING jobs from before a restart."""
    requeued = 0
    for job in list_active_jobs():
        if job.status == JobStatus.RUNNING:
            job.status = JobStatus.QUEUED
            save_job(job)
            logger.info("Recovering interrupted import job %s", job.id)
        if job.id not in _queued_order:
            _queued_order.append(job.id)
            requeued += 1
    if requeued:
        logger.info("Recovered %d active import jobs into the queue", requeued)


# ── Internal helpers ───────────────────────────────────────────────


def _emit(job_id: str, event: dict) -> None:
    """Send an event dict to all SSE subscribers for *job_id*."""
    with _sub_lock:
        subs = _subscribers.get(job_id, [])
        for q in subs:
            try:
                q.put_nowait(event)
            except queue.Full:
                pass


def _clear_cancel(job_id: str) -> threading.Event | None:
    """Return the cancel event for *job_id*, or None."""
    with _cancel_lock:
        return _cancel_events.get(job_id)


def _is_archive(path: Path) -> bool:
    """Return True if *path* has an archive extension."""
    return path.suffix.lower() in IMPORT_ARCHIVE_EXTS or path.name.lower().endswith(".tar.gz")


# ── Worker loop ────────────────────────────────────────────────────


def _worker_loop() -> None:
    while True:
        job_id = None
        with _priority_lock:
            if _queued_order:
                job_id = _queued_order.pop(0)
            else:
                # Clear inside the lock so idle workers actually block on
                # wait() instead of spinning on a permanently-set event.
                _queue_event.clear()
        if job_id is None:
            _queue_event.wait(timeout=0.5)
            continue
        try:
            _run_import(job_id)
        except Exception as exc:
            logger.error("Import worker error for job %s: %s", job_id, exc)
        finally:
            with _cancel_lock:
                _cancel_events.pop(job_id, None)


# ── Main import execution ──────────────────────────────────────────


def _run_import(job_id: str) -> None:
    """Execute a single import job — extract archives, beets import, SSE events."""
    job = get_job(job_id)
    if not job:
        logger.warning("Import job %s not found in DB", job_id)
        return

    cancel_event = _clear_cancel(job_id)

    # ── Early cancel check ────────────────────────────────────────
    if cancel_event is not None and cancel_event.is_set():
        job.status = JobStatus.CANCELLED
        save_job(job)
        _emit(job_id, {"type": "status", "status": "cancelled"})
        return

    # ── Mark running ──────────────────────────────────────────────
    job.status = JobStatus.RUNNING
    save_job(job)
    _emit(job_id, {"type": "status", "status": "running"})
    append_audit("import.started", job_id, {})

    staging_dir = IMPORT_STAGING_DIR / job_id
    uploaded_dir = staging_dir / "uploaded"
    extracted_dir = staging_dir / "extracted"
    extracted_dir.mkdir(parents=True, exist_ok=True)

    # ── Phase 1: Extract / copy audio files ───────────────────────
    try:
        if cancel_event is not None and cancel_event.is_set():
            raise _Cancelled("cancelled during extraction")

        uploaded_files: list[Path] = []
        if uploaded_dir.is_dir():
            uploaded_files = sorted(uploaded_dir.iterdir())

        if not uploaded_files:
            logger.warning("Import job %s: no uploaded files found in %s", job_id, uploaded_dir)
            job.status = JobStatus.ERROR
            job.error = "No uploaded files found"
            save_job(job)
            _emit(job_id, {"type": "status", "status": "error"})
            _emit(job_id, {"type": "error", "error": job.error})
            append_audit("import.error", job_id, {"error": job.error})
            return

        audio_files: list[Path] = []
        for f in uploaded_files:
            if _is_archive(f):
                _emit(job_id, {"type": "log", "line": f"Extracting {f.name}..."})
                try:
                    extracted = extract_archive(f, extracted_dir)
                    audio_files.extend(extracted)
                except ValueError as exc:
                    job.status = JobStatus.ERROR
                    job.error = str(exc)
                    save_job(job)
                    _emit(job_id, {"type": "status", "status": "error"})
                    _emit(job_id, {"type": "error", "error": job.error})
                    append_audit("import.error", job_id, {"error": job.error})
                    return
            else:
                # Assume audio file — copy to extracted dir
                dest = extracted_dir / f.name
                try:
                    shutil.copy2(f, dest)
                    audio_files.append(dest)
                except OSError as exc:
                    job.status = JobStatus.ERROR
                    job.error = f"Failed to copy {f.name}: {exc}"
                    save_job(job)
                    _emit(job_id, {"type": "status", "status": "error"})
                    _emit(job_id, {"type": "error", "error": job.error})
                    append_audit("import.error", job_id, {"error": job.error})
                    return

        # ── Validate audio files ─────────────────────────────────
        if not audio_files:
            job.status = JobStatus.ERROR
            job.error = "No audio files found after extraction"
            save_job(job)
            _emit(job_id, {"type": "status", "status": "error"})
            _emit(job_id, {"type": "error", "error": job.error})
            append_audit("import.error", job_id, {"error": job.error})
            return

    except _Cancelled:
        job.status = JobStatus.CANCELLED
        save_job(job)
        _emit(job_id, {"type": "status", "status": "cancelled"})
        append_audit("import.cancelled", job_id, {})
        shutil.rmtree(staging_dir, ignore_errors=True)
        return

    # ── Phase 2: Beets import ─────────────────────────────────----
    _emit(job_id, {"type": "status", "status": "importing"})

    result = beets_import_upload(
        staging_dir=extracted_dir,
        cancel_event=cancel_event,
        progress_callback=lambda line: _emit(job_id, {"type": "log", "line": line}),
    )

    # ── Phase 3: Handle result ────────────────────────────────────

    # Check if cancelled during beets import
    if cancel_event is not None and cancel_event.is_set():
        job.status = JobStatus.CANCELLED
        save_job(job)
        _emit(job_id, {"type": "status", "status": "cancelled"})
        append_audit("import.cancelled", job_id, {})
        shutil.rmtree(staging_dir, ignore_errors=True)
        return

    if not result.ok:
        job.status = JobStatus.ERROR
        job.error = result.error or "Import failed"
        save_job(job)
        _emit(job_id, {"type": "status", "status": "error"})
        _emit(job_id, {"type": "error", "error": job.error})
        append_audit("import.error", job_id, {"error": job.error})
        # Do NOT cleanup staging — preserve for retry
        return

    # ── Success ───────────────────────────────────────────────────
    job.status = JobStatus.COMPLETED
    job.files = [str(p) for p in result.imported_files]
    save_job(job)

    _emit(job_id, {"type": "result", "ok": True, "files": job.files})

    # Trigger Navidrome library scan (fire-and-forget)
    if NAVIDROME_AUTO_SCAN:
        threading.Thread(target=_trigger_navidrome_scan, daemon=True).start()

    append_audit("import.completed", job_id, {"files": len(result.imported_files)})

    # Cleanup staging
    shutil.rmtree(staging_dir, ignore_errors=True)


def _trigger_navidrome_scan() -> None:
    """Trigger a quick Navidrome scan (non-critical, background thread)."""
    try:
        import requests
        resp = requests.post(f"{NAVIDROME_URL}/api/scan?full=false", timeout=5)
        if resp.ok:
            logger.info("Navidrome scan triggered")
    except Exception as exc:
        logger.warning("Navidrome scan failed (non-critical): %s", exc)


class _Cancelled(Exception):
    """Internal exception to signal cancellation during extraction phase."""
