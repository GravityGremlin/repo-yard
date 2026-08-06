"""Stats dashboard — library stats, queue summary, inline config toggles."""

from __future__ import annotations

import logging
from pathlib import Path

from flask import Blueprint, render_template, request

from app.library.scan_cache import get_cached

from app.config import (
    MAX_CONCURRENT,
    ORGANIZE_WITH_BEETS,
    TIDAL_QUALITY,
    _as_bool,
    _config,
)
from app.config import DEFAULT_OVERRIDE_EXISTING
from app.models import JobStatus, _connect

logger = logging.getLogger(__name__)

bp = Blueprint("stats", __name__, url_prefix="/stats")

# Config keys the dashboard reads/writes. Names map to keys in tidalwave.yaml
# and the env-var overrides (see app.config).
_CONFIG_KEYS = ("tidal.quality", "tidal.max_concurrent",
                "downloads.organize_with_beets", "downloads.override_existing")

# Quality options exposed by the UI. Mirrors tidalapi's quality enum.
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
        "quality": TIDAL_QUALITY,
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
                           quality_options=QUALITY_OPTIONS,
                           saved=request.args.get("saved") == "1")


# ── Config API ─────────────────────────────────────────────────────────
# The /api/config endpoint is defined here (not in a separate bundle in this
# build of the repo) so the dashboard toggles have somewhere to POST. It only
# ever mutates tidalwave.yaml; module-level config constants are not re-read,
# so changes take effect on the next request/restart as appropriate. The
# worker pool consults configPOINT values lazily, but explicit MAX_CONCURRENT
# changes require a worker-pool restart and are noted in the UI hint.

@bp.route("/api/config", methods=["PUT", "POST"])
def api_config():
    """Update mutable config keys in tidalwave.yaml.

    Accepts a JSON or form-encoded body whose keys may be any of: quality,
    max_concurrent, organize_with_beets, override_existing.  Returns the
    rendered config partial so HTMX can swap it in with save/error feedback.
    """
    if request.is_json:
        data: dict = request.get_json() or {}
    else:
        data = dict(request.form)

    update_map = {
        "quality": ("tidal.quality", str),
        "max_concurrent": ("tidal.max_concurrent", int),
        "organize_with_beets": ("downloads.organize_with_beets", bool),
        "override_existing": ("downloads.override_existing", bool),
    }
    updates: dict[str, object] = {}
    error: str | None = None
    for key, value in data.items():
        if key not in update_map:
            continue
        yaml_path, cast = update_map[key]
        try:
            if cast is bool:
                value = _as_bool(value)
            elif cast is int:
                value = int(value)
            else:
                value = str(value).strip()
            if yaml_path == "tidal.quality" and value not in QUALITY_OPTIONS:
                error = f"Invalid quality: {value}"
                break
            if yaml_path == "tidal.max_concurrent":
                mc = int(value)
                if mc < 1 or mc > 32:
                    error = "max_concurrent must be 1–32"
                    break
                value = mc
            updates[yaml_path] = value
        except (TypeError, ValueError) as exc:
            error = f"Invalid value for {key}: {exc}"
            break

    if error is not None:
        return render_template("partials/stats_config.html",
                               config=_current_config(),
                               quality_options=QUALITY_OPTIONS,
                               error=error)

    if not updates:
        return render_template("partials/stats_config.html",
                               config=_current_config(),
                               quality_options=QUALITY_OPTIONS,
                               error="No valid config keys provided")

    try:
        _apply_config_updates(updates)
    except OSError as exc:
        logger.error("config write failed: %s", exc)
        return render_template("partials/stats_config.html",
                               config=_current_config(),
                               quality_options=QUALITY_OPTIONS,
                               error="Could not save config — disk write failed")

    logger.info("config updated: %s", ", ".join(updates.keys()))
    return render_template("partials/stats_config.html",
                           config=_current_config(),
                           quality_options=QUALITY_OPTIONS,
                           saved=True)


@bp.route("/api/convert-library", methods=["POST"])
def api_convert_library():
    """Batch-convert non-opus files in the beets library to opus.

    Runs 'beet convert' which respects the convert plugin config
    (opus 160k, auto, delete_originals). Returns rendered config
    partial with status message.
    """
    import subprocess
    from app.config import BEETS_DIR

    try:
        proc = subprocess.run(
            ["beet", "-c", str(BEETS_DIR / "config.yaml"), "convert"],
            capture_output=True, text=True, timeout=600,
        )
        if proc.returncode != 0:
            return render_template("partials/stats_config.html",
                                   config=_current_config(),
                                   quality_options=QUALITY_OPTIONS,
                                   error=f"Conversion failed: {proc.stderr.strip()[:200]}")

        lines = [l for l in proc.stdout.splitlines() if l.strip()]
        count = len(lines)
        # Show first few converted tracks
        preview = "\n".join(lines[:5])
        msg = f"Converted {count} file(s) to opus"
        if count > 5:
            msg += f" (first 5: {preview}...)"
        elif count > 0:
            msg = f"Converted {count} file(s):\n{preview}"

        return render_template("partials/stats_config.html",
                               config=_current_config(),
                               quality_options=QUALITY_OPTIONS,
                               saved=True,
                               save_message=msg)
    except subprocess.TimeoutExpired:
        return render_template("partials/stats_config.html",
                               config=_current_config(),
                               quality_options=QUALITY_OPTIONS,
                               error="Conversion timed out after 10 minutes")
    except FileNotFoundError:
        return render_template("partials/stats_config.html",
                               config=_current_config(),
                               quality_options=QUALITY_OPTIONS,
                               error="beets binary not found")


def _apply_config_updates(updates: dict[str, object]) -> None:
    """Persist dot-path updates into the live _config dict and yaml file."""
    import os
    from app import config as cfg_module

    config_path = cfg_module.CONFIG_PATH
    file_cfg = dict(_config) if isinstance(_config, dict) else {}

    for dotted, value in updates.items():
        parts = dotted.split(".")
        node = file_cfg
        for p in parts[:-1]:
            if not isinstance(node.get(p), dict):
                node[p] = {}
            node = node[p]
        node[parts[-1]] = value

    # Persist to disk so the next process load picks it up.
    import yaml
    config_path = os.fspath(config_path)
    try:
        Path(config_path).parent.mkdir(parents=True, exist_ok=True)
        with open(config_path, "w") as f:
            yaml.safe_dump(file_cfg, f, default_flow_style=False, sort_keys=False)
    except OSError:
        raise

    # Reflect live into the running process's config dict so subsequent
    # _cfg() reads on unchanged keys still see consistent state. Module-level
    # constants (TIDAL_QUALITY etc.) are intentionally NOT rebound here — see
    # docstring on api_config.
    cfg_module._config.clear()
    cfg_module._config.update(file_cfg)
