"""Audit log routes — browse recent download events."""
from __future__ import annotations

import logging

from flask import Blueprint, render_template, request, jsonify

from app.models import list_audit
from app.spotify.session import is_authenticated

logger = logging.getLogger(__name__)

audit_bp = Blueprint("audit", __name__, url_prefix="/audit")


def _require_auth():
    """Return None if authenticated, otherwise a 401 JSON response."""
    if not is_authenticated():
        return jsonify({"error": "not_authenticated"}), 401
    return None


@audit_bp.route("/")
def index():
    """Audit log page — most recent 100 events, optional job_id filter."""
    auth_err = _require_auth()
    if auth_err:
        return auth_err
    job_id = request.args.get("job_id", "").strip()
    events = list_audit(limit=100, job_id=job_id or None)
    return render_template("audit.html", events=events, job_id=job_id, limit=100, offset=0)


@audit_bp.route("/list")
def list_partial():
    """Audit log as HTMX partial (for polling and filtering)."""
    auth_err = _require_auth()
    if auth_err:
        return auth_err
    job_id = request.args.get("job_id", "").strip() or None
    offset = request.args.get("offset", 0, type=int)
    limit = request.args.get("limit", 50, type=int)
    events = list_audit(limit=limit, offset=offset, job_id=job_id)
    return render_template(
        "partials/audit_list.html",
        events=events,
        offset=offset,
        limit=limit,
        job_id=job_id or "",
    )
