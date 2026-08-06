"""Stats dashboard — library stats, queue summary, inline config toggles, batch convert."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import yaml
from flask import Blueprint, render_template, request, jsonify

from app.library.scan_cache import get_cached_stats
from app.config import (
    CONFIG_PATH,
    ORGANIZE_WITH_BEETS,
    _as_bool,
    _config,
)
from app.models import JobStatus, _connect

logger = logging.getLogger(__name__)

stats_bp = Blueprint("stats", __name__, url_prefix="/stats")

# Config keys the dashboard reads/writes.
_CONFIG_KEYS = ("downloads.organize_with_beets", "downloads.override_existing")


def _library_stats() -> dict[str, int]:
    """Return library stats from the background scan cache."""
    cached = get_cached_stats()
    if cached is None:
        return {"tracks": 0, "albums": 0, "artists": 0, "bytes": 0}
    return cached


def _queue_stats() -> dict[str, int]:
    """Count jobs per status using a direct SQL GROUP BY query."""
    counts = {s: 0 for s in (JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.COMPLETED,
                               JobStatus.ERROR, JobStatus.CANCELLED, JobStatus.PAUSED)}
    try:
        db = _connect()
        rows = db.execute(
            "SELECT json_extract(data, '$.status') AS st, COUNT(*) AS cnt "
            "FROM jobs GROUP BY st"
        ).fetchall()
        for row in rows:
            st = row["st"]
            cnt = row["cnt"]
            if st in counts:
                counts[st] = cnt
    except Exception:
        logger.warning("Failed to query queue stats", exc_info=True)
    return counts


def _collect_stats() -> dict:
    """Aggregate all stats for the dashboard."""
    lib = _library_stats()
    q = _queue_stats()
    total_bytes = lib.get("bytes", 0)

    # Total bytes downloaded across all completed jobs
    try:
        db = _connect()
        rows = db.execute(
            "SELECT data FROM jobs WHERE json_extract(data, '$.status') = 'completed'"
        ).fetchall()
        for row in rows:
            jdata = json.loads(row["data"])
            total_bytes += sum(f.get("size", 0) for f in jdata.get("files", []) if isinstance(f, dict))
    except Exception:
        logger.warning("Failed to query completed jobs stats", exc_info=True)

    return {
        "tracks": lib.get("tracks", 0),
        "albums": lib.get("albums", 0),
        "artists": lib.get("artists", 0),
        "total_bytes": total_bytes,
        "queued": q.get(JobStatus.QUEUED, 0),
        "running": q.get(JobStatus.RUNNING, 0),
        "paused": q.get(JobStatus.PAUSED, 0),
        "completed": q.get(JobStatus.COMPLETED, 0),
        "failed": q.get(JobStatus.ERROR, 0),
        "cancelled": q.get(JobStatus.CANCELLED, 0),
    }


def _current_config() -> dict:
    """Snapshot of current editable config values for the dashboard."""
    return {
        "organize_with_beets": ORGANIZE_WITH_BEETS,
    }


def _update_yaml(key: str, value) -> None:
    """Persist a config change to spotifryer.yaml (merge, not overwrite)."""
    p = Path(CONFIG_PATH)
    try:
        if p.exists():
            with open(p) as f:
                data = yaml.safe_load(f) or {}
        else:
            data = {}
    except Exception:
        logger.warning("Failed to read YAML config for update", exc_info=True)
        data = {}

    # Set nested key
    parts = key.split(".")
    cfg = data
    for part in parts[:-1]:
        if not isinstance(cfg, dict) or part not in cfg:
            if not isinstance(cfg, dict):
                cfg = {}
            cfg[part] = {}
        cfg = cfg[part]
    if isinstance(cfg, dict):
        cfg[parts[-1]] = value

    # Write back
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".yaml.tmp")
        with open(tmp, "w") as f:
            yaml.safe_dump(data, f, default_flow_style=False, sort_keys=True)
        os.replace(str(tmp), str(p))
        logger.info("YAML config updated: %s = %s", key, value)
    except Exception:
        logger.error("Failed to write YAML config", exc_info=True)


@stats_bp.route("/")
def index():
    """Stats dashboard page."""
    stats = _collect_stats()
    config = _current_config()
    return render_template("stats.html", stats=stats, config=config)


@stats_bp.route("/refresh")
def refresh():
    """Return fresh stats as JSON (for HTMX polling)."""
    stats = _collect_stats()
    return jsonify(stats)


@stats_bp.route("/config", methods=["POST"])
def update_config():
    """Toggle simple config values at runtime, persisted to YAML."""
    if request.is_json:
        data = request.get_json()
    else:
        data = request.form

    key = (data.get("key") or "").strip()
    value = data.get("value")

    if key not in _CONFIG_KEYS:
        return jsonify({"error": f"Unknown config key: {key}"}), 400

    # Coerce value
    parts = key.split(".")
    const_name = parts[-1].upper()

    if key in ("downloads.organize_with_beets", "downloads.override_existing"):
        coerced = _as_bool(value)
    else:
        coerced = value

    # Update the in-memory config dict
    cfg = _config
    for part in parts[:-1]:
        if part not in cfg:
            cfg[part] = {}
        cfg = cfg[part]
    if isinstance(cfg, dict):
        cfg[parts[-1]] = coerced

    # Also update the module-level constant if it exists
    import app.config as _cfg_module
    if hasattr(_cfg_module, const_name):
        setattr(_cfg_module, const_name, coerced)

    # Persist to YAML
    _update_yaml(key, coerced)

    logger.info("Config updated: %s = %s", key, coerced)

    # Return updated config partial for HTMX swap
    config = _current_config()
    return jsonify({"ok": True, "key": key, "value": coerced,
                    "config": config})


@stats_bp.route("/api/convert-library", methods=["POST"])
def convert_library():
    """Batch convert library files using beets. Returns JSON status."""
    import shutil
    import subprocess

    if not shutil.which("beet"):
        return jsonify({"error": "beet binary not found on PATH"}), 501

    from app.config import LIBRARY_DIR, BEETS_DIR
    beets_config = BEETS_DIR / "config.yaml"
    cmd = ["beet", "convert"]
    if beets_config.exists():
        cmd.extend(["-c", str(beets_config)])
    cmd.append(str(LIBRARY_DIR))

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=600,
        )
        return jsonify({
            "ok": result.returncode == 0,
            "stdout": result.stdout[-2000:] if result.stdout else "",
            "stderr": result.stderr[-2000:] if result.stderr else "",
        })
    except subprocess.TimeoutExpired:
        return jsonify({"error": "beet convert timed out after 600s"}), 504
    except FileNotFoundError:
        return jsonify({"error": "beet binary not found"}), 501
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
