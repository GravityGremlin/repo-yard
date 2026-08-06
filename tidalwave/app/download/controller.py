"""Download controller — background thread runner with progress tracking."""

from __future__ import annotations

import queue
import logging
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
import json
import os
import errno
import requests
from app.config import DOWNLOAD_DIR, JOBS_DIR, LIBRARY_DIR, MAX_CONCURRENT, ORGANIZE_WITH_BEETS, PLAYLIST_DIR, PROMOTE_EXISTS, PROXY_LIST, PROMOTE_TO_LIBRARY
from app.navidrome import trigger_navidrome_scan
from app.models import JobStatus, append_audit, get_job, list_active_jobs, save_job
from app.proxy import get_proxy_url
from app.tidal.downloader import DownloadCancelled, TidalDownloader
from app.tidal.session import init_session

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

_QUEUE_ORDER_FILE = JOBS_DIR / "queue_order.json"

# ── Progress-save throttle (optimization 2) ─────────────────────
_last_progress_save: dict[str, float] = {}  # job_id → monotonic timestamp
_PROGRESS_SAVE_INTERVAL = 1.0  # seconds between SQLite writes per job


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
    """Load persisted queue order, filtering out stale entries."""
    if not _QUEUE_ORDER_FILE.exists():
        return
    try:
        saved: list[str] = json.loads(_QUEUE_ORDER_FILE.read_text())
        with _priority_lock:
            restored = 0
            for jid in saved:
                job = get_job(jid)
                if job and job.status == JobStatus.QUEUED and jid not in _queued_order:
                    _queued_order.append(jid)
                    restored += 1
        if restored:
            logger.info("Restored %d queued jobs from disk", restored)
    except Exception:
        logger.warning("Failed to restore queue order", exc_info=True)


_cleanup_started = False


def _cleanup_subscribers_locked() -> None:
    """Remove subscriber entries whose queue lists are empty (caller holds _sub_lock)."""
    for job_id, subs in list(_subscribers.items()):
        if not subs:
            del _subscribers[job_id]


def _cleanup_subscriber_loop() -> None:
    """Long-lived daemon that purges stale subscriber entries every 300s."""
    while True:
        time.sleep(300)
        with _sub_lock:
            _cleanup_subscribers_locked()


def start_worker_pool() -> None:
    """Start the background download worker threads (idempotent)."""
    global _started, _cleanup_started
    if _started:
        return
    _restore_queue_order()
    _started = True
    if not _cleanup_started:
        _cleanup_started = True
        t = threading.Thread(target=_cleanup_subscriber_loop, daemon=True, name="sub-cleanup")
        t.start()
    for i in range(MAX_CONCURRENT):
        t = threading.Thread(target=_worker_loop, name=f"dl-worker-{i}", daemon=True)
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
        if not subs:
            _subscribers.pop(job_id, None)


def _emit(job_id: str, event: dict) -> None:
    with _sub_lock:
        subs = _subscribers.get(job_id, [])
        for q in list(subs):
            try:
                q.put_nowait(event)
            except queue.Full:
                subs.remove(q)
                logger.debug("Removed dead subscriber for job %s (queue full)", job_id)
                if not subs:
                    _subscribers.pop(job_id, None)
                    break


def _worker_loop() -> None:
    # Each worker owns exactly one requests.Session (thread-safe: single-owner).
    # The session proxy is reconfigured per-job below.
    http_session = requests.Session()
    while True:
        with _priority_lock:
            if _queued_order:
                job_id = _queued_order.pop(0)
            else:
                job_id = None
                _queue_event.clear()
        if job_id is not None:
            _save_queue_order()
        if job_id is None:
            _queue_event.wait(timeout=0.5)  # blocks with timeout instead of busy-sleeping
            continue
        try:
            _run_download(job_id, http_session=http_session)
        except Exception:
            logger.error("Worker error for job %s", job_id, exc_info=True)
        finally:
            _last_progress_save.pop(job_id, None)
            with _cancel_lock:
                _cancel_events.pop(job_id, None)


