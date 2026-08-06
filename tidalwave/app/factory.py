"""Application factory — Flask app creation, blueprint registration, startup."""

from __future__ import annotations

import logging
import os
from urllib.parse import urlparse

from flask import Flask, render_template, request

from app.logging_config import setup_logging
setup_logging()

_log = logging.getLogger(__name__)

from app.config import (
    __version__,
    _BUILD_TIME,
    DOWNLOAD_DIR,
    IMPORT_STAGING_DIR,
    JOBS_DIR,
    TIDAL_CONFIG_DIR,
    FLASK_PORT,
    PROXY_LIST,
    PROXY_LABELS,
)
from app.tidal.session import token_exists, get_token_expiry_info

# Hostnames allowed as Origin/Referer for CSRF-exempt server-to-server
# endpoints.  Covers repo-yard aggregator (10.8.0.10:19297 / ry.n0g.xyz),
# same-host tool access (10.8.0.10), and localhost dev.
_CSRF_EXEMPT_ALLOWED_HOSTNAMES = frozenset({
    "10.8.0.10",
    "localhost",
    "127.0.0.1",
    "ry.n0g.xyz",
})

# URLs exempt from SeaSurf CSRF (server-to-server from repo-yard aggregator).
_CRF_EXEMPT_PATHS = ("/playlist/resolve", "/download/enqueue", "/download/discography")


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
    csrf.exempt_urls(_CRF_EXEMPT_PATHS)

    # ── Origin/Referer guard for CSRF-exempt endpoints ────────────
    # These endpoints skip SeaSurf for server-to-server use.  To prevent
    # browser-originated CSRF from third-party sites, reject requests
    # whose Origin or Referer netloc is not a trusted host.  Requests
    # with *no* Origin/Referer (true server-to-server) are allowed.
    @app.before_request
    def _csrf_exempt_origin_guard():
        if request.path not in _CRF_EXEMPT_PATHS:
            return None  # not an exempt endpoint — SeaSurf handles it

        origin = request.headers.get("Origin")
        referer = request.headers.get("Referer")

        # No Origin and no Referer: server-to-server — allow.
        if not origin and not referer:
            return None

        # Parse netloc from whichever header is present.
        ref = origin or referer
        try:
            netloc = urlparse(ref).netloc
        except Exception:
            return None  # malformed — let SeaSurf / normal flow decide

        # Strip port for hostname comparison.
        hostname = netloc.rsplit(":", 1)[0] if "]" not in netloc and ":" in netloc else netloc
        # Handle IPv6 brackets: [::1]:port → ::1
        if hostname.startswith("[") and hostname.endswith("]"):
            hostname = hostname[1:-1]

        # Allow if hostname matches the tool's own host or the allowlist.
        own_host = request.host.rsplit(":", 1)[0] if ":" in request.host else request.host
        if hostname == own_host:
            return None
        if hostname in _CSRF_EXEMPT_ALLOWED_HOSTNAMES:
            return None

        _log.warning(
            "CSRF-exempt endpoint %s rejected untrusted Origin/Referer: %s (netloc=%s)",
            request.path, ref, netloc,
        )
        return ("Forbidden", 403)

    # Ensure directories exist
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    TIDAL_CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    # Register blueprints
    from app.search.routes import bp as search_bp
    from app.download.routes import bp as download_bp
    from app.tidal.routes import bp as tidal_bp
    from app.playlist.routes import bp as playlist_bp
    from app.library.routes import bp as library_bp
    from app.collection.routes import bp as collection_bp
    from app.audit.routes import bp as audit_bp
    from app.stats.routes import bp as stats_bp

    app.register_blueprint(search_bp)
    app.register_blueprint(download_bp)
    app.register_blueprint(tidal_bp)
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

    # Health check
    from flask import jsonify

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
                               error_message="Something went wrong on our end."), 500

    # Cache static assets (CSS/JS/PWA) at the CDN / browser edge.
    @app.after_request
    def _set_static_cache(response):
        from flask import request
        if request.path.startswith("/static/"):
            response.headers["Cache-Control"] = "public, max-age=86400"
        return response

    # Start download worker pool and recover any interrupted jobs.
    # recover_interrupted_jobs() is inlined below (without its internal
    # _restore_queue_order call) because start_worker_pool() already
    # restored the persisted queue order — calling it twice was redundant.
    from app.download.controller import start_worker_pool, _queued_order as _dl_q, _save_queue_order as _dl_save
    from app.models import JobStatus, save_job, list_active_jobs
    from app.import_upload.controller import start_worker as start_import_worker, recover_interrupted_jobs as recover_import_jobs
    start_worker_pool()

    # ── recover interrupted downloads (mirrors recover_interrupted_jobs,
    #    minus the redundant _restore_queue_order) ────────────────────
    requeued = 0
    for job in list_active_jobs():
        if job.status == JobStatus.RUNNING:
            job.status = JobStatus.QUEUED
            job.progress = 0.0
            save_job(job)
            _log.info("Recovering interrupted job %s", job.id)
        if job.id not in _dl_q:
            _dl_q.insert(0, job.id)
            requeued += 1
    if requeued:
        _log.info("Recovered %d active jobs into the queue", requeued)
    _dl_save()

    start_import_worker()
    recover_import_jobs()
    IMPORT_STAGING_DIR.mkdir(parents=True, exist_ok=True)

    # Warm up scan caches in background threads
    from app.library.scan_cache import refresh_now
    import threading as _threading
    for _kind in ("recent", "collection"):
        _threading.Thread(target=refresh_now, args=(_kind,), daemon=True).start()

    _log.info("tidalwave v%s ready on port %s", __version__, FLASK_PORT)
    return app
