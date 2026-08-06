"""Qobuz auth UI routes — token input."""

from __future__ import annotations

import logging
from http import HTTPStatus

from flask import Blueprint, render_template, request, jsonify

from app.qobuz.session import token_exists, get_token_expiry_info, login_via_token

_log = logging.getLogger(__name__)

bp = Blueprint("qobuz", __name__, url_prefix="/qobuz")


@bp.route("/auth", methods=["GET"])
def auth_page():
    """Render the Qobuz token authentication page."""
    connected = token_exists()
    info = get_token_expiry_info() if connected else {}
    return render_template("auth.html", platform="Qobuz", connected=connected, info=info)


@bp.route("/auth/validate", methods=["POST"])
def validate_token():
    """Validate and save a Qobuz token.

    Expects ``{"token": "..."}`` in the request body (JSON or form).
    """
    data = request.get_json(silent=True) or request.form
    token = (data or {}).get("token", "").strip()
    if not token:
        return jsonify({"status": "error", "message": "Token is required"}), HTTPStatus.BAD_REQUEST
    if len(token) < 20:
        return jsonify({"status": "error", "message": "Token looks too short"}), HTTPStatus.BAD_REQUEST

    result = login_via_token(token)
    if result.get("status") == "ok":
        return jsonify(result)
    return jsonify(result), HTTPStatus.UNAUTHORIZED


@bp.route("/status", methods=["GET"])
def status():
    """Return current Qobuz authentication status (JSON)."""
    connected = token_exists()
    info = get_token_expiry_info() if connected else {}
    return jsonify({"connected": connected, "info": info})
