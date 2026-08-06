"""Download controller — background worker pool with provider chain orchestration."""

from __future__ import annotations

import json
import logging
import os
import errno
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import requests

from app.config import (
    DOWNLOAD_DIR,
    FILENAME_TEMPLATE,
    JOBS_DIR,
    LIBRARY_DIR,
    MAX_CONCURRENT,
    MAX_QUEUE_SIZE,
    NAVIDROME_AUTO_SCAN,
    NAVIDROME_URL,
    ORGANIZE_WITH_BEETS,
    PLAYLIST_DIR,
    PROMOTE_EXISTS,
    SOURCES_PRIORITY,
)
from app.models import (
    Job,
    JobStatus,
    append_audit,
    get_job,
    list_active_jobs,
    list_jobs,
    save_job,
)
from app.spotify.resolver import (
    fetch_album_tracks,
    fetch_playlist_tracks,
    fetch_track,
    resolve_url,
)
from app.download.beets_integration import organize_with_beets
from app.sources.provider import DownloadCancelled, Track

logger = logging.getLogger(__name__)

# ── Provider registry (lazy import to avoid circular deps) ────────────────
_provider_classes: dict[str, type] = {}


def _get_provider(name: str):
    """Return an *instance* of the named provider, creating on first use."""
    cls = _provider_classes.get(name)
    if cls is None:
        if name == "streamrip":
            from app.sources.streamrip import StreamripProvider
            cls = StreamripProvider
        elif name == "ytdlp":
            from app.sources.ytdlp_fallback import YTDlpProvider
            cls = YTDlpProvider
        else:
            raise ValueError(f"Unknown provider: {name}")
        _provider_classes[name] = cls
    return cls()


# ── Queue state ───────────────────────────────────────────────────────────
_queued_order: list[str] = []
_priority_lock = threading.Lock()

_queue_event = threading.Event()
_cancel_events: dict[str, threading.Event] = {}
_cancel_lock = threading.Lock()

_subscribers: dict[str, list[queue.Queue]] = {}
_sub_lock = threading.Lock()

_started = False

_QUEUE_ORDER_FILE = JOBS_DIR / "queue_order.json"

# Progress-save throttle
_last_progress_save: dict[str, float] = {}  # job_id -> monotonic timestamp
_PROGRESS_SAVE_INTERVAL = 1.0


# ── Queue persistence ─────────────────────────────────────────────────────
def _save_queue_order() -> None:
    """Persist queued job order to disk atomically."""
    try:
        with _priority_lock:
            order = list(_queued_order)
        tmp = _QUEUE_ORDER_FILE.with_suffix(".tmp")
        os.makedirs(os.path.dirname(_QUEUE_ORDER_FILE), exist_ok=True)
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


# ── Subscriber cleanup ────────────────────────────────────────────────────
_cleanup_timer: threading.Timer | None = None


def _cleanup_subscribers() -> None:
    """Periodically remove stale subscriber entries."""
    global _cleanup_timer
    with _sub_lock:
        for job_id, subs in list(_subscribers.items()):
            if not subs:
                del _subscribers[job_id]
    _cleanup_timer = threading.Timer(300, _cleanup_subscribers)
    _cleanup_timer.daemon = True
    _cleanup_timer.start()


# ── Worker pool ───────────────────────────────────────────────────────────
def start_worker_pool() -> None:
    """Start the background download worker threads (idempotent)."""
    global _started
    if _started:
        return
    _restore_queue_order()
    _cleanup_subscribers()
    _started = True
    for i in range(MAX_CONCURRENT):
        t = threading.Thread(target=_worker_loop, name=f"sf-worker-{i}", daemon=True)
        t.start()
    logger.info("Started %d download workers", MAX_CONCURRENT)


def recover_interrupted_jobs() -> None:
    """On startup, re-queue any jobs left running/queued from a previous run."""
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


# ── Worker loop ───────────────────────────────────────────────────────────
def _worker_loop() -> None:
    while True:
        with _priority_lock:
            if _queued_order:
                job_id = _queued_order.pop(0)
            else:
                job_id = None
                # Clear inside the lock: an enqueue that happens after this
                # point necessarily sets the event again, so no wakeup is lost.
                # Without this the event stays set forever after the first
                # enqueue and every idle worker spins at 100% CPU.
                _queue_event.clear()
        if job_id is not None:
            _save_queue_order()
        if job_id is None:
            _queue_event.wait(timeout=0.5)
            continue
        try:
            _process_job(job_id)
        except Exception:
            logger.error("Worker error for job %s", job_id, exc_info=True)
        finally:
            _last_progress_save.pop(job_id, None)
            with _cancel_lock:
                _cancel_events.pop(job_id, None)


