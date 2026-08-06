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
    IMPORT_STAGING_DIR,
    JOBS_DIR,
    DEEZER_CONFIG_DIR,
    FLASK_PORT,
    PROXY_LIST,
    PROXY_LABELS,
)
from app.deezer.session import token_exists, get_token_expiry_info

# Hostnames / netlocs that are trusted for CSRF-exempt POST endpoints.
# Server-to-server calls (no Origin/Referer header) always pass.
_CSRF_ALLOWED_NETLOCS: set[str] = {
    "10.8.0.10",
    "localhost",
    "127.0.0.1",
    "ry.n0g.xyz",
    "dz.n0g.xyz",
}


def create_app() -> Flask:
    """Flask application factory."""
    app = Flask(__name__)
    app.secret_key = os.environ.get("SECRET_KEY") or os.urandom(32).hex()
    app.config["TEMPLATES_AUTO_RELOAD"] = True

    # CSRF protection — protects all POST/PUT/DELETE routes
    from flask_seasurf import SeaSurf
    csrf = SeaSurf(app)
    # The playlist resolve endpoint is read-only (no state change); the
    # repo-yard aggregator calls it server-to-server without a CSRF token.
    # Download endpoints are session-authenticated state changes, called
    # server-to-server from the aggregator on the same host network.
    csrf.exempt_urls(("/playlist/resolve", "/download/enqueue", "/download/discography"))

    # ── CSRF-origin guard for exempt endpoints ──────────────────────────
    # Harden (not remove) the exemptions: reject cross-origin POSTs from
    # untrusted browser origins while still allowing:
    #   • Server-to-server calls (no Origin / Referer header)
    #   • Same-origin requests (Origin netloc == request Host)
    #   • Known internal origins (repo-yard aggregator, local dev, LAN)
    _csrf_exempt_paths = frozenset({"/playlist/resolve", "/download/enqueue", "/download/discography"})

    @app.before_request
    def _csrf_origin_guard():
        if request.method not in ("POST", "PUT", "DELETE", "PATCH"):
            return None
        if request.path not in _csrf_exempt_paths:
            return None  # SeaSurf handles non-exempt routes normally
        origin = request.headers.get("Origin") or request.headers.get("Referer")
        if not origin:
            return None  # No Origin/Referer → server-to-server, allow
        # Relative Referer (e.g. "/download/enqueue") is same-origin
        if origin.startswith("/") and not origin.startswith("//"):
            return None
        parsed = urlparse(origin)
        netloc = parsed.netloc.split(":")[0]  # strip port for hostname match
        # Same-origin: Origin netloc matches the request's own Host header
        req_host = request.host.split(":")[0]
        if netloc == req_host:
            return None
        if netloc in _CSRF_ALLOWED_NETLOCS:
            return None
        _log.warning("CSRF origin guard blocked %s on %s", origin, request.path)
        return jsonify({"error": "forbidden", "message": "CSRF origin check failed"}), 403

    # Ensure directories exist
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    DEEZER_CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    # Bootstrap a session from DEEZER_ARL env var if provided (container deployments)
    from app.deezer.session import bootstrap_env_arl
    bootstrap_env_arl()

    # Register blueprints
    from app.search.routes import bp as search_bp
    from app.download.routes import bp as download_bp
    from app.deezer.routes import bp as deezer_bp
    from app.playlist.routes import bp as playlist_bp
    from app.library.routes import bp as library_bp
    from app.collection.routes import bp as collection_bp
    from app.audit.routes import bp as audit_bp
    from app.stats.routes import bp as stats_bp

    app.register_blueprint(search_bp)
    app.register_blueprint(download_bp)
    app.register_blueprint(deezer_bp)
    app.register_blueprint(playlist_bp)
    app.register_blueprint(library_bp)
    app.register_blueprint(collection_bp)
    app.register_blueprint(audit_bp)
    app.register_blueprint(stats_bp)

    from app.import_upload.routes import bp as import_bp
    app.register_blueprint(import_bp)

    # ── Health check ─────────────────────────────────────────────────────
    @app.route("/health")
    def health():
        return jsonify({"status": "ok"})

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

    # Error handlers
    @app.errorhandler(404)
    def not_found(err):
        return render_template("error.html", error_code="404",
                              error_message="The page you're looking for doesn't exist."), 404

    @app.errorhandler(500)
    def server_error(err):
        _log.error("Internal server error: %s", err, exc_info=True)
        return render_template("error.html", error_code="500",
                               error_message="Something went wrong on our end."), 500

    # Start download worker pool and recover any interrupted jobs
    from app.download.controller import start_worker_pool, recover_interrupted_jobs
    from app.import_upload.controller import start_worker as start_import_worker, recover_interrupted_jobs as recover_import_jobs
    start_worker_pool()
    recover_interrupted_jobs()
    start_import_worker()
    recover_import_jobs()
    IMPORT_STAGING_DIR.mkdir(parents=True, exist_ok=True)

    # Warm up scan caches in background threads
    from app.library.scan_cache import refresh_now
    import threading as _threading
    for _kind in ("recent", "collection"):
        _threading.Thread(target=refresh_now, args=(_kind,), daemon=True).start()

    _log.info("deeznutz v%s ready on port %s", __version__, FLASK_PORT)
    return app
