"""Audit log routes — browse recent events."""
from __future__ import annotations

import logging

from flask import Blueprint, render_template

from app.models import list_audit

logger = logging.getLogger(__name__)

bp = Blueprint("audit", __name__, url_prefix="/audit")


@bp.route("/")
def index():
    """Audit log page."""
    events = list_audit(limit=100)
    return render_template("audit.html", events=events)


@bp.route("/list")
def list_partial():
    """Audit log as HTMX partial (for polling)."""
    events = list_audit(limit=100)
    return render_template("partials/audit_list.html", events=events)
