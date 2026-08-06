"""repo-yard — unified cross-service search aggregator.

Queries the four quad tools (spotifryer, qoochie, tidalwave, deeznutz) over
HTTP, normalizes their results to one schema, deduplicates by ISRC, and serves
a unified HTMX search UI. No tool code is imported or merged — coupling is
purely over the /search/json HTTP contract.
"""

import os

from flask import Flask


def _duration_filter(ms):
    """Milliseconds → m:ss for the results list."""
    if not ms:
        return ""
    total = int(ms) // 1000
    return f"{total // 60}:{total % 60:02d}"


def create_app() -> Flask:
    app = Flask(__name__)
    app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "repo-yard-dev")
    app.jinja_env.filters["duration"] = _duration_filter

    from .search_aggregator import yard_bp

    app.register_blueprint(yard_bp)
    return app
