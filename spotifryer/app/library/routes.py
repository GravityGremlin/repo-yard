"""Library routes — browse the music library, serve audio files."""

from __future__ import annotations

import logging
import mimetypes

from flask import Blueprint, abort, render_template, send_file, jsonify

from app.config import LIBRARY_DIR
from app.library.scan_cache import _AUDIO_EXTS
from app.security import safe_resolve
from app.spotify.session import is_authenticated

logger = logging.getLogger(__name__)

library_bp = Blueprint("library", __name__, url_prefix="/library")

_AUDIO_MIME: dict[str, str] = {
    ".flac": "audio/flac",
    ".m4a": "audio/mp4",
    ".mp3": "audio/mpeg",
    ".opus": "audio/ogg",
    ".ogg": "audio/ogg",
    ".wav": "audio/wav",
    ".aac": "audio/aac",
}


def _require_auth():
    """Return None if authenticated, otherwise a 401 JSON response."""
    if not is_authenticated():
        return jsonify({"error": "not_authenticated"}), 401
    return None


@library_bp.route("/")
def index():
    """Library page shell — tabs load content via HTMX."""
    return render_template("library.html")


@library_bp.route("/browse/")
@library_bp.route("/browse/<path:subpath>")
def browse(subpath: str = ""):
    """Directory listing as JSON."""
    auth_err = _require_auth()
    if auth_err:
        return auth_err
    root = LIBRARY_DIR
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass

    target = safe_resolve(root, subpath)
    if target is None:
        return jsonify({"error": "Path outside allowed directory."}), 403
    if not target.exists() or not target.is_dir():
        return jsonify({"error": "Path not found."}), 404

    items = []
    try:
        entries = sorted(target.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower()))
    except OSError:
        return jsonify({"error": "Cannot read directory."}), 500

    for entry in entries:
        if entry.name.startswith("."):
            continue
        try:
            stat = entry.stat()
        except OSError:
            continue

        items.append({
            "name": entry.name,
            "is_dir": entry.is_dir(),
            "size": stat.st_size if entry.is_file() else None,
            "mtime": stat.st_mtime,
            "ext": entry.suffix.lower() if entry.is_file() else None,
        })

    return jsonify({"path": subpath, "items": items})


@library_bp.route("/serve/<path:filepath>")
def serve(filepath: str):
    """Stream an audio file."""
    auth_err = _require_auth()
    if auth_err:
        return auth_err

    root = LIBRARY_DIR
    target = safe_resolve(root, filepath)
    if target is None:
        abort(403)
    if not target.exists() or not target.is_file():
        abort(404)

    ext = target.suffix.lower()
    if ext not in _AUDIO_MIME:
        abort(404)

    mime = _AUDIO_MIME.get(ext, mimetypes.guess_type(target.name)[0] or "application/octet-stream")
    return send_file(str(target), mimetype=mime)


@library_bp.route("/cover/<path:filepath>")
def cover(filepath: str):
    """Serve embedded cover art extracted from an audio file."""
    auth_err = _require_auth()
    if auth_err:
        return auth_err

    root = LIBRARY_DIR
    target = safe_resolve(root, filepath)
    if target is None:
        abort(403)
    if not target.exists() or not target.is_file():
        abort(404)

    try:
        from mutagen.mp4 import MP4
        from mutagen.flac import FLAC
        from mutagen.mp3 import MP3
        from mutagen.oggvorbis import OggVorbis

        ext = target.suffix.lower()
        audio = None
        if ext == ".m4a":
            audio = MP4(str(target))
            if audio.get("covr"):
                cover_data = audio["covr"][0]
                mime = "image/jpeg"  # MP4 cover art is typically JPEG
            else:
                abort(404)
        elif ext == ".flac":
            audio = FLAC(str(target))
            if audio.pictures:
                pic = audio.pictures[0]
                cover_data = pic.data
                mime = pic.mime
            else:
                abort(404)
        elif ext == ".mp3":
            audio = MP3(str(target))
            if audio.get("APIC"):
                apic = audio["APIC"].data if hasattr(audio["APIC"], "data") else audio["APIC"][0].data
                cover_data = apic
                mime = "image/jpeg"
            else:
                abort(404)
        elif ext in (".ogg", ".opus"):
            audio = OggVorbis(str(target))
            if audio.get("METADATA_BLOCK_PICTURE"):
                # Vorbis comment picture block — decode first picture
                import base64
                from mutagen.flac import Picture
                b64_data = audio["METADATA_BLOCK_PICTURE"][0]
                pic = Picture()
                pic.parse(base64.b64decode(b64_data))
                cover_data = pic.data
                mime = pic.mime
            else:
                abort(404)
        else:
            abort(404)

        from flask import Response
        return Response(cover_data, mimetype=mime)
    except ImportError:
        logger.warning("mutagen not installed — cannot extract cover art")
        abort(501)
    except Exception:
        logger.warning("Failed to extract cover art from %s", filepath, exc_info=True)
        abort(404)


@library_bp.route("/recent")
def recent():
    """Return JSON list of recently completed jobs with file info."""
    from app.models import list_jobs, JobStatus

    jobs = list_jobs(limit=100)
    recent_jobs = [j for j in jobs if j.status == JobStatus.COMPLETED][:20]
    return jsonify([j.to_dict() for j in recent_jobs])


@library_bp.route("/download/")
@library_bp.route("/download/<path:subpath>")
def download_zip(subpath: str = ""):
    """Stream the entire library (or a subfolder) as a zip file.

    Uses a temp file on disk to avoid OOM with large libraries.
    """
    import os
    import tempfile
    import zipfile

    root = LIBRARY_DIR
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass

    if subpath:
        target = safe_resolve(root, subpath)
        if target is None:
            abort(403)
        if not target.exists() or not target.is_dir():
            abort(404)
    else:
        target = root

    # Build a zip of audio files only, streamed via a temp file
    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".zip", prefix="spotifryer-lib-")
    os.close(tmp_fd)
    try:
        with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_STORED) as zf:
            for audio_ext in _AUDIO_EXTS:
                for f in target.rglob(f"*{audio_ext}"):
                    arcname = str(f.relative_to(root))
                    zf.write(f, arcname)
        fname = f"spotifryer-{subpath.replace('/', '-') or 'library'}.zip"
        return send_file(
            tmp_path,
            mimetype="application/zip",
            as_attachment=True,
            download_name=fname,
        )
    except Exception:
        logger.error("Failed to create library zip", exc_info=True)
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        abort(500)
