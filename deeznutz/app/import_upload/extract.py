"""Archive extraction for import uploads — zip, tar, tar.gz with safety guards."""

from __future__ import annotations

import logging
import tarfile
import zipfile
from pathlib import Path

from app.config import IMPORT_ALLOWED_EXTS, IMPORT_ARCHIVE_EXTS

logger = logging.getLogger(__name__)

# Maximum total uncompressed size allowed (bytes).
# Uses a 4× multiplier to account for archive expansion.
# Recomputed at extraction time so runtime config changes take effect.
_UNCOMPRESSED_MULTIPLIER = 4


def _max_uncompressed_bytes() -> int:
    """Return the current uncompressed size limit in bytes.

    Reads ``IMPORT_MAX_UPLOAD_MB`` from ``app.config`` each call so
    runtime changes to the config value are respected immediately.
    """
    from app.config import IMPORT_MAX_UPLOAD_MB as _max_mb
    return _max_mb * _UNCOMPRESSED_MULTIPLIER * 1024 * 1024


def _is_safe_member(name: str) -> bool:
    """Check that a member name does not attempt path traversal.

    Rejects names that contain ``..`` path components or are absolute paths
    (starting with ``/``).
    """
    if name.startswith("/"):
        return False
    # Split on '/' and check for '..' components (handles both zip and tar
    # separators since they both use '/' internally).
    parts = name.split("/")
    if ".." in parts:
        return False
    return True


_ALLOWED_EXTS: frozenset[str] = frozenset(
    ext
    for entry in IMPORT_ALLOWED_EXTS
    for ext in entry.split()
)


def _is_audio_file(name: str) -> bool:
    """Return True if *name* has an allowed audio extension (case-insensitive)."""
    return Path(name).suffix.lower() in _ALLOWED_EXTS


def _cleanup(extracted: list[Path]) -> None:
    """Remove all files tracked in *extracted*, ignoring errors."""
    for p in extracted:
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass


def _extract_zip(archive_path: Path, dest_dir: Path) -> list[Path]:
    """Extract audio files from a ZIP archive."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    extracted: list[Path] = []

    try:
        with zipfile.ZipFile(archive_path, "r") as zf:
            # ── Size guard ────────────────────────────────────────
            total = sum(info.file_size for info in zf.infolist())
            if total > _max_uncompressed_bytes():
                raise ValueError(
                    f"Archive too large: {total} bytes uncompressed exceeds "
                    f"limit of {_max_uncompressed_bytes()} bytes"
                )

            # ── Extract audio members ─────────────────────────────
            for info in zf.infolist():
                name = info.filename

                if not _is_safe_member(name):
                    raise ValueError(
                        f"Path traversal detected in archive: {name}"
                    )

                if not _is_audio_file(name):
                    continue

                zf.extract(info, dest_dir)
                extracted.append(dest_dir / name)

        return extracted

    except zipfile.BadZipFile as e:
        _cleanup(extracted)
        raise ValueError(f"Corrupted or invalid archive: {e}") from e
    except Exception:
        _cleanup(extracted)
        raise


def _extract_tar(archive_path: Path, dest_dir: Path) -> list[Path]:
    """Extract audio files from a TAR or TAR.GZ archive."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    extracted: list[Path] = []

    try:
        with tarfile.open(archive_path, "r") as tf:
            # ── Size guard ────────────────────────────────────────
            members = tf.getmembers()
            total = sum(info.size for info in members if info.isfile())
            if total > _max_uncompressed_bytes():
                raise ValueError(
                    f"Archive too large: {total} bytes uncompressed exceeds "
                    f"limit of {_max_uncompressed_bytes()} bytes"
                )

            # ── Extract audio members ─────────────────────────────
            for info in members:
                name = info.name

                # Skip symlinks ─ warn and continue
                if info.islnk() or info.issym():
                    logger.warning("Skipping symlink in archive: %s", name)
                    continue

                if not _is_safe_member(name):
                    raise ValueError(
                        f"Path traversal detected in archive: {name}"
                    )

                if not _is_audio_file(name):
                    continue

                # Ensure parent directory exists for nested paths
                target = dest_dir / name
                target.parent.mkdir(parents=True, exist_ok=True)

                tf.extract(info, dest_dir, set_attrs=False)
                extracted.append(target)

        return extracted

    except tarfile.TarError as e:
        _cleanup(extracted)
        raise ValueError(f"Corrupted or invalid archive: {e}") from e
    except Exception:
        _cleanup(extracted)
        raise


def extract_archive(archive_path: Path, dest_dir: Path) -> list[Path]:
    """Detect format by extension and safely extract audio files to *dest_dir*.

    Supported formats:
        - ``.zip`` (via :mod:`zipfile`)
        - ``.tar`` and ``.tar.gz`` (via :mod:`tarfile`)

    Parameters
    ----------
    archive_path:
        Path to the uploaded archive file.
    dest_dir:
        Directory into which audio files are extracted.

    Returns
    -------
    list[Path]
        Paths of extracted audio files within *dest_dir*.

    Raises
    ------
    ValueError
        If the format is unsupported, the archive is corrupt, a path-traversal
        attempt is detected, or the total uncompressed size exceeds the limit.
    """
    suffix = archive_path.suffix.lower()

    if suffix == ".zip":
        return _extract_zip(archive_path, dest_dir)

    if suffix == ".tar" or archive_path.name.lower().endswith(".tar.gz"):
        return _extract_tar(archive_path, dest_dir)

    raise ValueError(
        f"Unsupported archive format: {archive_path.suffix}. "
        f"Accepted: {IMPORT_ARCHIVE_EXTS}"
    )
