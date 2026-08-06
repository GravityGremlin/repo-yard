"""Tidal auth routes — device login page, OAuth polling, token status, backup restore."""

from __future__ import annotations

import json as _json
import logging
from pathlib import Path

from flask import Blueprint, jsonify, render_template

from app.config import TIDAL_CONFIG_DIR
from app.tidal.session import (
    start_device_login,
    check_login_status,
    get_token_expiry_info,
    token_exists,
)

logger = logging.getLogger(__name__)

bp = Blueprint("tidal", __name__, url_prefix="/tidal")

# ── Backup token paths (mirrors entrypoint.sh) ─────────────────────
_BACKUP_PATHS = [
    Path("/opt/backups/tidalwave-auth/token.json.latest"),
    Path("/mnt/backups/cthulhu/tidalwave-auth/token.json.latest"),
]


@bp.route("/auth")
def auth_page():
    """Show the Tidal auth/login page."""
    info = get_token_expiry_info()
    connected = token_exists() and info.get("valid", False)
    return render_template(
        "auth.html",
        connected=connected,
        token_info=info,
    )


@bp.route("/auth/start", methods=["POST"])
def auth_start():
    """Start a device login flow. Returns JSON with URL + device code."""
    result = start_device_login()
    return jsonify(result)


@bp.route("/auth/status/<device_code>")
def auth_status(device_code: str):
    """Poll login status for a device code."""
    result = check_login_status(device_code)
    return jsonify(result)


@bp.route("/status")
def status():
    """Return Tidal connection status as JSON."""
    # Attempt to create/refresh session — this will auto-refresh an expired token
    from app.tidal.session import get_session
    session = get_session()
    if session is not None:
        # Session created or refreshed successfully
        info = get_token_expiry_info()
        return jsonify({
            "connected": True,
            "expires_in": info.get("expires_in"),
            "valid": info.get("valid", False),
        })
    info = get_token_expiry_info()
    return jsonify({
        "connected": False,
        "expires_in": info.get("expires_in"),
        "valid": info.get("valid", False),
    })


@bp.route("/auth/restore", methods=["POST"])
def auth_restore():
    """Attempt to restore Tidal auth token from backup and refresh it.

    Reads the most recent backup token, copies it to the live location,
    and tries to refresh the access token.  Returns JSON with the result.
    """
    # ── Find valid backup ─────────────────────────────────────────
    backup_src: Path | None = None
    for p in _BACKUP_PATHS:
        if p.is_file():
            try:
                raw = _json.loads(p.read_text())
            except (_json.JSONDecodeError, OSError):
                continue
            if raw.get("refresh_token"):
                backup_src = p
                break

    if backup_src is None:
        logger.warning("Restore requested but no valid backup token found")
        return jsonify({"error": "No valid backup token found on disk", "connected": False}), 404

    # ── Copy backup to live location ───────────────────────────────
    live_token = TIDAL_CONFIG_DIR / "token.json"
    live_token.parent.mkdir(parents=True, exist_ok=True)
    try:
        live_token.write_text(backup_src.read_text())
        logger.info("Token restored from backup: %s", backup_src)
    except OSError as exc:
        logger.exception("Failed to copy backup token to live location")
        return jsonify({"error": f"Failed to write token file: {exc}", "connected": False}), 500

    # ── Refresh the restored token ─────────────────────────────────
    from app.tidal.session import get_session, get_token_expiry_info
    session = get_session()
    if session is not None:
        info = get_token_expiry_info()
        logger.info("Auth restored successfully — token valid for %.0fs", info.get("expires_in", 0))
        return jsonify({
            "connected": True,
            "expires_in": info.get("expires_in"),
            "valid": info.get("valid", False),
            "message": "Auth restored and token refreshed successfully",
        })

    # Token copied but refresh failed — backup refresh_token may be stale
    logger.warning("Token restored from backup but refresh failed — may need re-authentication")
    return jsonify({
        "error": "Token restored from backup but refresh failed — the backup refresh token may have expired. Try re-authenticating.",
        "connected": False,
    })