def _clear_cancel(job_id: str) -> threading.Event | None:
    with _cancel_lock:
        return _cancel_events.get(job_id)


def _run_download(job_id: str, http_session: requests.Session) -> None:
    """Execute a single download job, reusing the worker's HTTP session."""
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
    _emit(job_id, {"type": "status", "status": job.status, "progress": 0.0})

    proxy_url = get_proxy_url(job.proxy_index) if job.proxy_index < len(PROXY_LIST) else ""
    # Reconfigure the worker's reusable session for this job's proxy.
    http_session.proxies.clear()
    if proxy_url:
        http_session.proxies.update({"http": proxy_url, "https": proxy_url})
    try:
        session = init_session(proxy_url=proxy_url)
    except Exception as exc:
        job.status = JobStatus.ERROR
        job.error = f"Session init failed: {exc}"
        save_job(job)
        _emit(job_id, {"type": "error", "error": job.error})
        append_audit("job.error", job_id, {"error": job.error})
        return
    if not session:
        job.status = JobStatus.ERROR
        job.error = "Not connected to Tidal"
        save_job(job)
        _emit(job_id, {"type": "error", "error": job.error})
        append_audit("job.error", job_id, {"error": job.error})
        return

    downloader = TidalDownloader(session, http_session=http_session)

    def cancel_check() -> bool:
        return cancel_event is not None and cancel_event.is_set()

    def on_progress(bytes_done: int, bytes_total: int) -> None:
        if bytes_total > 0:
            job.bytes_downloaded = bytes_done
            job.total_bytes = bytes_total
            job.progress = bytes_done / bytes_total
        else:
            job.bytes_downloaded = bytes_done
            job.progress = min(0.99, bytes_done / max(bytes_total, 1))
        # Throttle SQLite writes: only persist if ≥1 s since last save.
        # Terminal saves (completed/error/cancelled) happen outside this
        # callback and are never throttled.
        now = time.monotonic()
        if now - _last_progress_save.get(job_id, 0.0) >= _PROGRESS_SAVE_INTERVAL:
            save_job(job)
            _last_progress_save[job_id] = now
        _emit(job_id, {
            "type": "progress",
            "bytes_downloaded": bytes_done,
            "total_bytes": bytes_total,
            "progress": job.progress,
        })

    try:
        files = downloader.download_url(job.url, DOWNLOAD_DIR, callback=on_progress,
                                        cancel_check=cancel_check)
        if ORGANIZE_WITH_BEETS:
            final_files = _beets_organize_job(job_id, files, override=job.override_existing)
        else:
            final_files = _maybe_promote(files)
        job.files = [str(f) for f in final_files]
        # Playlist jobs: expose a browsable PLAYLIST_DIR/<name>/ view of the
        # library files via hard links (no data duplication). Falls back to
        # copy2 if the dirs land on different devices (EXDEV).
        if job.kind == "playlist":
            _hardlink_playlist(job, final_files)
        # Guard against a concurrent cancel overwriting CANCELLED with COMPLETED.
        if cancel_event is not None and cancel_event.is_set():
            job.status = JobStatus.CANCELLED
            save_job(job)
            _emit(job_id, {"type": "status", "status": job.status})
            append_audit("job.cancelled", job_id, {})
            logger.info("Job %s cancelled after download completed", job_id)
            return
        job.status = JobStatus.COMPLETED
        job.progress = 1.0
        save_job(job)
        _emit(job_id, {"type": "status", "status": job.status, "progress": 1.0,
                        "files": job.files})
        logger.info("Job %s completed: %d files", job_id, len(final_files))
        # Trigger Navidrome library scan (fire-and-forget)
        threading.Thread(target=trigger_navidrome_scan, daemon=True).start()
        append_audit("job.completed", job_id, {"files": len(final_files)})
    except DownloadCancelled:
        job.status = JobStatus.CANCELLED
        save_job(job)
        _emit(job_id, {"type": "status", "status": job.status})
        append_audit("job.cancelled", job_id, {})
        logger.info("Job %s cancelled", job_id)
    except Exception as exc:
        job.status = JobStatus.ERROR
        job.error = str(exc)
        save_job(job)
        _emit(job_id, {"type": "error", "error": job.error})
        append_audit("job.error", job_id, {"error": job.error})
        logger.error("Job %s failed: %s", job_id, exc)


