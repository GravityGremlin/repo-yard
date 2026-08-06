"""Pytest fixtures for spotifryer — path isolation, app factory, DB setup."""

from __future__ import annotations

from pathlib import Path

import pytest


def _patch_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point all config-driven paths into a tmp directory."""
    dl = tmp_path / "downloads"
    lib = tmp_path / "music"
    jobs = tmp_path / "data"
    spotify_cfg = tmp_path / "spotify_config"
    import_s = tmp_path / "import_staging"
    beets = tmp_path / "beets_config"
    streamrip_cfg = tmp_path / "streamrip_config"

    for d in (dl, lib, jobs, spotify_cfg, import_s, beets, streamrip_cfg):
        d.mkdir(parents=True, exist_ok=True)

    # ── app.config ──
    monkeypatch.setattr("app.config.DOWNLOAD_DIR", dl)
    monkeypatch.setattr("app.config.LIBRARY_DIR", lib)
    monkeypatch.setattr("app.config.JOBS_DIR", jobs)
    monkeypatch.setattr("app.config.SPOTIFY_CONFIG_DIR", spotify_cfg)
    monkeypatch.setattr("app.config.IMPORT_STAGING_DIR", import_s)
    monkeypatch.setattr("app.config.BEETS_DIR", beets)
    monkeypatch.setattr("app.config.STREAMRIP_CONFIG_DIR", streamrip_cfg)
    monkeypatch.setattr("app.config._IS_CONTAINER", False)

    # ── downstream modules ──
    monkeypatch.setattr("app.download.controller.DOWNLOAD_DIR", dl)
    monkeypatch.setattr("app.download.controller.LIBRARY_DIR", lib)
    monkeypatch.setattr("app.models.JOBS_DIR", jobs)
    monkeypatch.setattr("app.models.DB_PATH", jobs / "jobs.db")
    monkeypatch.setattr("app.library.routes.LIBRARY_DIR", lib)
    monkeypatch.setattr("app.library.scan_cache.LIBRARY_DIR", lib)
    monkeypatch.setattr("app.import_upload.controller.IMPORT_STAGING_DIR", import_s)

    # ── re-init DB guard ──
    # Reset the cached module-level connection too, not just _db_initialized.
    # Otherwise every test after the first reuses the first test's open
    # connection (pointing at the first test's DB file) and jobs leak across
    # tests within a file.
    monkeypatch.setattr("app.models._db_initialized", False)
    monkeypatch.setattr("app.models._conn", None)

    # ── disable background tasks (patched at source module, not factory module) ──
    monkeypatch.setattr("app.download.controller.start_worker_pool", lambda: None)
    monkeypatch.setattr("app.download.controller.recover_interrupted_jobs", lambda: None)
    monkeypatch.setattr("app.library.scan_cache.start_scan_cache", lambda: None)
    monkeypatch.setattr("app.import_upload.controller.start_import_worker", lambda: None)
    monkeypatch.setattr("app.import_upload.controller.recover_import_jobs", lambda: None)

    # ── fake Spotify auth: all routes see "authenticated" ──
    monkeypatch.setattr("app.spotify.session.is_authenticated", lambda: True)

    # ── CSRF: SeaSurf caches _csrf_disable at init_app() time, so setting
    #    app.config["CSRF_DISABLE"] after create_app() is too late.  Disable
    #    the before_request hook directly so POST routes work in tests. ──
    monkeypatch.setattr("flask_seasurf.SeaSurf._before_request", lambda self: None)


@pytest.fixture
def app_client(tmp_path, monkeypatch):
    """Flask test client with isolated paths."""
    _patch_paths(tmp_path, monkeypatch)

    from app.factory import create_app
    app = create_app()
    app.config["TESTING"] = True
    app.config["CSRF_DISABLE"] = True
    with app.test_client() as client:
        yield client


@pytest.fixture
def jobs_db(tmp_path, monkeypatch):
    """Isolated job database for tests."""
    _patch_paths(tmp_path, monkeypatch)

    from app.models import _init_db
    _init_db()
    yield tmp_path / "data"