# ── Job processing ────────────────────────────────────────────────────────
def _process_job(job_id: str) -> None:
    """Execute a single download job through the provider chain."""
    job = get_job(job_id)
    if not job:
        logger.warning("Job %s not found in DB", job_id)
        return

    cancel_event = _get_cancel_event(job_id)
    if cancel_event is not None and cancel_event.is_set():
        job.status = JobStatus.CANCELLED
        save_job(job)
        _emit(job_id, {"type": "status", "status": job.status})
        return

    if job.status == JobStatus.PAUSED:
        logger.info("Job %s is paused — skipping", job_id)
        return

    # Mark running
    job.status = JobStatus.RUNNING
    job.progress = 0.0
    save_job(job)
    _emit(job_id, {"type": "status", "status": job.status, "progress": 0.0})
    append_audit("job.running", job_id, {"url": job.url})

    def cancel_check() -> bool:
        return cancel_event is not None and cancel_event.is_set()

    def on_progress(downloaded: int, total: int) -> None:
        if total > 0:
            job.bytes_downloaded = downloaded
            job.total_bytes = total
            job.progress = downloaded / total
        else:
            job.bytes_downloaded = downloaded
            job.progress = min(0.99, downloaded / max(total, 1))
        now = time.monotonic()
        if now - _last_progress_save.get(job_id, 0.0) >= _PROGRESS_SAVE_INTERVAL:
            save_job(job)
            _last_progress_save[job_id] = now
        _emit(job_id, {
            "type": "progress",
            "bytes_downloaded": downloaded,
            "total_bytes": total,
            "progress": job.progress,
        })

    try:
        # Fetch Spotify metadata for this track.
        spotify_id = job.data.get("spotify_id", "")
        if not spotify_id:
            # Try to extract from URL via resolver
            try:
                _kind, spotify_id = resolve_url(job.url)
            except ValueError:
                job.status = JobStatus.ERROR
                job.error = "Could not determine Spotify ID from URL"
                save_job(job)
                _emit(job_id, {"type": "error", "error": job.error})
                append_audit("job.error", job_id, {"error": job.error})
                return

        try:
            track_meta = fetch_track(spotify_id)
        except Exception as exc:
            job.status = JobStatus.ERROR
            job.error = f"Failed to fetch track metadata: {exc}"
            save_job(job)
            _emit(job_id, {"type": "error", "error": job.error})
            append_audit("job.error", job_id, {"error": job.error})
            return

        provider_track = _spotify_track_to_provider_track(track_meta)

        # Try each provider in configured priority order.
        downloaded_path: Path | None = None
        used_provider: str = ""
        staging_dir = DOWNLOAD_DIR / job.id

        for provider_name in SOURCES_PRIORITY:
            if cancel_check():
                raise DownloadCancelled()

            try:
                provider = _get_provider(provider_name)
            except ValueError:
                logger.warning("Skipping unknown provider: %s", provider_name)
                continue

            _emit(job_id, {"type": "provider", "provider": provider.name})
            logger.info("Job %s: trying provider %s", job_id, provider.name)

            try:
                resource_id = provider.search(provider_track)
            except Exception:  # Providers are abstract; may raise OSError, ValueError, etc.
                logger.warning("Provider %s search failed for job %s", provider.name, job_id, exc_info=True)
                continue

            if resource_id is None:
                logger.info("Provider %s: not found for job %s", provider.name, job_id)
                continue

            try:
                staging_dir.mkdir(parents=True, exist_ok=True)
                result = provider.download(
                    provider_track,
                    resource_id,
                    staging_dir,
                    progress_cb=on_progress,
                    cancel_signal=cancel_check,
                )
                if result and result.exists():
                    downloaded_path = result
                    used_provider = provider.name
                    break
                else:
                    logger.warning("Provider %s: download returned no file for job %s", provider.name, job_id)
            except DownloadCancelled:
                raise
            except Exception:  # Providers are abstract; may raise subprocess, network, or OS errors
                logger.warning("Provider %s download failed for job %s", provider.name, job_id, exc_info=True)
                continue

        if downloaded_path is None:
            job.status = JobStatus.ERROR
            job.error = "No source found"
            save_job(job)
            _emit(job_id, {"type": "error", "error": job.error})
            append_audit("job.error", job_id, {"error": "No source found"})
            return

        # Finalize: move to library.
        final_files = _finalize_download(job, downloaded_path)

        job.files = [str(f) for f in final_files]
        # Playlist jobs: mirror library files into PLAYLIST_DIR/<name>/ as
        # hard links (no data duplication), copy2 fallback on EXDEV.
        if job.kind == "playlist":
            _hardlink_playlist(job, final_files)
        job.status = JobStatus.COMPLETED
        job.progress = 1.0
        job.provider = used_provider
        save_job(job)
        _emit(job_id, {
            "type": "status",
            "status": job.status,
            "progress": 1.0,
            "files": job.files,
        })
        logger.info("Job %s completed via %s: %d files", job_id, used_provider, len(final_files))
        append_audit("job.completed", job_id, {"files": len(final_files), "provider": used_provider})

        # Trigger Navidrome scan (fire-and-forget).
        if NAVIDROME_AUTO_SCAN:
            threading.Thread(target=_trigger_navidrome_scan, daemon=True).start()

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
        logger.error("Job %s failed: %s", job_id, exc, exc_info=True)
    finally:
        # Clean up staging dir on success; keep on error for debugging.
        if downloaded_path is not None and staging_dir.exists():
            shutil.rmtree(staging_dir, ignore_errors=True)


