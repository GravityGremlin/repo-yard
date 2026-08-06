"""Beets import orchestration and simple library promotion.

Optional, config-driven module. Runs ``beet import`` when enabled, otherwise
falls back to a direct copy into a $artist/$album/ structure.
"""
from __future__ import annotations

import logging
import re
import shutil
import sqlite3
import subprocess
from pathlib import Path
from typing import Optional

from app.config import (
    BEETS_DIR,
    NAVIDROME_AUTO_SCAN,
    NAVIDROME_URL,
    ORGANIZE_WITH_BEETS,
    PROMOTE_EXISTS,
)

logger = logging.getLogger(__name__)


def organize_with_beets(file_path: Path, library_dir: Path) -> Optional[Path]:
    """Import a file into a beets-managed library.

    Runs ``beet import -q -C -l <config_dir> <file>``.  Returns the new
    path inside *library_dir* on success, or ``None`` if beets is disabled,
    missing, or fails.
    """
    if not ORGANIZE_WITH_BEETS:
        return None

    if not shutil.which("beet"):
        logger.warning("beet binary not found on PATH — skipping beets import")
        return None

    # ISRC pre-check: skip if already in the shared library DB
    if _isrc_in_library(file_path):
        logger.info("ISRC already in library, skipping import: %s", file_path)
        return None

    # Metadata gate: reject files with no title metadata and no parsable filename
    title = _extract_title(file_path)
    if not title:
        filename_title = _filename_title(file_path)
        if filename_title:
            try:
                from mutagen import File as MutagenFile
                audio = MutagenFile(str(file_path), easy=True)
                if audio is not None:
                    audio["title"] = filename_title
                    audio.save()
            except Exception:
                logger.warning("could not write title to %s", file_path, exc_info=True)
            title = filename_title
        else:
            _reject_file(file_path, library_dir, reason="no title metadata")
            return None

    beets_config = BEETS_DIR / "config.yaml"
    cmd = [
        "beet", "import", "-q", "-C",
        "-d", str(library_dir),
    ]
    if beets_config.exists():
        cmd.extend(["-c", str(beets_config)])
    cmd.append(str(file_path))

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except FileNotFoundError:
        logger.warning("beet binary not found — skipping import")
        return None
    except subprocess.TimeoutExpired:
        logger.error("beet import timed out for %s", file_path)
        return None

    if result.returncode != 0:
        logger.error("beet import failed (rc=%d): %s", result.returncode, result.stderr.strip())
        return None

    # Beets moves/copies the file into a new path per its paths template,
    # renaming it, so search by content signature: newest file with the same
    # suffix and a matching title stem, else newest matching-suffix file.
    candidates = [p for p in library_dir.rglob(f"*{file_path.suffix}") if p.is_file()]
    if not candidates:
        return None
    title_hint = file_path.stem.lower()
    def _score(p: Path) -> int:
        low = p.stem.lower()
        return sum(1 for part in title_hint.replace("_", " ").split() if part and part in low)
    best = max(candidates, key=lambda p: (_score(p), p.stat().st_mtime))
    if _score(best) > 0:
        return best
    return max(candidates, key=lambda p: p.stat().st_mtime)


def promote_to_library(file_path: Path, library_dir: Path) -> Path:
    """Copy file into a $artist/$album/ directory tree.

    Reads artist/album tags from the audio file via mutagen, falling back
    to *Unknown Artist* / *Unknown Album* when tags are missing.

    Returns the destination path.
    """
    try:
        from mutagen import File as MutagenFile
        audio = MutagenFile(str(file_path))
    except ImportError:
        audio = None
    except Exception:
        logger.warning("mutagen failed to load audio file")
        audio = None

    artist = "Unknown Artist"
    album = "Unknown Album"

    if audio is not None:
        artist = _first_tag(audio, "artist") or artist
        album = _first_tag(audio, "album") or album

    # Sanitize for filesystem
    artist = re.sub(r'[<>:"/\\|?*]', "_", artist).strip(". ") or "Unknown Artist"
    album = re.sub(r'[<>:"/\\|?*]', "_", album).strip(". ") or "Unknown Album"

    dest_dir = library_dir / artist / album
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / file_path.name

    if PROMOTE_EXISTS == "skip" and dest.exists():
        logger.debug("Destination exists, skipping: %s", dest)
        return dest

    shutil.copy2(file_path, dest)
    logger.info("Promoted to library: %s", dest)
    return dest


