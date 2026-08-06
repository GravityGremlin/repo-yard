"""Beets import integration — runs `beet import` on a staged download dir."""
from __future__ import annotations

import logging
import os
import re
import select
import signal
import sqlite3
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import yaml
from mutagen import File as MutagenFile

from app.config import BEETS_DIR
from app.library.scan_cache import _AUDIO_EXTS

logger = logging.getLogger(__name__)

# Bind-mounted download dirs (NFS/overlay) can lag: a file tidalwave just wrote
# is not always immediately visible to the beets subprocess. Poll briefly for
# the staged audio to appear+settle before invoking beets.
_STAGE_READY_ATTEMPTS = 5
_STAGE_READY_INTERVAL_S = 1.0


@dataclass
class BeetsResult:
    ok: bool
    imported_files: list[Path]
    skipped_duplicate: bool = False
    removed_existing: bool = False
    error: str | None = None
    rejected_files: list[Path] = field(default_factory=list)


def _tag_value(audio, key: str) -> str | None:
    values = audio.get(key) if audio is not None else None
    if not values:
        return None
    value = values[0] if isinstance(values, (list, tuple)) else values
    text = str(value).strip()
    return text or None


def _audio_metadata(path: Path) -> tuple[str | None, str | None]:
    try:
        audio = MutagenFile(str(path), easy=True)
        return _tag_value(audio, "title"), _tag_value(audio, "isrc")
    except Exception:
        logger.warning("could not read metadata from %s", path, exc_info=True)
        return None, None


def _filename_title(path: Path) -> str | None:
    title = re.sub(r"^\s*\d+\s*-\s*", "", path.stem).strip()
    title = title.strip("-_. ")
    return title or None


def _reject_untitled_files(album_dir: Path) -> list[Path]:
    rejected: list[Path] = []
    rejected_dir = album_dir.parent / "_rejected"
    for path in _audio_files(album_dir):
        title, _ = _audio_metadata(path)
        if title:
            continue
        filename_title = _filename_title(path)
        if filename_title:
            try:
                audio = MutagenFile(str(path), easy=True)
                if audio is not None:
                    audio["title"] = filename_title
                    audio.save()
            except Exception:
                logger.warning("could not write title to %s", path, exc_info=True)
            continue
        rejected_dir.mkdir(parents=True, exist_ok=True)
        destination = rejected_dir / path.name
        counter = 1
        while destination.exists():
            destination = rejected_dir / f"{path.stem}.{counter}{path.suffix}"
            counter += 1
        path.replace(destination)
        rejected.append(destination)
        logger.warning("rejecting staged audio with no title: %s", path)
    return rejected


def _all_isrcs_in_library(album_dir: Path) -> bool:
    files = _audio_files(album_dir)
    isrcs = {_audio_metadata(path)[1] for path in files}
    if not files or None in isrcs or not (BEETS_DIR / "library.db").is_file():
        return False
    db_path = BEETS_DIR / "library.db"
    try:
        uri = f"file:{db_path}?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=1) as conn:
            placeholders = ",".join("?" for _ in isrcs)
            rows = conn.execute(
                f"SELECT DISTINCT isrc FROM items WHERE isrc IN ({placeholders})", tuple(isrcs)
            )
            return isrcs <= {row[0] for row in rows}
    except (OSError, sqlite3.Error):
        logger.warning("could not check staged ISRCs in %s; importing normally", db_path,
                       exc_info=True)
        return False



def _wait_for_staged_audio(album_dir: Path) -> bool:
    os.sync()
    for _ in range(_STAGE_READY_ATTEMPTS):
        if album_dir.exists() and any(_audio_files(album_dir)):
            return True
        time.sleep(_STAGE_READY_INTERVAL_S)
    return False


