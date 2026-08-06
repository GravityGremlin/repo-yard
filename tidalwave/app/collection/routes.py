"""Collection overview — scan the music library and report summary stats.

Full Tidal-vs-library ISRC/UPC matching remains on the roadmap; this view
gives a live, bounded snapshot of what is actually on disk.
"""

from __future__ import annotations

import logging
import threading

from flask import Blueprint, render_template, request

from app.config import LIBRARY_DIR
from app.library.scan_cache import get_cached, refresh_now

logger = logging.getLogger(__name__)

bp = Blueprint("collection", __name__, url_prefix="/collection")

_CATALOG_LIMIT = 50  # initial artists per page


@bp.route("/")
def index():
    """Library summary statistics from cached scan."""
    stats = get_cached("collection")
    if stats is None:
        stats = {
            "path": str(LIBRARY_DIR),
            "exists": LIBRARY_DIR.exists(),
            "artists": 0, "albums": 0, "tracks": 0,
            "total_bytes": 0, "scanned": 0, "capped": False, "by_ext": {},
            "scanning": True,
        }
    else:
        stats["scanning"] = False
    return render_template("collection.html", stats=stats)


@bp.route("/catalog")
def catalog():
    """Artist→album catalog as an HTMX partial. Paginates 50 artists per call.

    Returns a scanning placeholder on the very first call (cache cold) so the
    browser shows progress while the background walk runs.
    """
    offset = max(0, request.args.get("offset", default=0, type=int))
    data = get_cached("catalog")
    if data is None:
        return render_template("partials/catalog.html", artists=[], offset=offset,
                               limit=_CATALOG_LIMIT, total=0, scanning=True, exists=True)
    full = data["artists"]
    window = full[offset:offset + _CATALOG_LIMIT]
    return render_template("partials/catalog.html", artists=window, offset=offset,
                           limit=_CATALOG_LIMIT, total=len(full), scanning=False,
                           exists=data["exists"])


@bp.route("/refresh", methods=["POST"])
def refresh():
    """Force a background cache refresh."""
    threading.Thread(
        target=lambda: (refresh_now("collection"), refresh_now("recent"), refresh_now("catalog")),
        daemon=True,
    ).start()
    return {"refreshing": True}
