"""Spotify auth routes — login page, OAuth callback, status, logout."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from flask import (
    Blueprint,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)

from app.config import SPOTIFY_CONFIG_DIR
from app.spotify.session import (
    SpotifyAuthError,
    exchange_code,
    get_auth_url,
    is_authenticated,
)

logger = logging.getLogger(__name__)

bp = Blueprint("spotify", __name__, url_prefix="/spotify")

_token_file: Path = SPOTIFY_CONFIG_DIR / "spotify_token.json"

_BACKUP_PATHS = [
    Path("/opt/backups/spotifryer-config/spotify_token.json"),
]


@bp.route("/auth")
def auth_page():
    """Show the Spotify auth page. Redirect home if already authenticated."""
    if is_authenticated():
        return redirect(url_for("search.index"))
    return render_template("auth.html", connected=False)


@bp.route("/auth/start")
def auth_start():
    """Return the OAuth authorization URL for the user to visit."""
    try:
        url = get_auth_url()
        return redirect(url)
    except SpotifyAuthError as exc:
        logger.error("Cannot start Spotify auth: %s", exc)
        flash(f"Cannot connect to Spotify: {exc}", "error")
        return redirect(url_for("spotify.auth_page"))


@bp.route("/auth/callback")
def auth_callback():
    """Handle the OAuth callback — exchange code for tokens."""
    code = request.args.get("code")
    error = request.args.get("error")
    if error:
        flash(f"Spotify authorization failed: {error}", "error")
        return redirect(url_for("spotify.auth_page"))
    if not code:
        flash("No authorization code received", "error")
        return redirect(url_for("spotify.auth_page"))

    try:
        exchange_code(code)
        flash("Successfully connected to Spotify", "success")
        return redirect(url_for("search.index"))
    except SpotifyAuthError as exc:
        logger.error("Spotify token exchange failed: %s", exc)
        flash(f"Token exchange failed: {exc}", "error")
        return redirect(url_for("spotify.auth_page"))


@bp.route("/auth/status")
def auth_status():
    """Return whether Spotify is authenticated."""
    return jsonify({"authenticated": is_authenticated()})


@bp.route("/auth/logout", methods=["POST"])
def auth_logout():
    """Delete the cached token file."""
    try:
        if _token_file.exists():
            _token_file.unlink()
            logger.info("Spotify token file deleted")
        return jsonify({"ok": True})
    except OSError as exc:
        logger.error("Failed to delete token file: %s", exc)
        return jsonify({"ok": False, "error": str(exc)}), 500


@bp.route("/auth/restore", methods=["POST"])
def auth_restore():
    """Restore Spotify auth from a backup token file."""
    backup_src: Path | None = None
    for p in _BACKUP_PATHS:
        if p.is_file():
            try:
                raw = json.loads(p.read_text())
            except (json.JSONDecodeError, OSError):
                continue
            if raw.get("access_token") and raw.get("refresh_token"):
                backup_src = p
                break

    if backup_src is None:
        logger.warning("No valid backup token found for restore")
        return jsonify({
            "error": "No valid backup token found",
            "connected": False,
        }), 404

    try:
        _token_file.parent.mkdir(parents=True, exist_ok=True)
        _token_file.write_text(backup_src.read_text())
        logger.info("Spotify token restored from %s", backup_src)
    except OSError as exc:
        logger.error("Failed to write restored token: %s", exc)
        return jsonify({
            "error": f"Failed to write token: {exc}",
            "connected": False,
        }), 500

    connected = is_authenticated()
    return render_template("partials/auth_badge.html", connected=connected)
