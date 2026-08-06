"""Download controller — background thread runner with progress tracking."""

from __future__ import annotations

import queue
import logging
import re
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
import json
import os
import errno
from app.config import DOWNLOAD_DIR, JOBS_DIR, LIBRARY_DIR, MAX_CONCURRENT, PLAYLIST_DIR, PROMOTE_EXISTS, PROMOTE_TO_LIBRARY  # noqa: F401 (re-exported; tests monkeypatch controller.DOWNLOAD_DIR)
from app.models import JobStatus, append_audit, get_job, list_active_jobs, save_job
from app.proxy import get_proxy_url
from app.qobuz.downloader import DownloadCancelled, QobuzDownloader
from app.qobuz.session import init_session

logger = logging.getLogger(__name__)

# Priority queue: LIFO (newest first) via list + lock
_queued_order: list[str] = []
_priority_lock = threading.Lock()

_active_count = 0
_active_lock = threading.Lock()
_started = False
_queue_event = threading.Event()


_subscribers: dict[str, list[queue.Queue]] = {}
_sub_lock = threading.Lock()

_cancel_events: dict[str, threading.Event] = {}
_cancel_lock = threading.Lock()

_pause_events: dict[str, threading.Event] = {}
_pause_lock = threading.Lock()

# Progress-save throttle
_last_progress_save: dict[str, float] = {}  # job_id -> monotonic timestamp
_PROGRESS_SAVE_INTERVAL = 1.0

_QUEUE_ORDER_FILE = JOBS_DIR / "queue_order.json"


def _save_queue_order() -> None:
    """Persist queued job order to disk atomically."""
    try:
        with _priority_lock:
            order = list(_queued_order)
        tmp = _QUEUE_ORDER_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(order))
        os.replace(tmp, _QUEUE_ORDER_FILE)
    except Exception:
        logger.warning("Failed to save queue order", exc_info=True)


def _restore_queue_order() -> None:
    """Load queued job order from disk on startup."""
    global _queued_order
    try:
        if _QUEUE_ORDER_FILE.exists():
            _queued_order = json.loads(_QUEUE_ORDER_FILE.read_text())
            if not isinstance(_queued_order, list):
                _queued_order = []
    except (json.JSONDecodeError, OSError):
        _queued_order = []


def start_worker_pool() -> None:
    """Start the download worker pool (idempotent). Called from factory.py."""
    global _started
    if _started:
        return
    _started = True
    for _ in range(MAX_CONCURRENT):
        t = threading.Thread(target=_worker_loop, daemon=True, name="download-worker")
        t.start()
    logger.info("Started %d download workers", MAX_CONCURRENT)


def recover_interrupted_jobs() -> None:
    """On startup, re-queue any jobs left running/queued from a previous run.
    Restores persisted queue order and recovers any jobs not in the saved list."""
    _restore_queue_order()
    requeued = 0
    for job in list_active_jobs():
        if job.status == JobStatus.RUNNING:
            job.status = JobStatus.QUEUED
            job.progress = 0.0
            save_job(job)
            logger.info("Recovering interrupted job %s", job.id)
        if job.id not in _queued_order:
            _queued_order.insert(0, job.id)
            requeued += 1
    if requeued:
        logger.info("Recovered %d active jobs into the queue", requeued)
    _save_queue_order()

def enqueue(job_id: str) -> None:
    """Add a job ID to the download queue (LIFO - newest first)."""
    with _cancel_lock:
        _cancel_events[job_id] = threading.Event()
    with _pause_lock:
        _pause_events[job_id] = threading.Event()
    with _priority_lock:
        _queued_order.insert(0, job_id)
    logger.info("Job %s enqueued (LIFO)", job_id)
    append_audit("job.enqueued", job_id, {"url": ""})
    _save_queue_order()
    _queue_event.set()


def subscribe(job_id: str) -> queue.Queue:
    """Subscribe to SSE events for a job. Returns a queue to read events from."""
    q: queue.Queue = queue.Queue()
    with _sub_lock:
        _subscribers.setdefault(job_id, []).append(q)
    return q


def unsubscribe(job_id: str, q: queue.Queue) -> None:
    """Unsubscribe from SSE events for a job."""
    with _sub_lock:
        subs = _subscribers.get(job_id, [])
        if q in subs:
            subs.remove(q)