def _convert_to_opus(files: list[Path]) -> list[Path]:
    """Convert audio files to Opus 160k via ffmpeg (MP3 and Opus passthrough).

    Matches the beets convert plugin configuration.  MP3 files are kept as-is
    and existing Opus files are left untouched; all other audio types are
    transcoded.  Conversion is done to a temporary file first, then atomically
    replaces the original.  On failure the original file is kept and a warning
    is logged.

    The original source file is **not** removed here; callers (e.g.
    ``_maybe_promote``) are responsible for unlinking it after the converted
    opus file has been successfully moved to its final destination.
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


# Mapping of converted opus paths → their original source paths.
# Populated by _convert_to_opus; cleaned up by _maybe_promote after
# a successful promote.
_converted_originals: dict[Path, Path] = {}


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


def _maybe_promote(files: list[Path]) -> list[Path]:
    """Move completed downloads into the library directory, preserving the
    artist/album/track structure the downloader created under DOWNLOAD_DIR.

    Skips promotion if disabled in config, or if the download dir already is
    the library dir. Returns the final file paths (library paths when promoted).
    """
    if not files or not PROMOTE_TO_LIBRARY:
        return files

    try:
        src_root = DOWNLOAD_DIR.resolve()
        dst_root = LIBRARY_DIR.resolve()
    except OSError:
        return files
    if src_root == dst_root:
        return files

    # Convert to opus before promoting (all non-opus files get transcoded)
    files = _convert_to_opus(files)

    dst_root.mkdir(parents=True, exist_ok=True)

    # ── Skip mode ───────────────────────────────────────────────
    if PROMOTE_EXISTS == "skip":
        final: list[Path] = []
        for src in files:
            src_resolved = src.resolve()
            try:
                rel = src_resolved.relative_to(src_root)
            except (ValueError, OSError):
                final.append(src)
                continue
            dst = dst_root / rel
            if dst.exists():
                logger.info("promote_exists=skip: keeping existing %s, leaving staged %s", dst, src)
                final.append(src)
                continue
            # Doesn't exist yet — promote normally
            dst.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.move(str(src_resolved), str(dst))
                _cleanup_converted_original(src_resolved)
                _maybe_rmdir(src.parent, src_root)
                final.append(dst)
            except (OSError, shutil.Error) as exc:
                logger.warning("Promote failed for %s: %s", src, exc)
                _rollback_converted_original(src_resolved)
                final.append(_converted_originals.get(src_resolved, src))
        return final

    # ── Overwrite mode ──────────────────────────────────────────
    final: list[Path] = []
    for src in files:
        src_resolved = src.resolve()
        try:
            rel = src_resolved.relative_to(src_root)
        except (ValueError, OSError):
            final.append(src)
            continue
        dst = dst_root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists():
            try:
                dst.unlink()
            except OSError:
                pass
        try:
            shutil.move(str(src_resolved), str(dst))
            _cleanup_converted_original(src_resolved)
            # Remove now-empty parent dirs left behind in the staging area.
            _maybe_rmdir(src.parent, src_root)
            final.append(dst)
        except (OSError, shutil.Error) as exc:
            logger.warning("Promote failed for %s: %s", src, exc)
            _rollback_converted_original(src_resolved)
            final.append(_converted_originals.get(src_resolved, src))
    return final


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


def _beets_organize_job(job_id: str, files: list[Path], override: bool = False) -> list[Path]:
    from app.download.beets_integration import beets_import_album

    album_dirs: dict[Path, list[Path]] = {}
    for f in files:
        album_dirs.setdefault(f.parent, []).append(f)

    all_imported: list[Path] = []
    import shutil as _shutil
    for album_dir, src_files in album_dirs.items():
        _emit(job_id, {"type": "status", "status": JobStatus.RUNNING, "phase": "organizing"})
        append_audit("job.organizing", job_id, {"album_dir": str(album_dir), "tracks": len(src_files), "override": override})
        result = beets_import_album(album_dir, override=override)
        if not result.ok:
            logger.error("beets import failed for %s: %s", album_dir, result.error)
            job = get_job(job_id)
            if job:
                job.error = f"beets organize failed: {result.error}"
                job.status = JobStatus.ERROR
                save_job(job)
            _emit(job_id, {"type": "error", "error": f"beets organize failed: {result.error}"})
            return []
        if result.skipped_duplicate:
            logger.info("beets skipped duplicate %s (override=False)", album_dir)
            append_audit("job.duplicate_skipped", job_id, {"album_dir": str(album_dir)})
            _emit(job_id, {"type": "status", "status": JobStatus.RUNNING, "phase": "duplicate-skipped",
                           "album_dir": str(album_dir)})
        if result.removed_existing:
            logger.info("beets replaced existing library copy of %s (override=True)", album_dir)
            append_audit("job.overrode_existing", job_id, {"album_dir": str(album_dir)})
            _emit(job_id, {"type": "status", "status": JobStatus.RUNNING, "phase": "overrode-existing",
                           "album_dir": str(album_dir)})
        all_imported.extend(result.imported_files)

    for album_dir in album_dirs:
        try:
            resolved = album_dir.resolve()
            if resolved.is_relative_to(DOWNLOAD_DIR.resolve()):
                _shutil.rmtree(resolved)
                if album_dir.parent != DOWNLOAD_DIR and not any(album_dir.parent.iterdir()):
                    album_dir.parent.rmdir()
        except Exception:
            logger.warning("could not clean staged %s", album_dir, exc_info=True)

    return all_imported


def _maybe_rmdir(d: Path, root: Path) -> None:
    """Remove d and its empty ancestors up to (not including) root."""
    try:
        d_rel = d.resolve()
        root_rel = root.resolve()
        d_rel.relative_to(root_rel)
    except (ValueError, OSError):
        return
    while d != root and d.exists():
        try:
            d.rmdir()
        except OSError:
            logger.debug("Could not remove %s (not empty or missing)", d)
            break
        d = d.parent


def cancel_job(job_id: str) -> bool:
    """Cancel a queued or running job. Returns True if cancelled."""
    job = get_job(job_id)
    if not job or job.status in JobStatus.TERMINAL:
        return False
    # Remove from priority queue if queued
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
    append_audit("job.cancelled", job_id, {})
    return True

def reorder_queue(job_id: str, direction: str) -> bool:
    """Reorder a queued job up/down in the priority queue.

    Args:
        job_id: The job ID to move.
        direction: 'up' to move toward front (sooner), 'down' to move toward back (later).

    Returns:
        True if moved, False if not found or already at boundary."""
    if direction not in ("up", "down"):
        return False
    with _priority_lock:
        try:
            idx = _queued_order.index(job_id)
        except ValueError:
            return False
        if direction == "up":
            if idx == 0:
                return False
            _queued_order[idx], _queued_order[idx - 1] = _queued_order[idx - 1], _queued_order[idx]
        else:  # down
            if idx == len(_queued_order) - 1:
                return False
            _queued_order[idx], _queued_order[idx + 1] = _queued_order[idx + 1], _queued_order[idx]
        _save_queue_order()
        return True