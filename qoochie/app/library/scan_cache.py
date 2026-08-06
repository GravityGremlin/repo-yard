"""Background library scan cache — runs scans off the request thread with TTL."""

from __future__ import annotations

import heapq
import logging
import os
import threading
import time
from datetime import datetime

from app.config import LIBRARY_DIR

logger = logging.getLogger(__name__)

_AUDIO_EXTS = {".flac", ".m4a", ".mp3", ".wav", ".ogg", ".opus", ".aac"}
_MAX_SCAN_ENTRIES = 30_000
_RECENT_LIMIT = 50
_SCAN_TTL = 300  # 5 minutes

_scan_cache: dict[str, dict | None] = {"recent": None, "collection": None, "stats": None}
_scan_mtime: dict[str, float] = {"recent": 0.0, "collection": 0.0, "stats": 0.0}
_scan_running: set[str] = set()
_scan_lock = threading.Lock()


def get_cached(kind: str) -> dict | None:
    """Return cached scan data. Kicks a background refresh if stale.

    Returns None only on the very first call (no cache, no refresh done yet).
    After that, always returns the latest cached result even if a refresh is in flight.
    """
    now = time.time()
    with _scan_lock:
        cache = _scan_cache.get(kind)
        mtime = _scan_mtime.get(kind, 0)
        stale = (now - mtime) > _SCAN_TTL
        already_running = kind in _scan_running

    if stale and not already_running:
        thread = threading.Thread(target=_refresh, args=(kind,), daemon=True)
        with _scan_lock:
            _scan_running.add(kind)
        thread.start()
        logger.debug("Background scan started for %s", kind)

    return cache


def refresh_now(kind: str) -> None:
    """Force a synchronous refresh (for the refresh button or startup warmup)."""
    _refresh(kind)


def _refresh(kind: str) -> None:
    """Run the scan and update the cache."""
    try:
        if kind == "recent":
            data = _scan_recent()
        elif kind == "collection":
            data = _scan_collection()
        elif kind == "stats":
            data = _collect_stats()
        else:
            return
        with _scan_lock:
            _scan_cache[kind] = data
            _scan_mtime[kind] = time.time()
    except Exception:
        logger.warning("Background scan failed for %s", kind, exc_info=True)
    finally:
        with _scan_lock:
            _scan_running.discard(kind)


def _scan_recent() -> dict:
    """Scan for most recently modified audio files. Uses heap for top-N."""
    root = LIBRARY_DIR
    if not root.exists():
        return {"items": [], "capped": False, "scanned": 0}

    heap: list[tuple[float, str, int]] = []
    scanned = 0
    try:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for name in filenames:
                scanned += 1
                if scanned > _MAX_SCAN_ENTRIES:
                    break
                if os.path.splitext(name)[1].lower() not in _AUDIO_EXTS:
                    continue
                full = os.path.join(dirpath, name)
                try:
                    stat = os.stat(full)
                except OSError:
                    continue
                rel = os.path.relpath(full, root)
                if len(heap) < _RECENT_LIMIT:
                    heapq.heappush(heap, (stat.st_mtime, rel, stat.st_size))
                elif stat.st_mtime > heap[0][0]:
                    heapq.heapreplace(heap, (stat.st_mtime, rel, stat.st_size))
            if scanned > _MAX_SCAN_ENTRIES:
                break
    except OSError as exc:
        logger.warning("Recent scan failed: %s", exc)

    items = [
        {
            "name": os.path.basename(rel),
            "path": rel,
            "size": size,
            "modified": datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M"),
            "is_dir": False,
        }
        for mtime, rel, size in sorted(heap, key=lambda x: x[0], reverse=True)
    ]
    return {"items": items, "capped": scanned > _MAX_SCAN_ENTRIES, "scanned": scanned}


def _scan_collection() -> dict:
    """Scan for collection statistics."""
    root = LIBRARY_DIR
    stats = {
        "path": str(root),
        "exists": True,
        "artists": 0,
        "albums": 0,
        "tracks": 0,
        "total_bytes": 0,
        "scanned": 0,
        "capped": False,
        "by_ext": {},
    }
    if not root.exists():
        stats["exists"] = False
        return stats

    by_ext: dict[str, int] = {}
    scanned = 0
    try:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            depth = dirpath[len(str(root)):].count(os.sep)
            if not filenames and not dirnames:
                continue
            if depth == 0:
                stats["artists"] += len(dirnames)
            elif depth == 1:
                stats["albums"] += len(dirnames)
            for name in filenames:
                scanned += 1
                if scanned > _MAX_SCAN_ENTRIES:
                    stats["capped"] = True
                    break
                ext = os.path.splitext(name)[1].lower()
                if ext not in _AUDIO_EXTS:
                    continue
                full = os.path.join(dirpath, name)
                try:
                    st = os.stat(full)
                except OSError:
                    continue
                stats["tracks"] += 1
                stats["total_bytes"] += st.st_size
                by_ext[ext] = by_ext.get(ext, 0) + 1
            if scanned > _MAX_SCAN_ENTRIES:
                stats["capped"] = True
                break
    except OSError as exc:
        logger.warning("Collection scan failed: %s", exc)

    stats["scanned"] = scanned
    stats["by_ext"] = dict(sorted(by_ext.items(), key=lambda kv: kv[1], reverse=True))
    return stats


def _collect_stats() -> dict:
    """Collect library stats for the dashboard. Delegates to _scan_collection() to avoid a duplicate walk."""
    data = _scan_collection()
    return {
        "tracks": data["tracks"],
        "artists": data["artists"],
        "albums": data["albums"],
        "bytes": data["total_bytes"],
    }
