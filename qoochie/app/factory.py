"""Application factory — Flask app creation, blueprint registration, startup."""

from __future__ import annotations

import logging
import os
from urllib.parse import urlparse

from flask import Flask, jsonify, render_template, request

from app.logging_config import setup_logging
setup_logging()

_log = logging.getLogger(__name__)

from app.config import (
    __version__,
    _BUILD_TIME,
    DOWNLOAD_DIR,
    JOBS_DIR,
    QOBUZ_CONFIG_DIR,
    PROXY_LIST,
    PROXY_LABELS,
)
from app.qobuz.session import token_exists, get_token_expiry_info

# ── CSRF origin guard for server-to-server exempted endpoints ────
# These endpoints are exempted from SeaSurf CSRF because the repo-yard
# aggregator calls them server-to-server (no browser session cookie).
# The origin guard ensures foreign browser POSTs are still blocked while
# allowing: (a) same-origin requests, (b) repo-yard server requests (no
# Origin/Referer header), and (c) requests from known trusted origins.
_CSRF_EXEMPT_PATHS = frozenset({"/download/enqueue", "/download/discography",
                                  "/playlist/resolve"})
_TRUSTED_NETLOCS = frozenset({
    "10.8.0.10",        # cluster host
    "localhost",        # local dev
    "127.0.0.1",        # loopback
    "ry.n0g.xyz",       # repo-yard public host
})


def _netloc_is_trusted(netloc: str) -> bool:
    """Return True if *netloc* (host or host:port) is in the trusted set."""
    # Strip port for comparison
    host = netloc.split(":")[0] if netloc else ""
    return host in _TRUSTED_NETLOCS


def _csrf_origin_guard() -> None:
    """before_request hook: reject foreign-origin POSTs on CSRF-exempt paths.

    Requests with no Origin/Referer header (server-to-server) pass through.
    Same-origin and trusted-origin requests pass through.
    Foreign-origin POSTs are rejected with 403.
    """
    if request.method not in ("POST", "PUT", "DELETE", "PATCH"):
        return
    if request.path not in _CSRF_EXEMPT_PATHS:
        return

    origin = request.headers.get("Origin", "")
    referer = request.headers.get("Referer", "")

    # No Origin and no Referer → server-to-server, allow
    if not origin and not referer:
        return

    # Check Origin header first (authoritative for CORS requests)
    if origin:
        try:
            parsed = urlparse(origin)
            if _netloc_is_trusted(parsed.netloc):
                return
        except Exception:
            pass
        _log.warning("CSRF origin guard: rejected Origin=%s for %s", origin, request.path)
        from flask import abort
        abort(403)

    # Fall back to Referer check
    if referer:
        try:
            parsed = urlparse(referer)
            if _netloc_is_trusted(parsed.netloc):
                return
        except Exception:
            pass
        _log.warning("CSRF origin guard: rejected Referer=%s for %s", referer, request.path)
        from flask import abort
        abort(403)


def create_app() -> Flask:
    """Flask application factory."""
    app = Flask(__name__)
    app.secret_key = os.environ.get("SECRET_KEY") or os.urandom(32).hex()
    app.config["TEMPLATES_AUTO_RELOAD"] = True

    # CSRF protection — protects all POST/PUT/DELETE routes
    from flask_seasurf import SeaSurf
    csrf = SeaSurf(app)
    # These endpoints are called server-to-server by the repo-yard aggregator
    # without a CSRF token.  /playlist/resolve is read-only; the download
    # endpoints are POST state-changes authorized at the network level.
    csrf.exempt_urls(("/playlist/resolve", "/download/enqueue", "/download/discography"))
    if os.environ.get("CSRF_DISABLE", "").lower() in ("1", "true", "yes"):
        csrf._csrf_disable = True

    # Origin guard for CSRF-exempted endpoints (rejects foreign-origin POSTs)
    app.before_request(_csrf_origin_guard)

    # Ensure directories exist
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    QOBUZ_CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    # Bootstrap a session from QOBUZ_TOKEN env var if provided (container deployments)
    from app.qobuz.session import bootstrap_env_token, ensure_session
    bootstrap_env_token()

    # Ensure a QobuzClient is available on each request
    app.before_request(ensure_session)

    # Register blueprints
    from app.search.routes import bp as search_bp
    from app.download.routes import bp as download_bp
    from app.qobuz.routes import bp as qobuz_bp
    from app.playlist.routes import bp as playlist_bp
    from app.library.routes import bp as library_bp
    from app.collection.routes import bp as collection_bp
    from app.audit.routes import bp as audit_bp
    from app.stats.routes import bp as stats_bp

    app.register_blueprint(search_bp)
    app.register_blueprint(download_bp)
    app.register_blueprint(qobuz_bp)
    app.register_blueprint(playlist_bp)
    app.register_blueprint(library_bp)
    app.register_blueprint(collection_bp)
    app.register_blueprint(audit_bp)
    app.register_blueprint(stats_bp)

    from app.import_upload.routes import bp as import_bp
    app.register_blueprint(import_bp)

    # Template filters
    @app.template_filter("human_size")
    def human_size(n: int) -> str:
        for unit in ("B", "KB", "MB", "GB"):
            if n < 1024:
                return f"{n:.1f} {unit}"
            n /= 1024
        return f"{n:.1f} TB"

    @app.template_filter("duration")
    def format_duration(seconds: int) -> str:
        if not seconds:
            return "—"
        m, s = divmod(int(seconds), 60)
        return f"{m}:{s:02d}"

    # Context processor — inject app-wide template vars
    @app.context_processor
    def inject_globals():
        info = get_token_expiry_info()
        connected = token_exists() and info.get("valid", False)
        return {
            "version": __version__,
            "build_time": _BUILD_TIME,
            "connected": connected,
            "proxy_labels": PROXY_LABELS,
            "proxy_count": len(PROXY_LIST),
        }

    # Health check endpoint
    @app.route("/health")
    def health():
        return jsonify({"status": "ok"}), 200

    # Error handlers
    @app.errorhandler(404)
    def not_found(err):
        return render_template("error.html", error_code="404",
                              error_message="The page you're looking for doesn't exist."), 404

    @app.errorhandler(500)
    def server_error(err):
        _log.error("Internal server error: %s", err, exc_info=True)
        return render_template("error.html", error_code="500",
                              error_message="Something went wrong."), 500

    # ── Background workers (startup) ────────────────────────────
    from app.download.controller import start_worker_pool, recover_interrupted_jobs
    start_worker_pool()
    recover_interrupted_jobs()

    from app.import_upload.controller import start_worker as start_import_worker
    start_import_worker()

    # ── Import upload blueprint ─────────────────────────────────

    _log.info("Qoochie v%s ready", __version__)
    return app