def beets_import_album(album_dir: Path, override: bool = False) -> BeetsResult:
    if not _wait_for_staged_audio(album_dir):
        return BeetsResult(ok=True, imported_files=[])

    rejected_files = _reject_untitled_files(album_dir)
    if not _audio_files(album_dir):
        return BeetsResult(ok=True, imported_files=[], rejected_files=rejected_files)

    existing = _album_in_library(album_dir)
    if existing and override:
        logger.info("override=True: removing existing library copy %s", existing)
        if not _beet_remove_album(existing):
            return BeetsResult(
                ok=False, imported_files=[], error=f"override remove failed for {existing}"
            )
    elif existing and not override:
        logger.info(
            "album already in library, override=False: skipping import of %s", album_dir
        )
        return BeetsResult(
            ok=True, imported_files=[], rejected_files=rejected_files,
            skipped_duplicate=True,
        )

    if not override and _all_isrcs_in_library(album_dir):
        logger.info("all staged ISRCs already in library: skipping import of %s", album_dir)
        return BeetsResult(
            ok=True, imported_files=[], rejected_files=rejected_files,
            skipped_duplicate=True,
        )

    cmd = [
        "beet", "-c", str(BEETS_DIR / "config.yaml"),
        "import", "--noautotag", "-C", "-q",
        str(album_dir.resolve()),
    ]
    logger.info("beets import: %s", " ".join(cmd))
    max_retries = 3
    for attempt in range(max_retries):
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
            break
        except subprocess.TimeoutExpired as e:
            return BeetsResult(ok=False, imported_files=[], error=f"beets timed out: {e}")
        except FileNotFoundError:
            return BeetsResult(
                ok=False, imported_files=[], error="beet binary not found — beets install broken"
            )
        except Exception:
            if attempt < max_retries - 1:
                wait = 0.5 * (2 ** attempt)
                logger.warning("beets import attempt %d failed, retrying in %.1fs", attempt + 1, wait)
                time.sleep(wait)
            else:
                raise
    logger.info("beets stdout: %s", proc.stdout.strip()[:500])
    if proc.stderr.strip():
        logger.info("beets stderr: %s", proc.stderr.strip()[:500])
    if proc.returncode != 0:
        return BeetsResult(
            ok=False, imported_files=[],
            error=f"beets rc={proc.returncode}: {proc.stderr.strip()[:500]}",
        )
    imported = _recent_beets_imports(album_dir)
    return BeetsResult(
        ok=True, imported_files=imported, rejected_files=rejected_files,
        removed_existing=bool(existing),
    )