def cancel_job(job_id: str) -> bool:
    """Cancel a queued or running job. Returns True if cancel was accepted."""
    with _cancel_lock:
        evt = _cancel_events.get(job_id)
    if evt is not None:
        evt.set()
        logger.info("Cancel signal sent for job %s", job_id)
        return True
    # If not in active map, try to set status directly
    job = get_job(job_id)
    if job and job.status in (JobStatus.QUEUED, JobStatus.RUNNING):
        job.status = JobStatus.CANCELLED
        save_job(job)
        _emit(job_id, {"type": "status", "status": job.status})
        logger.info("Job %s cancelled", job_id)
        return True
    return False


def retry_job(job_id: str) -> bool:
    """Retry a failed or cancelled job."""
    job = get_job(job_id)
    if not job:
        return False
    if job.status in (JobStatus.ERROR, JobStatus.CANCELLED, JobStatus.PAUSED):
        job.status = JobStatus.QUEUED
        job.progress = 0.0
        job.error = None
        save_job(job)
        enqueue(job_id)
        return True
    return False


def pause_job(job_id: str) -> bool:
    """Pause a running or queued job."""
    job = get_job(job_id)
    if not job:
        return False
    if job.status in (JobStatus.QUEUED, JobStatus.RUNNING):
        job.status = JobStatus.PAUSED
        save_job(job)
        _emit(job_id, {"type": "status", "status": job.status})
        return True
    return False


def resume_job(job_id: str) -> bool:
    """Resume a paused job."""
    job = get_job(job_id)
    if not job:
        return False
    if job.status == JobStatus.PAUSED:
        job.status = JobStatus.QUEUED
        save_job(job)
        enqueue(job_id)
        return True
    return False


def purge_terminal() -> int:
    """Delete all terminal (completed/error/cancelled) jobs. Returns count deleted."""
    from app.models import _connect
    db = _connect()
    cursor = db.execute("SELECT id FROM jobs")
    deleted = 0
    for row in cursor.fetchall():
        job = get_job(row[0])
        if job and job.status in (JobStatus.COMPLETED, JobStatus.ERROR, JobStatus.CANCELLED):
            from app.models import delete_job
            delete_job(job.id)
            deleted += 1
    return deleted


def retry_all_errored() -> int:
    """Retry all errored/cancelled jobs. Returns count retried."""
    from app.models import _connect
    db = _connect()
    cursor = db.execute("SELECT id FROM jobs")
    retried = 0
    for row in cursor.fetchall():
        job = get_job(row[0])
        if job and job.status in (JobStatus.ERROR, JobStatus.CANCELLED):
            if retry_job(job.id):
                retried += 1
    return retried


def _emit(job_id: str, event: dict) -> None:
    with _sub_lock:
        subs = _subscribers.get(job_id, [])
        for q in subs:
            try:
                q.put_nowait(event)
            except queue.Full:
                pass


def _worker_loop() -> None:
    while True:
        with _priority_lock:
            if _queued_order:
                job_id = _queued_order.pop(0)
            else:
                job_id = None
                # Clear inside the lock. Clearing outside it can wipe the flag
                # set by an enqueue that landed in between, delaying that job
                # until the wait() timeout expires.
                _queue_event.clear()
        if job_id is not None:
            _save_queue_order()
        if job_id is None:
            _queue_event.wait(timeout=0.5)  # blocks with timeout instead of busy-sleeping
            continue
        try:
            _run_download(job_id)
        except Exception:
            logger.error("Worker error for job %s", job_id, exc_info=True)
        finally:
            _last_progress_save.pop(job_id, None)
            with _cancel_lock:
                _cancel_events.pop(job_id, None)


def _clear_cancel(job_id: str) -> threading.Event | None:
    with _cancel_lock:
        return _cancel_events.get(job_id)


# ---------------------------------------------------------------------------
# Opus conversion (pre-promote)
# ---------------------------------------------------------------------------

_AUDIO_SUFFIXES = {".flac", ".m4a", ".mp3", ".wav", ".ogg", ".aac", ".aiff", ".opus"}


def _collect_audio_files(path: Path) -> list[Path]:
    """Return audio files under *path*.

    Single file → ``[file]`` (if it is audio); directory → all matching audio
    files inside it (sorted).
    """
    if path.is_file():
        return [path] if path.suffix.lower() in _AUDIO_SUFFIXES else []
    if path.is_dir():
        return sorted(
            f for f in path.iterdir()
            if f.is_file() and f.suffix.lower() in _AUDIO_SUFFIXES
        )
    return []


# Mapping of converted opus paths → their original source paths.
# Populated by _convert_to_opus; cleaned up by the promote path after
# a successful promote.
_converted_originals: dict[Path, Path] = {}