# ── Sanitization helpers ───────────────────────────────────────────────────
_CONTROL_CHARS = re.compile(r'[\x00-\x1f\x7f]')


def _sanitize_path(segment: str) -> str:
    """Sanitize a single path segment to prevent traversal and invalid chars.

    - Replaces ``/`` and ``\\`` with ``-``
    - Strips leading dots (prevents ``..`` traversal)
    - Strips leading/trailing whitespace
    - Removes null bytes and control characters
    - Truncates to 200 characters
    """
    segment = _CONTROL_CHARS.sub('', segment)
    segment = segment.replace('/', '-').replace('\\', '-')
    segment = segment.strip().strip('.')
    return segment[:200]


def _validate_download_path(file_path: Path) -> None:
    """Raise ValueError if *file_path* does not resolve under DOWNLOAD_DIR.

    Prevents path-traversal (``../``) exploits from passing arbitrary files
    to ffmpeg or other subprocesses.
    """
    resolved = file_path.resolve()
    try:
        resolved.relative_to(DOWNLOAD_DIR.resolve())
    except ValueError:
        raise ValueError(
            f"Path {file_path} resolves outside DOWNLOAD_DIR: {resolved}"
        )


def _convert_to_opus(file_path: Path) -> Path:
    """Transcode *file_path* to Opus 160 kbps stereo and return the new path.

    MP3 files are kept as-is. Files already ending in ``.opus`` are returned
    unchanged.  On any ffmpeg failure the original path is returned and a
    warning is logged.

    Raises ValueError if *file_path* resolves outside DOWNLOAD_DIR (path
    traversal guard).
    """
    if file_path.suffix.lower() in (".opus", ".mp3"):
        return file_path

    _validate_download_path(file_path)

    fd, tmp = tempfile.mkstemp(suffix=".opus", dir=file_path.parent)
    os.close(fd)
    opus_path = file_path.with_suffix(".opus")
    try:
        subprocess.run(
            [
                "ffmpeg",
                "-i",
                str(file_path),
                "-y",
                "-c:a",
                "libopus",
                "-b:a",
                "160k",
                "-ac",
                "2",
                str(opus_path),
            ],
            check=True,
            capture_output=True,
            timeout=300,
        )
        os.replace(tmp, opus_path)
        file_path.unlink()
        return opus_path
    except Exception:
        logger.warning("Failed to transcode %s to opus, keeping original", file_path.name)
        # Clean up the temp file if it was created.
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return file_path


# ── Finalization helpers ──────────────────────────────────────────────────
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


def _finalize_download(job: Job, file_path: Path) -> list[Path]:
    """Move downloaded file to library, optionally run beets."""
    file_path = _convert_to_opus(file_path)

    # Prefer beets import (proper library structure); fall back to flat move.
    if ORGANIZE_WITH_BEETS:
        try:
            organized = organize_with_beets(file_path, LIBRARY_DIR)
        except Exception:
            organized = None
        if organized is not None and organized.exists():
            return [organized]

    dest_dir = LIBRARY_DIR
    dest_dir.mkdir(parents=True, exist_ok=True)

    # Build filename from template.
    title = _sanitize_path(job.title or file_path.stem)
    artist = _sanitize_path(job.artist or "Unknown Artist")
    filename = FILENAME_TEMPLATE.format(artist=artist, title=title)
    dest = dest_dir / f"{filename}{file_path.suffix}"

    # Handle existing files.
    if dest.exists():
        if PROMOTE_EXISTS == "overwrite" or job.override_existing:
            dest.unlink()
        else:
            # skip — return the existing path
            logger.info("File already exists, skipping: %s", dest)
            return [dest]

    shutil.move(str(file_path), str(dest))
    logger.info("Promoted %s → %s", file_path.name, dest)
    return [dest]


