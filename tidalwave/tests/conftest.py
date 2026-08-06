"""Pytest fixtures for tidalwave — path isolation, fake Tidal session, DB setup."""

from __future__ import annotations

from pathlib import Path

import pytest


def _patch_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point all config-driven paths into a tmp directory.

    Patches ``app.config`` attrs *and* the cached copies in downstream modules
    that imported them at module level, so ``create_app()`` sees the isolated
    directories.  Also disables the download worker pool since tests should not
    run background threads.
    """
    dl = tmp_path / "downloads"
    lib = tmp_path / "music"
    jobs = tmp_path / "data"
    tidal_cfg = tmp_path / "tidal_config"
    import_s = tmp_path / "import_staging"

    for d in (dl, lib, jobs, tidal_cfg, import_s):
        d.mkdir(parents=True, exist_ok=True)

    # ── app.config ──────────────────────────────────────────────
    monkeypatch.setattr("app.config.DOWNLOAD_DIR", dl)
    monkeypatch.setattr("app.config.LIBRARY_DIR", lib)
    monkeypatch.setattr("app.config.JOBS_DIR", jobs)
    monkeypatch.setattr("app.config.TIDAL_CONFIG_DIR", tidal_cfg)
    monkeypatch.setattr("app.config.IMPORT_STAGING_DIR", import_s)
    monkeypatch.setattr("app.config._IS_CONTAINER", False)

    # ── downstream modules that captured the old value ──────────
    monkeypatch.setattr("app.download.controller.DOWNLOAD_DIR", dl)
    monkeypatch.setattr("app.download.controller.LIBRARY_DIR", lib)
    monkeypatch.setattr("app.tidal.downloader.DOWNLOAD_DIR", dl)
    monkeypatch.setattr("app.library.routes.LIBRARY_DIR", lib)
    monkeypatch.setattr("app.library.scan_cache.LIBRARY_DIR", lib)
    monkeypatch.setattr("app.search.routes.LIBRARY_DIR", lib)
    monkeypatch.setattr("app.collection.routes.LIBRARY_DIR", lib)
    monkeypatch.setattr("app.models.JOBS_DIR", jobs)
    monkeypatch.setattr("app.models.DB_PATH", jobs / "jobs.db")
    monkeypatch.setattr("app.tidal.session._config_dir", tidal_cfg)
    monkeypatch.setattr("app.tidal.session._token_file", tidal_cfg / "token.json")
    monkeypatch.setattr("app.import_upload.controller.IMPORT_STAGING_DIR", import_s)

    # ── re-init DB guard ────────────────────────────────────────
    # Reset the cached module-level connection too, not just _db_initialized.
    # Otherwise every test after the first reuses the first test's open
    # connection (pointing at the first test's DB file) and jobs leak across
    # tests within a file.
    monkeypatch.setattr("app.models._db_initialized", False)
    monkeypatch.setattr("app.models._conn", None)

    # ── disable background tasks (patched at source module) ─────
    monkeypatch.setattr("app.download.controller.start_worker_pool", lambda: None)
    monkeypatch.setattr("app.download.controller.recover_interrupted_jobs", lambda: None)
    monkeypatch.setattr("app.import_upload.controller.start_worker", lambda: None)
    monkeypatch.setattr("app.import_upload.controller.recover_interrupted_jobs", lambda: None)

    # ── CSRF: SeaSurf caches _csrf_disable at init_app() time, so setting
    #    app.config["CSRF_DISABLE"] after create_app() is too late.  Disable
    #    the before_request hook directly so POST routes work in tests. ──
    import flask_seasurf
    monkeypatch.setattr(flask_seasurf.SeaSurf, "_before_request", lambda self: None)



@pytest.fixture
def app_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Flask test client with isolated directories and no worker pool.

    Every test gets a fresh app and temp directories — no state leaks between
    tests.
    """
    _patch_paths(tmp_path, monkeypatch)

    from app.factory import create_app
    app = create_app()
    app.config["TESTING"] = True
    app.config["CSRF_DISABLE"] = True

    with app.test_client() as client:
        yield client


@pytest.fixture
def jobs_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Isolated job database for tests — re-patches paths and re-inits DB."""
    _patch_paths(tmp_path, monkeypatch)

    from app.models import _init_db
    _init_db()