def _album_in_library(album_dir: Path) -> str | None:
    """Return the library path of *album_dir* if it already exists in the beets
    library, or ``None`` if not found.

    The returned value can be passed directly to :func:`_beet_remove_album`.
    """
    album_artist = album_dir.parent.name
    album_title = album_dir.name
    album_title = re.sub(r"\s*\(\d{4}\)\s*$", "", album_title).strip()
    query = f"album:'{album_title}' albumartist:'{album_artist}'"
    try:
        proc = subprocess.run(
            ["beet", "-c", str(BEETS_DIR / "config.yaml"), "ls", "-f", "$path", query],
            capture_output=True, text=True, timeout=30,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            # Return the first matched album path (directory path).
            return proc.stdout.strip().splitlines()[0]
    except Exception:
        logger.warning("album-in-library check failed for %s", album_dir, exc_info=True)
    return None


def _beet_remove_album(query: str) -> bool:
    try:
        proc = subprocess.run(
            ["beet", "-c", str(BEETS_DIR / "config.yaml"), "remove", "-f", "-d", query],
            capture_output=True, text=True, timeout=120,
        )
        return proc.returncode == 0
    except Exception:
        logger.error("beet remove failed for query %s", query, exc_info=True)
        return False


def _audio_files(d: Path) -> list[Path]:
    return [
        p for p in d.iterdir()
        if p.suffix.lower() in _AUDIO_EXTS
    ]


def _recent_beets_imports(album_dir: Path) -> list[Path]:
    cmd = [
        "beet", "-c", str(BEETS_DIR / "config.yaml"),
        "ls", "-f", "$path", "path::^/music/",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        now = time.time()
        recent: list[Path] = []
        for line in proc.stdout.splitlines():
            p = Path(line.strip())
            if p.exists() and (now - p.stat().st_mtime) < 300:
                recent.append(p)
        return recent
    except Exception:
        logger.warning("could not enumerate recent beets imports", exc_info=True)
        return []


def beets_import_upload(
    staging_dir: Path,
    cancel_event: threading.Event | None = None,
    progress_callback: Callable[[str], None] | None = None,
) -> BeetsResult:
    """Autotag-enabled import counterpart to beets_import_album.

    Generates a temporary beets config overlay, runs ``beet -C -q import``
    (without ``--noautotag`` so MusicBrainz matching is active), and streams
    real-time output through *progress_callback*.  Supports cancellation and
    a 600-second overall timeout.
    """
    if not _wait_for_staged_audio(staging_dir):
        return BeetsResult(ok=True, imported_files=[])

    temp_dir = Path("/tmp/tidalwave")
    temp_dir.mkdir(parents=True, exist_ok=True)
    job_id = uuid.uuid4().hex
    temp_config = temp_dir / f"job_{job_id}.yaml"

    try:
        # ---- config overlay -------------------------------------------------
        try:
            with open(BEETS_DIR / "config.yaml") as fh:
                cfg = yaml.safe_load(fh) or {}
        except Exception as exc:
            return BeetsResult(
                ok=False, imported_files=[], error=f"Failed to read beets config: {exc}"
            )

        cfg.setdefault("import", {})["quiet_fallback"] = "skip"

        try:
            with open(temp_config, "w") as fh:
                yaml.dump(cfg, fh)
        except Exception as exc:
            return BeetsResult(
                ok=False, imported_files=[], error=f"Failed to write temp config: {exc}"
            )

        # ---- build command (no --noautotag) ---------------------------------
        cmd = [
            "beet", "-c", str(temp_config),
            "import", "-C", "-q",
            str(staging_dir.resolve()),
        ]
        logger.info("beets import upload: %s", " ".join(cmd))

        # ---- run beets with streaming output --------------------------------
        start_time = time.monotonic()
        timeout = 600
        output_lines: list[str] = []

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                universal_newlines=True,
                bufsize=1,
                preexec_fn=os.setsid,
            )
            assert proc.stdout is not None  # PIPE guarantees a stream

            def _kill_process() -> None:
                """Send SIGTERM to the process group; escalate to SIGKILL after 5 s."""
                try:
                    pgid = os.getpgid(proc.pid)
                    os.killpg(pgid, signal.SIGTERM)
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                    except ProcessLookupError:
                        pass

            def _emit(line: str) -> None:
                """Strip \\r carriage-return progress lines and forward to callback."""
                if "\r" in line:
                    line = line.rsplit("\r", 1)[-1]
                stripped = line.rstrip()
                if stripped and progress_callback:
                    progress_callback(stripped)
                output_lines.append(line.rstrip())

            while True:
                # ---- cancellation check -------------------------------------
                if cancel_event and cancel_event.is_set():
                    _kill_process()
                    return BeetsResult(ok=False, imported_files=[], error="Cancelled by user")

                # ---- timeout check ------------------------------------------
                elapsed = time.monotonic() - start_time
                if elapsed > timeout:
                    _kill_process()
                    return BeetsResult(
                        ok=False, imported_files=[], error=f"beets timed out after {timeout}s"
                    )

                # ---- non-blocking read --------------------------------------
                ready, _, _ = select.select([proc.stdout.fileno()], [], [], 2.0)
                if ready:
                    line = proc.stdout.readline()
                    if not line and proc.poll() is not None:
                        break
                    if line:
                        _emit(line)
                elif proc.poll() is not None:
                    # process exited: drain remaining output
                    for line in proc.stdout.readlines():
                        _emit(line)
                    break

            rc = proc.wait()

        except FileNotFoundError:
            return BeetsResult(
                ok=False, imported_files=[], error="beet binary not found — beets install broken"
            )
        except Exception as exc:
            return BeetsResult(ok=False, imported_files=[], error=f"beets import failed: {exc}")

        # ---- interpret return code ------------------------------------------
        if rc != 0:
            last_lines = "\n".join(output_lines[-20:]) if output_lines else "(no output)"
            return BeetsResult(
                ok=False,
                imported_files=[],
                error=f"beets rc={rc}: {last_lines[:500]}",
            )

        imported = _recent_beets_imports(staging_dir)
        if not imported:
            return BeetsResult(
                ok=False,
                imported_files=[],
                error="Could not tag any uploaded files — beets was unable to match them "
                "to a MusicBrainz record",
            )

        return BeetsResult(ok=True, imported_files=imported)

    finally:
        # ---- clean up temp config -------------------------------------------
        try:
            temp_config.unlink(missing_ok=True)
        except Exception:
            pass