def trigger_navidrome_scan() -> None:
    """Fire-and-forget POST to Navidrome scan endpoint."""
    if not NAVIDROME_AUTO_SCAN:
        return
    try:
        import requests as _requests
        _requests.post(
            f"{NAVIDROME_URL}/api/scan",
            timeout=5,
        )
    except Exception:
        logger.warning("Navidrome scan notification failed", exc_info=True)


# ── helpers ──────────────────────────────────────────────────────

def _first_tag(audio, key: str) -> Optional[str]:
    """Extract the first value from a mutagen tag, regardless of format."""
    try:
        val = audio.get(key)
        if val is None:
            return None
        if isinstance(val, (list, tuple)):
            return str(val[0]) if val else None
        return str(val)
    except Exception:
        logger.warning("Tag extraction failed")
        return None


def _extract_title(path: Path) -> Optional[str]:
    """Return the title tag from audio metadata, or None if unreadable/empty."""
    try:
        from mutagen import File as MutagenFile
        audio = MutagenFile(str(path), easy=True)
    except Exception:
        logger.warning("could not read metadata from %s", path, exc_info=True)
        return None
    return _first_tag(audio, "title")


def _extract_isrc(path: Path) -> Optional[str]:
    """Return the ISRC tag from audio metadata, or None if unreadable/empty."""
    try:
        from mutagen import File as MutagenFile
        audio = MutagenFile(str(path), easy=True)
    except Exception:
        logger.warning("could not read ISRC from %s", path, exc_info=True)
        return None
    return _first_tag(audio, "isrc")


def _filename_title(path: Path) -> Optional[str]:
    """Derive a plausible title from the filename (e.g. '01 - Carousel.opus')."""
    title = re.sub(r"^\s*\d+\s*-\s*", "", path.stem).strip()
    title = title.strip("-_. ")
    return title or None


def _isrc_in_library(file_path: Path) -> bool:
    """Check whether this file's ISRC already exists in the shared beets DB.

    Best-effort: a missing/unreachable DB proceeds with the import rather than
    blocking a legit first import.  Returns True only when the ISRC is found.
    """
    db_path = BEETS_DIR / "library.db"
    if not db_path.is_file():
        return False
    isrc = _extract_isrc(file_path)
    if not isrc:
        return False
    try:
        uri = f"file:{db_path}?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=1) as conn:
            row = conn.execute(
                "SELECT 1 FROM items WHERE isrc = ? LIMIT 1", (isrc,)
            ).fetchone()
            return row is not None
    except (OSError, sqlite3.Error):
        logger.warning("could not check ISRC in %s; importing normally",
                       db_path, exc_info=True)
        return False


def _reject_file(file_path: Path, library_dir: Path, reason: str) -> None:
    """Move a rejected file out of the library tree into a ``_rejected`` dir."""
    rejected_dir = library_dir / "_rejected"
    try:
        rejected_dir.mkdir(parents=True, exist_ok=True)
        destination = rejected_dir / file_path.name
        counter = 1
        while destination.exists():
            stem = file_path.stem
            suffix = file_path.suffix
            destination = rejected_dir / f"{stem}.{counter}{suffix}"
            counter += 1
        file_path.replace(destination)
        logger.warning("rejected %s (%s): %s", file_path, reason, destination)
    except OSError:
        logger.warning("could not move rejected file %s (%s)",
                       file_path, reason, exc_info=True)
