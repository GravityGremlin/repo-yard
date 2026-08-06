"""Library scan — browse the real music library, show recent additions."""

from __future__ import annotations

import io
import logging
import os
import time
import zipfile
from datetime import datetime
from pathlib import Path

from flask import Blueprint, render_template, send_file, abort, Response, request

from app.config import LIBRARY_DIR
from app.library.scan_cache import _AUDIO_EXTS, get_cached
from app.security import safe_resolve

logger = logging.getLogger(__name__)

bp = Blueprint("library", __name__, url_prefix="/library")

_search_cache: dict = {"query": "", "results": [], "timestamp": 0}
_SEARCH_CACHE_TTL = 30  # seconds


@bp.route("/")
def index():
    """Library page shell — tabs load content via HTMX."""
    return render_template("library.html")


@bp.route("/browse/")
@bp.route("/browse/<path:subpath>")
def browse(subpath: str = ""):
    """Directory listing as an HTMX partial."""
    root = LIBRARY_DIR
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass

    target = safe_resolve(root, subpath)
    if target is None:
        return render_template("partials/error.html",
                               message="Path outside allowed directory."), 403
    if not target.exists() or not target.is_dir():
        return render_template("partials/error.html", message="Path not found."), 404

    items = []
    try:
        entries = sorted(target.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower()))
    except OSError:
        return render_template("partials/error.html", message="Cannot read directory."), 500

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
            "size": stat.st_size if entry.is_file() else 0,
            "modified": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M"),
        })

    crumbs = []
    parts = subpath.split("/") if subpath else []
    for i, part in enumerate(parts):
        crumbs.append({"name": part, "path": "/".join(parts[:i + 1])})

    return render_template("partials/library_items.html", items=items,
                           crumbs=crumbs, current_path=subpath, recent=False)


@bp.route("/serve/<path:subpath>")
def serve_file(subpath: str):
    """Stream a file from the music library."""
    target = safe_resolve(LIBRARY_DIR, subpath)
    if target is None or not target.is_file():
        abort(404)
    return send_file(target, as_attachment=False, conditional=True)


@bp.route("/recent")
def recent():
    """Most recently modified audio files from cached scan."""
    data = get_cached("recent")
    if data is None:
        return render_template("partials/library_items.html", items=[], crumbs=[],
                               current_path="", recent=True, scanning=True)
    return render_template("partials/library_items.html", items=data["items"], crumbs=[],
                           current_path="", recent=True, scanning=False)

@bp.route("/search")
def library_search():
    """Full-text search within the downloaded library."""
    global _search_cache
    query = request.args.get("q", "").strip().lower()
    if not query or len(query) < 2:
        return render_template("partials/library_search.html", query=query, results=[])
    # Return cached results if query unchanged and within TTL
    now = time.time()
    if query == _search_cache["query"] and (now - _search_cache["timestamp"]) < _SEARCH_CACHE_TTL:
        return render_template("partials/library_search.html", query=query, results=_search_cache["results"])
    root = LIBRARY_DIR
    if not root.exists():
        return render_template("partials/library_search.html", query=query, results=[])
    results = []
    for ext in _AUDIO_EXTS:
        for audio_path in root.rglob(f"*{ext}"):
            if query in str(audio_path).lower():
                rel = audio_path.relative_to(root)
                parts = rel.parts
                if len(parts) >= 3:
                    artist, album, filename = parts[0], parts[1], parts[2]
                    track_name = filename.split(" - ", 1)[-1].rsplit(".", 1)[0]
                    results.append({"artist": artist, "album": album, "track": track_name, "path": str(rel), "size": audio_path.stat().st_size})
                    if len(results) >= 200:
                        break
            if len(results) >= 200:
                break
    _search_cache = {"query": query, "results": results, "timestamp": time.time()}
    return render_template("partials/library_search.html", query=query, results=results)


@bp.route("/download/<path:subpath>")
def download_zip(subpath: str):
    """Stream a directory as a zip file."""
    target = safe_resolve(LIBRARY_DIR, subpath)
    if target is None or not target.is_dir():
        abort(404)

    # Pre-flight size check: abort early if the directory is too large to
    # buffer as a ZIP in memory (avoid OOM).
    _MAX_ZIP_BYTES = 2 * 1024**3  # 2 GB
    total_size = 0
    for _root, _dirs, files in os.walk(target):
        for _file in files:
            try:
                total_size += (Path(_root) / _file).stat().st_size
            except OSError:
                continue
            if total_size > _MAX_ZIP_BYTES:
                abort(413)

    def generate_zip():
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
            for root, _dirs, files in os.walk(target):
                for file in files:
                    file_path = Path(root) / file
                    arcname = str(file_path.relative_to(target))
                    zf.write(file_path, arcname)
        buf.seek(0)
        while chunk := buf.read(8192):
            yield chunk

    zip_name = target.name + ".zip"
    return Response(
        generate_zip(),
        mimetype='application/zip',
        headers={'Content-Disposition': f'attachment; filename="{zip_name}"'}
    )
