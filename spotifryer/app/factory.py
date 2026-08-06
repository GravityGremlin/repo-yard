"""Application factory — Flask app creation, blueprint registration, startup."""

from __future__ import annotations

import logging
import os

from flask import Flask, render_template

from app.logging_config import setup_logging
setup_logging()

_log = logging.getLogger(__name__)

from app.config import (
    __version__,
    _BUILD_TIME,
    DOWNLOAD_DIR,
    JOBS_DIR,
    PLAYLIST_DIR,
    SPOTIFY_CONFIG_DIR,
    IMPORT_STAGING_DIR,
    FLASK_PORT,
    PROXY_LIST,
    PROXY_LABELS,
)
from app.spotify.session import is_authenticated


def create_app() -> Flask:
    """Flask application factory."""
    app = Flask(__name__)
    app.secret_key = os.environ.get("SECRET_KEY")
    if not app.secret_key:
        logging.getLogger(__name__).critical(
            "SECRET_KEY environment variable is not set! "
            "Generate one with: openssl rand -hex 32"
        )
        app.secret_key = os.urandom(32).hex()  # still works but is logged
    app.config["TEMPLATES_AUTO_RELOAD"] = True

    # CSRF protection — protects all POST/PUT/DELETE routes
    from flask_seasurf import SeaSurf
    csrf = SeaSurf(app)
    # Server-to-server endpoints called by the repo-yard aggregator (no CSRF token).
    # /playlist/resolve is read-only; /download/enqueue and /download/discography
    # are state-changing but authenticated via the session, not CSRF.
    csrf.exempt_urls((
        "/playlist/resolve",
        "/download/enqueue",
        "/download/discography",
    ))

    # Ensure directories exist
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    SPOTIFY_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    IMPORT_STAGING_DIR.mkdir(parents=True, exist_ok=True)
    PLAYLIST_DIR.mkdir(parents=True, exist_ok=True)

    # Register blueprints
    from app.spotify.routes import bp as spotify_bp
    from app.download.routes import bp as download_bp
    from app.library.routes import library_bp
    from app.search.routes import search_bp
    from app.playlist.routes import playlist_bp
    from app.stats.routes import stats_bp
    from app.audit.routes import audit_bp
    from app.collection.routes import collection_bp

    app.register_blueprint(spotify_bp)
    app.register_blueprint(download_bp)
    app.register_blueprint(library_bp)
    app.register_blueprint(search_bp)
    app.register_blueprint(playlist_bp)
    app.register_blueprint(stats_bp)
    app.register_blueprint(audit_bp)
    app.register_blueprint(collection_bp)

    from app.import_upload.routes import import_upload_bp
    app.register_blueprint(import_upload_bp)

    # Start background library scan cache
    from app.library.scan_cache import start_scan_cache
    start_scan_cache()

    # Start import worker
    from app.import_upload.controller import start_import_worker, recover_import_jobs
    recover_import_jobs()
    start_import_worker()

    # Template filters
    @app.template_filter("human_size")
    def human_size(n: int) -> str:
        for unit in ("B", "KB", "MB", "GB"):
            if n < 1024:
                return f"{n:.1f} {unit}"
            n /= 1024
        return f"{n:.1f} TB"

    @app.template_filter("format_duration")
    def format_duration(ms: int) -> str:
        if not ms:
            return "—"
        seconds = ms // 1000
        m, s = divmod(seconds, 60)
        return f"{m}:{s:02d}"

    @app.template_filter("relative_time")
    def relative_time(dt: str) -> str:
        """Format a datetime string as relative time (e.g. '5 minutes ago')."""
        from datetime import datetime, timezone
        from dateutil.parser import parse as parse_dt
        try:
            when = parse_dt(str(dt))
            now = datetime.now(timezone.utc)
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            diff = now - when
            seconds = int(diff.total_seconds())
            if seconds < 60:
                return "just now"
            minutes = seconds // 60
            if minutes < 60:
                return f"{minutes} minute{'s' if minutes != 1 else ''} ago"
            hours = minutes // 60
            if hours < 24:
                return f"{hours} hour{'s' if hours != 1 else ''} ago"
            days = hours // 24
            if days < 30:
                return f"{days} day{'s' if days != 1 else ''} ago"
            months = days // 30
            return f"{months} month{'s' if months != 1 else ''} ago"
        except Exception:
            _log.debug("relative_time filter failed for input %r", dt)
            return str(dt)

    # Context processor — inject app-wide template vars
    @app.context_processor
    def inject_globals():
        return {
            "version": __version__,
            "build_time": _BUILD_TIME,
            "connected": is_authenticated(),
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

    # Health check endpoint
    @app.route("/health")
    def health_check():
        from flask import jsonify
        return jsonify({"status": "ok"}), 200

    _log.info("spotifryer v%s ready on port %s", __version__, FLASK_PORT)

    # Start download worker pool and recover interrupted jobs
    from app.download.controller import start_worker_pool, recover_interrupted_jobs
    recover_interrupted_jobs()
    start_worker_pool()

    return app
