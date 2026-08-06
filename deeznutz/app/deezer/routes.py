"""Deezer auth UI routes — ARL token input (not OAuth)."""

from __future__ import annotations

import logging
import re
from http import HTTPStatus

from flask import Blueprint, render_template, request, jsonify

from app.deezer.session import token_exists, get_token_expiry_info, login_via_arl

_log = logging.getLogger(__name__)

bp = Blueprint("deezer", __name__, url_prefix="/deezer")

# ARL cookie format: hex/alphanumeric string, typically 190+ chars.
# Minimum 20 chars is lenient; typical ARLs are ~192 chars.
_ARL_RE = re.compile(r"^[a-f0-9]{20,}$", re.IGNORECASE)


@bp.route("/auth", methods=["GET"])
def auth_page():
    """Render the Deezer ARL authentication page."""
    connected = token_exists()
    info = get_token_expiry_info() if connected else {}
    return render_template("auth.html", platform="Deezer", connected=connected, info=info)


@bp.route("/auth/validate", methods=["POST"])
def validate_arl():
    """Validate and save a Deezer ARL cookie.

    Expects ``{"arl": "..."}`` in the request body (JSON or form).
    Validates ARL format first, then authenticates against the Deezer API.
    """
    data = request.get_json(silent=True) or request.form
    arl = (data or {}).get("arl", "").strip()
    if not arl:
        return jsonify({"status": "error", "message": "ARL cookie is required"}), HTTPStatus.BAD_REQUEST

    # Structure check: ARL must look like a valid Deezer ARL cookie
    if not _ARL_RE.match(arl):
        return jsonify({
            "status": "error",
            "message": "ARL format is invalid — expected a hex string of at least 20 characters",
        }), HTTPStatus.BAD_REQUEST

    # Live validation: authenticate against Deezer API
    result = login_via_arl(arl)
    if result.get("status") == "ok":
        return jsonify(result)
    return jsonify(result), HTTPStatus.UNAUTHORIZED


@bp.route("/status", methods=["GET"])
def status():
    """Return current Deezer authentication status (JSON)."""
    connected = token_exists()
    info = get_token_expiry_info() if connected else {}
    return jsonify({"connected": connected, "info": info})