def _trigger_navidrome_scan() -> None:
    """Trigger a Navidrome library scan (fire-and-forget)."""
    try:
        resp = requests.post(f"{NAVIDROME_URL}/api/scan?full=false", timeout=5)
        if resp.ok:
            logger.info("Navidrome scan triggered")
    except Exception as exc:
        logger.warning("Navidrome scan failed (non-critical): %s", exc)


# ── Metadata conversion ──────────────────────────────────────────────────
def _spotify_track_to_provider_track(meta: dict) -> Track:
    """Convert a Spotify resolver dict to a provider Track dataclass."""
    return Track(
        title=meta.get("title", ""),
        artist=meta.get("artist", ""),
        album=meta.get("album", ""),
        isrc=meta.get("isrc") or None,
        cover_url=meta.get("cover_url") or None,
        duration_ms=meta.get("duration_ms"),
        track_number=meta.get("track_number"),
        spotify_id=meta.get("spotify_id", ""),
    )


# ── SSE pub/sub ──────────────────────────────────────────────────────────
def _emit(job_id: str, event: dict) -> None:
    """Publish an event to all SSE subscribers for a job."""
    with _sub_lock:
        subs = _subscribers.get(job_id, [])
        for q in subs:
            try:
                q.put_nowait(event)
            except queue.Full:
                logger.warning("SSE queue full for job %s — event dropped", job_id)


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


# ── Enqueue / cancel ─────────────────────────────────────────────────────
def enqueue_download(url: str, kind: str | None = None) -> list[str]:
    """Main entry point: resolve a Spotify URL and enqueue download jobs.

    Returns a list of created job IDs.
    """
    if kind is None:
        try:
            kind, spotify_id = resolve_url(url)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc
    else:
        # Extract spotify_id from URL for the given kind
        try:
            _kind, spotify_id = resolve_url(url)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc

    group_id = _make_group_id()
    job_ids: list[str] = []

    # Queue size guard — reject new jobs when the backlog is too deep
    queue_size = len(_queued_order)
    if queue_size >= MAX_QUEUE_SIZE:
        raise RuntimeError(
            f"Queue is full ({queue_size}/{MAX_QUEUE_SIZE}). "
            "Wait for some downloads to complete or increase downloads.max_queue_size in config."
        )

    if kind == "track":
        job = _create_track_job(url, spotify_id, group_id)
        job_ids.append(job.id)

    elif kind == "album":
        try:
            tracks = fetch_album_tracks(spotify_id)
        except Exception as exc:
            raise RuntimeError(f"Failed to fetch album tracks: {exc}") from exc
        for t in tracks:
            track_url = f"https://open.spotify.com/track/{t['spotify_id']}"
            job = _create_track_job(track_url, t["spotify_id"], group_id)
            job_ids.append(job.id)

    elif kind == "playlist":
        try:
            tracks, playlist_name = fetch_playlist_tracks(spotify_id)
        except Exception as exc:
            raise RuntimeError(f"Failed to fetch playlist tracks: {exc}") from exc
        for t in tracks:
            track_url = f"https://open.spotify.com/track/{t['spotify_id']}"
            job = _create_track_job(track_url, t["spotify_id"], group_id)
            job.data["playlist_name"] = playlist_name
            job.data["playlist_url"] = url
            save_job(job)
            job_ids.append(job.id)

    else:
        raise ValueError(f"Unsupported kind: {kind}")

    # Enqueue all jobs and wake workers.
    for jid in job_ids:
        _enqueue_id(jid)

    return job_ids


