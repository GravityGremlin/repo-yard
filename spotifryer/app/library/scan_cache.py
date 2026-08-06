"""Background library scan cache — runs scans off the request thread with TTL."""

from __future__ import annotations

import logging
import os
import threading
import time

from app.config import LIBRARY_DIR

logger = logging.getLogger(__name__)

_AUDIO_EXTS = {".flac", ".m4a", ".mp3", ".wav", ".ogg", ".opus", ".aac"}
_MAX_SCAN_ENTRIES = 30_000
_RECENT_LIMIT = 50
_SCAN_TTL = 60  # seconds

_scan_cache: dict[str, dict | None] = {"listing": None, "stats": None}
_scan_mtime: dict[str, float] = {"listing": 0.0, "stats": 0.0}
_scan_running: set[str] = set()
_scan_lock = threading.Lock()

_bg_thread: threading.Thread | None = None


def get_cached_listing(subpath: str = "") -> list[dict] | None:
    """Return cached directory listing. Kicks a background refresh if stale.

    Returns None only on the very first call (no cache, no refresh done yet).
    After that, always returns the latest cached result even if a refresh is in flight.
    """
    now = time.time()
    with _scan_lock:
        cache = _scan_cache.get("listing")
        mtime = _scan_mtime.get("listing", 0)
        stale = (now - mtime) > _SCAN_TTL
        already_running = "listing" in _scan_running
        if stale and not already_running:
            _scan_running.add("listing")
            may_kick = True
        else:
            may_kick = False

    if may_kick:
        thread = threading.Thread(target=_refresh, args=("listing",), daemon=True)
        thread.start()
        logger.debug("Background scan started for listing")

    if cache is None:
        # First call — do a synchronous scan so we have something to return.
        _refresh("listing")
        with _scan_lock:
            cache = _scan_cache.get("listing")
    return cache


def get_cached_stats() -> dict:
    """Return cached library stats (tracks, albums, artists, bytes)."""
    now = time.time()
    with _scan_lock:
        cache = _scan_cache.get("stats")
        mtime = _scan_mtime.get("stats", 0)
        stale = (now - mtime) > _SCAN_TTL
        already_running = "stats" in _scan_running
        if stale and not already_running:
            _scan_running.add("stats")
            may_kick = True
        else:
            may_kick = False

    if may_kick:
        thread = threading.Thread(target=_refresh, args=("stats",), daemon=True)
        thread.start()

    if cache is None:
        _refresh("stats")
        with _scan_lock:
            cache = _scan_cache.get("stats") or {"tracks": 0, "albums": 0, "artists": 0, "bytes": 0}
    return cache


def refresh_now(kind: str) -> None:
    """Force a synchronous refresh (for the refresh button or startup warmup)."""
    _refresh(kind)


def start_scan_cache() -> None:
    """Start the background scan thread that refreshes cache periodically."""
    global _bg_thread
    if _bg_thread is not None and _bg_thread.is_alive():
        return

    def _loop():
        while True:
            time.sleep(_SCAN_TTL)
            for kind in ("listing", "stats"):
                with _scan_lock:
                    if kind in _scan_running:
                        continue
                    _scan_running.add(kind)
                    thread = threading.Thread(target=_refresh, args=(kind,), daemon=True)
                    thread.start()

    _bg_thread = threading.Thread(target=_loop, name="sf-scan-cache", daemon=True)
    _bg_thread.start()
    logger.info("Library scan cache started (TTL=%ds)", _SCAN_TTL)


def _refresh(kind: str) -> None:
    """Run the scan and update the cache."""
    try:
        if kind == "listing":
            data = _scan_listing()
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


def _scan_listing() -> dict:
    """Scan LIBRARY_DIR and build a dict of {relative_path: [files]}."""
    root = LIBRARY_DIR
    if not root.exists():
        return {"directories": {}, "files": []}

    directories: dict[str, list[dict]] = {}
    top_files: list[dict] = []
    scanned = 0

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        rel_dir = os.path.relpath(dirpath, root)
        if rel_dir == ".":
            rel_dir = ""

        entries: list[dict] = []
        for name in dirnames:
            entries.append({"name": name, "is_dir": True})
        for name in sorted(filenames):
            if name.startswith("."):
                continue
            ext = os.path.splitext(name)[1].lower()
            if ext not in _AUDIO_EXTS:
                continue
            scanned += 1
            if scanned > _MAX_SCAN_ENTRIES:
                break
            full = os.path.join(dirpath, name)
            try:
                st = os.stat(full)
            except OSError:
                continue
            entries.append({
                "name": name,
                "is_dir": False,
                "size": st.st_size,
                "mtime": st.st_mtime,
            })

        if rel_dir == "":
            top_files = entries
        else:
            directories[rel_dir] = entries

        if scanned > _MAX_SCAN_ENTRIES:
            break

    return {"directories": directories, "files": top_files}


def _collect_stats() -> dict:
    """Count tracks, albums, artists, and total bytes in LIBRARY_DIR."""
    root = LIBRARY_DIR
    if not root.exists():
        return {"tracks": 0, "albums": 0, "artists": 0, "bytes": 0}

    tracks = 0
    albums: set[str] = set()
    artists: set[str] = set()
    total_bytes = 0
    scanned = 0

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        rel = os.path.relpath(dirpath, root)
        parts = rel.split(os.sep) if rel != "." else []

        if len(parts) >= 1:
            artists.add(parts[0])
        if len(parts) >= 2:
            albums.add(f"{parts[0]}/{parts[1]}")

        for name in filenames:
            if name.startswith("."):
                continue
            ext = os.path.splitext(name)[1].lower()
            if ext not in _AUDIO_EXTS:
                continue
            scanned += 1
            if scanned > _MAX_SCAN_ENTRIES:
                break
            full = os.path.join(dirpath, name)
            try:
                st = os.stat(full)
                tracks += 1
                total_bytes += st.st_size
            except OSError:
                continue

        if scanned > _MAX_SCAN_ENTRIES:
            break

    return {
        "tracks": tracks,
        "albums": len(albums),
        "artists": len(artists),
        "bytes": total_bytes,
    }
