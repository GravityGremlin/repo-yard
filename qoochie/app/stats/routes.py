"""Stats dashboard — library stats, queue summary, inline config toggles."""

from __future__ import annotations

import logging
from pathlib import Path

from flask import Blueprint, render_template, request

from app.library.scan_cache import get_cached

from app.config import (
    MAX_CONCURRENT,
    ORGANIZE_WITH_BEETS,
    QOBUZ_QUALITY,
    _as_bool,
)
from app.config import DEFAULT_OVERRIDE_EXISTING
from app.models import JobStatus, _connect

logger = logging.getLogger(__name__)

bp = Blueprint("stats", __name__, url_prefix="/stats")

# Config keys the dashboard reads/writes. Names map to keys in qoochie.yaml
# and the env-var overrides (see app.config).
_CONFIG_KEYS = ("qobuz.quality", "qobuz.max_concurrent",
                "downloads.organize_with_beets", "downloads.override_existing")

# Quality options exposed by the UI.
QUALITY_OPTIONS = ("LOW", "HIGH", "LOSSLESS", "HIRES")


def _library_stats() -> dict[str, int]:
    """Return library stats from the background scan cache.

    Uses the same 5-min TTL cache that powers the library scanner,
    avoiding a full rglib walk on every HTMX auto-refresh.
    If no scan has completed yet (cache is None), returns empty stats.
    """
    cached = get_cached("stats")
    if cached is None:
        return {"tracks": 0, "albums": 0, "artists": 0, "bytes": 0}
    return cached


def _queue_stats() -> dict[str, int]:
    """Count jobs per status using a direct SQL GROUP BY query."""
    counts = {JobStatus.QUEUED: 0, JobStatus.RUNNING: 0,
              JobStatus.COMPLETED: 0, JobStatus.ERROR: 0,
              JobStatus.CANCELLED: 0, JobStatus.PAUSED: 0}
    try:
        db = _connect()
        rows = db.execute(
            "SELECT json_extract(data, '$.status') AS status, COUNT(*) AS cnt "
            "FROM jobs GROUP BY status"
        ).fetchall()
        for row in rows:
            status = row["status"]
            if status in counts:
                counts[status] = row["cnt"]
    except Exception as exc:  # noqa: BLE001 — DB may be mid-migration
        logger.warning("queue stats failed: %s", exc, exc_info=True)
    return counts


def _current_config() -> dict[str, object]:
    """Snapshot of current editable config values for the dashboard."""
    return {
        "quality": QOBUZ_QUALITY,
        "max_concurrent": MAX_CONCURRENT,
        "organize_with_beets": ORGANIZE_WITH_BEETS,
        "override_existing": DEFAULT_OVERRIDE_EXISTING,
    }


@bp.route("/")
def index():
    """Render the stats dashboard shell. Content loads via HTMX partials."""
    return render_template("stats.html", config=_current_config(),
                           quality_options=QUALITY_OPTIONS)


@bp.route("/partial")
def partial():
    """HTMX partial: combined library + queue stats cards."""
    return render_template("partials/stats_partial.html",
                           library=_library_stats(),
                           queue=_queue_stats())


@bp.route("/partial/config")
def partial_config():
    """HTMX partial: just the config form, post-update confirmation."""
    return render_template("partials/stats_config.html",
                           config=_current_config(),
                           quality_options=QUALITY_OPTIONS)


@bp.route("/api/config", methods=["PUT"])
def update_config():
    """Update configuration from the dashboard form.

    Accepts form-encoded data with keys: quality, max_concurrent,
    organize_with_beets, override_existing.  Returns the refreshed
    config partial with success/error feedback.
    """
    from app.config import (
        QOBUZ_QUALITY, MAX_CONCURRENT, ORGANIZE_WITH_BEETS, DEFAULT_OVERRIDE_EXISTING,
    )

    form = request.form
    quality = form.get("quality", QOBUZ_QUALITY)
    try:
        max_concurrent = int(form.get("max_concurrent", MAX_CONCURRENT))
    except (TypeError, ValueError):
        max_concurrent = MAX_CONCURRENT
    organize_with_beets = _as_bool(form.get("organize_with_beets", ORGANIZE_WITH_BEETS))
    override_existing = _as_bool(form.get("override_existing", DEFAULT_OVERRIDE_EXISTING))

    # Write back to YAML
    try:
        _update_yaml({
            "qobuz.quality": quality,
            "qobuz.max_concurrent": max_concurrent,
            "downloads.organize_with_beets": organize_with_beets,
            "downloads.override_existing": override_existing,
        })
        save_message = "✓ Config saved"
    except Exception as exc:
        return render_template("partials/stats_config.html",
                               config=_current_config(),
                               quality_options=QUALITY_OPTIONS,
                               error=str(exc))

    # Return updated config partial
    return render_template("partials/stats_config.html",
                           config={
                               "quality": quality,
                               "max_concurrent": max_concurrent,
                               "organize_with_beets": organize_with_beets,
                               "override_existing": override_existing,
                           },
                           quality_options=QUALITY_OPTIONS,
                           saved=True,
                           save_message=save_message)


def _update_yaml(updates: dict[str, object]) -> None:
    """Write key=value pairs back to the YAML config file.

    Keys use dot notation (e.g. ``downloads.organize_with_beets``).
    """
    import yaml
    from app.config import CONFIG_PATH

    path = Path(CONFIG_PATH)
    data: dict = {}
    if path.exists():
        with open(path) as f:
            data = yaml.safe_load(f) or {}

    for key, value in updates.items():
        parts = key.split(".")
        target = data
        for part in parts[:-1]:
            target = target.setdefault(part, {})
        target[parts[-1]] = value

    path.write_text(yaml.dump(data, default_flow_style=False, sort_keys=True))