def _create_track_job(url: str, spotify_id: str, group_id: str) -> Job:
    """Fetch metadata and create a Job for a single track."""
    try:
        meta = fetch_track(spotify_id)
    except Exception:
        logger.warning("Failed to create job metadata for %s", url)
        # Metadata fetch failed — create a stub job; controller will retry.
        meta = {"title": "", "artist": "", "spotify_id": spotify_id}

    job = Job(
        url=url,
        title=meta.get("title", ""),
        artist=meta.get("artist", ""),
        kind="track",
        group_id=group_id,
    )
    job.data["spotify_id"] = spotify_id
    job.data["album"] = meta.get("album", "")
    job.data["isrc"] = meta.get("isrc", "")
    job.data["cover_url"] = meta.get("cover_url", "")
    job.data["duration_ms"] = meta.get("duration_ms", 0)
    job.data["track_number"] = meta.get("track_number", 0)
    save_job(job)
    return job


def _enqueue_id(job_id: str) -> None:
    """Add a job ID to the download queue (LIFO — newest first)."""
    with _cancel_lock:
        _cancel_events[job_id] = threading.Event()
    with _priority_lock:
        _queued_order.insert(0, job_id)
    logger.info("Job %s enqueued (LIFO)", job_id)
    append_audit("job.enqueued", job_id, {})
    _save_queue_order()
    _queue_event.set()


def cancel_download(job_id: str) -> bool:
    """Request cancellation of a job. Returns True if the signal was sent."""
    with _cancel_lock:
        evt = _cancel_events.get(job_id)
    if evt is not None:
        evt.set()
        logger.info("Cancel signal sent for job %s", job_id)
        return True
    # Job may not be actively running — set status directly.
    job = get_job(job_id)
    if job and job.status in JobStatus.ACTIVE:
        job.status = JobStatus.CANCELLED
        save_job(job)
        _emit(job_id, {"type": "status", "status": job.status})
        append_audit("job.cancelled", job_id, {})
        return True
    return False


def _get_cancel_event(job_id: str) -> threading.Event | None:
    with _cancel_lock:
        return _cancel_events.get(job_id)


# ── Job lifecycle control ──────────────────────────────────────────────────
def pause_job(job_id: str) -> bool:
    """Pause a running or queued job. Returns True on success."""
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
    """Resume a paused job. Returns True on success."""
    job = get_job(job_id)
    if not job:
        return False
    if job.status == JobStatus.PAUSED:
        job.status = JobStatus.QUEUED
        save_job(job)
        _enqueue_id(job_id)
        return True
    return False


def retry_job(job_id: str) -> bool:
    """Retry a failed, cancelled, or paused job. Returns True on success."""
    job = get_job(job_id)
    if not job:
        return False
    if job.status in (JobStatus.ERROR, JobStatus.CANCELLED, JobStatus.PAUSED):
        job.status = JobStatus.QUEUED
        job.progress = 0.0
        job.error = ""
        save_job(job)
        _enqueue_id(job_id)
        return True
    return False


def purge_terminal() -> int:
    """Delete all terminal (completed/error/cancelled) jobs. Returns count deleted."""
    from app.models import delete_job as _delete_job
    deleted = 0
    for job in list_jobs(limit=100_000):
        if job.status in (JobStatus.COMPLETED, JobStatus.ERROR, JobStatus.CANCELLED):
            _delete_job(job.id)
            deleted += 1
    return deleted


def retry_all_errored() -> int:
    """Retry all errored/cancelled jobs. Returns count retried."""
    retried = 0
    for job in list_jobs(limit=100_000):
        if job.status in (JobStatus.ERROR, JobStatus.CANCELLED):
            job.status = JobStatus.QUEUED
            job.progress = 0.0
            job.error = ""
            save_job(job)
            _enqueue_id(job.id)
            retried += 1
    return retried


# ── Priority ──────────────────────────────────────────────────────────────
def priority_up(job_id: str) -> bool:
    """Move a queued job up one position (toward the front). Returns True on success."""
    with _priority_lock:
        if job_id not in _queued_order:
            return False
        idx = _queued_order.index(job_id)
        if idx == 0:
            return True  # already at front
        _queued_order[idx], _queued_order[idx - 1] = _queued_order[idx - 1], _queued_order[idx]
    _save_queue_order()
    return True


def priority_down(job_id: str) -> bool:
    """Move a queued job down one position (toward the back). Returns True on success."""
    with _priority_lock:
        if job_id not in _queued_order:
            return False
        idx = _queued_order.index(job_id)
        if idx >= len(_queued_order) - 1:
            return True  # already at back
        _queued_order[idx], _queued_order[idx + 1] = _queued_order[idx + 1], _queued_order[idx]
    _save_queue_order()
    return True


# ── Helpers ───────────────────────────────────────────────────────────────
def _make_group_id() -> str:
    """Generate a short unique group ID for batched jobs."""
    import uuid
    return uuid.uuid4().hex[:8]