def _convert_to_opus(files: list[Path]) -> list[Path]:
    """Convert audio files to Opus 160k via ffmpeg (MP3 and Opus passthrough).

    Matches the beets convert plugin configuration.  MP3 files are kept as-is
    and existing Opus files are left untouched; all other audio types are
    transcoded.  Conversion is done to a temporary file first, then atomically
    replaces the original.  On failure the original file is kept and a warning
    is logged.

    The original source file is **not** removed here; callers are responsible
    for unlinking it after the converted opus file has been successfully moved
    to its final destination.
    """
    result: list[Path] = []
    for src in files:
        if src.suffix.lower() in (".opus", ".mp3"):
            result.append(src)
            continue

        opus_path = src.with_suffix(".opus")
        tmp = None
        try:
            # Write to a temp file in the same directory for atomic rename
            fd, tmp = tempfile.mkstemp(suffix=".opus", dir=src.parent)
            os.close(fd)

            cmd = [
                "ffmpeg",
                "-i", str(src),
                "-y",
                "-c:a", "libopus",
                "-b:a", "160k",
                "-ac", "2",
                str(tmp),
            ]
            subprocess.run(cmd, check=True, capture_output=True, timeout=300)

            # Atomically replace the source
            os.replace(tmp, opus_path)
            tmp = None  # successfully moved; don't clean up below

            # Track original so the caller can unlink it after promote succeeds
            _converted_originals[opus_path.resolve()] = src.resolve()

            logger.info("Converted %s -> %s", src.name, opus_path.name)
            result.append(opus_path)
        except Exception as exc:
            logger.warning("opus conversion failed for %s: %s", src, exc)
            result.append(src)
        finally:
            if tmp is not None:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
    return result


def _cleanup_converted_original(opus_resolved: Path) -> None:
    """After a converted opus file is promoted, remove the original staging source."""
    original = _converted_originals.pop(opus_resolved, None)
    if original is not None:
        try:
            original.unlink(missing_ok=True)
            logger.debug("Removed converted original %s after promote", original)
        except OSError as exc:
            logger.warning("Could not remove converted original %s: %s", original, exc)


def _rollback_converted_original(opus_resolved: Path) -> None:
    """When promote fails for a converted opus, remove the leftover .opus so the
    original source is the only copy left in staging (no orphans)."""
    original = _converted_originals.get(opus_resolved)
    if original is None:
        return  # not a converted file — nothing to roll back
    try:
        opus_resolved.unlink(missing_ok=True)
        logger.debug("Removed orphaned .opus %s after promote failure", opus_resolved)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Download runner
# ---------------------------------------------------------------------------


def _run_download(job_id: str) -> None:
    """Execute a single download job."""
    job = get_job(job_id)
    if not job:
        logger.warning("Job %s not found in DB", job_id)
        return

    cancel_event = _clear_cancel(job_id)

    if cancel_event is not None and cancel_event.is_set():
        job.status = JobStatus.CANCELLED
        save_job(job)
        _emit(job_id, {"type": "status", "status": job.status})
        return
    if job.status == JobStatus.PAUSED:
        logger.info("Job %s is paused — skipping", job_id)
        return

    job.status = JobStatus.RUNNING
    job.progress = 0.0
    save_job(job)
    _emit(job_id, {"type": "status", "status": job.status})

    def progress_callback(bytes_done: int, bytes_total: int) -> None:
        if bytes_total > 0:
            job.progress = bytes_done / bytes_total
            now = time.monotonic()
            if now - _last_progress_save.get(job_id, 0.0) >= _PROGRESS_SAVE_INTERVAL:
                save_job(job)
                _last_progress_save[job_id] = now
            _emit(job_id, {"type": "progress", "progress": job.progress})

    def is_cancelled() -> bool:
        return cancel_event is not None and cancel_event.is_set()

    try:
        # Initialize the Qobuz session if needed
        session = init_session()

        downloader = QobuzDownloader(
            session=session,
            progress_cb=progress_callback,
            cancel_check=is_cancelled,
            proxy_url=get_proxy_url(job.proxy_index),
        )
        result_path = downloader.download_url(job.url)
        job.status = JobStatus.COMPLETED
        job.progress = 1.0
        job.output_path = str(result_path)
        save_job(job)
        _emit(job_id, {"type": "status", "status": job.status, "path": job.output_path})
        logger.info("Job %s completed: %s", job_id, result_path)

        # Convert downloaded audio to opus (except MP3 and existing opus)
        audio_files = _collect_audio_files(Path(result_path))
        if audio_files:
            converted = _convert_to_opus(audio_files)
            # Update result_path for single-file downloads so promote moves the
            # converted file rather than the now-removed original.
            if not Path(result_path).is_dir() and len(converted) == 1:
                result_path = str(converted[0])
            job.output_path = str(result_path)
            save_job(job)

        # Post-download: promote to library if configured
        promoted = []
        if PROMOTE_TO_LIBRARY and result_path:
            promoted = _promote_to_library(Path(result_path), job, job_id)
        # Playlist jobs: mirror library files into PLAYLIST_DIR/<name>/ as
        # hard links (no data duplication), copy2 fallback on EXDEV.
        if job.kind == "playlist" and promoted:
            _hardlink_playlist(job, promoted)

    except DownloadCancelled:
        job.status = JobStatus.CANCELLED
        save_job(job)
        _emit(job_id, {"type": "status", "status": job.status})
        logger.info("Job %s cancelled", job_id)
    except Exception as exc:
        job.status = JobStatus.ERROR
        job.error = str(exc)
        save_job(job)
        _emit(job_id, {"type": "status", "status": job.status, "error": str(exc)})
        logger.error("Job %s failed: %s", job_id, exc)


def _safe_dir_name(name: str) -> str:
    """Sanitize a playlist title into a safe directory name."""
    safe = "".join(c if (c.isalnum() or c in " ._-") else "_" for c in name).strip()
    return safe[:120] or "playlist"


def _hardlink_playlist(job, final_files: list[Path]) -> None:
    """Mirror a playlist's library files into PLAYLIST_DIR/<name>/ as hard links.

    The tracks already live in the beets library under artist/album dirs; the
    playlist folder gives a browsable view of the playlist without duplicating
    data. Falls back to copy2 if the library and playlists dir are on different
    devices (EXDEV). Best-effort: failures log and never fail the job.
    """
    if not final_files or not job.title:
        return
    dest_dir = PLAYLIST_DIR / _safe_dir_name(job.title)
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return

    linked = 0
    for f in final_files:
        src = Path(f)
        if not src.is_file():
            continue
        dst = dest_dir / src.name
        try:
            if dst.exists():
                if dst.samefile(src):
                    continue  # already linked
                dst.unlink()
            os.link(str(src), str(dst))
            linked += 1
        except OSError as exc:
            if exc.errno == errno.EXDEV:
                try:
                    shutil.copy2(str(src), str(dst))
                    linked += 1
                except OSError:
                    logger.warning("playlist copy2 failed for %s", src, exc_info=True)
            else:
                logger.warning("playlist hardlink failed for %s", src, exc_info=True)
    logger.info("Playlist '%s': %d files mirrored into %s", job.title, linked, dest_dir)


def _promote_to_library(source_path: Path, job, job_id: str) -> list[Path]:
    """Move/copy downloaded files into the library structure as ``artist/album/file``.

    Uses :data:`PROMOTE_EXISTS` to decide skip vs overwrite behaviour.
    Handles both single-file (track) and directory (album) downloads.
    Returns the final promoted file paths.
    """
    if not source_path.exists():
        logger.warning("Promote: source path does not exist: %s", source_path)
        return []

    artist = (job.artist or "Unknown Artist").strip()
    safe_artist = re.sub(r'[\\/:*?"<>|]', '_', artist)[:80]
    promoted: list[Path] = []

    if source_path.is_dir():
        # Album / playlist download — the directory is the album
        album_name = source_path.name
        dest_dir = LIBRARY_DIR / safe_artist / album_name
        dest_dir.mkdir(parents=True, exist_ok=True)
        for f in sorted(source_path.iterdir()):
            if f.is_file():
                promoted.append(_promote_single_file(f, dest_dir / f.name))
        # Clean up empty source directory
        try:
            if not any(source_path.iterdir()):
                source_path.rmdir()
        except OSError:
            pass
    else:
        # Single track
        dest_dir = LIBRARY_DIR / safe_artist / "Singles"
        dest_dir.mkdir(parents=True, exist_ok=True)
        promoted.append(_promote_single_file(source_path, dest_dir / source_path.name))

    return [p for p in promoted if p]

def _promote_single_file(src: Path, dst: Path) -> Path | None:
    """Copy *src* to *dst*, honouring :data:`PROMOTE_EXISTS`. Returns dst or None."""
    if dst.exists() and PROMOTE_EXISTS == "skip":
        logger.debug("Promote: skipping existing %s", dst)
        return dst
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    logger.debug("Promote: copied %s -> %s", src, dst)
    return dst


def list_jobs(limit: int = 100) -> list:
    """List jobs from the database."""
    from app.models import list_jobs as _list_jobs
    return _list_jobs(limit=limit)
