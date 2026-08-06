"""Pytest fixtures for qoochie — path isolation, app factory, DB setup."""

from __future__ import annotations

from pathlib import Path

import pytest


def _patch_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point all config-driven paths into a tmp directory."""
    dl = tmp_path / "downloads"
    lib = tmp_path / "music"
    jobs = tmp_path / "data"
    qobuz_cfg = tmp_path / "qobuz_config"
    import_s = tmp_path / "import_staging"

    for d in (dl, lib, jobs, qobuz_cfg, import_s):
        d.mkdir(parents=True, exist_ok=True)

    # ── app.config ──
    monkeypatch.setattr("app.config.DOWNLOAD_DIR", dl)
    monkeypatch.setattr("app.config.LIBRARY_DIR", lib)
    monkeypatch.setattr("app.config.JOBS_DIR", jobs)
    monkeypatch.setattr("app.config.QOBUZ_CONFIG_DIR", qobuz_cfg)
    monkeypatch.setattr("app.config.IMPORT_STAGING_DIR", import_s)
    monkeypatch.setattr("app.config._IS_CONTAINER", False)

    # ── downstream modules ──
    monkeypatch.setattr("app.download.controller.DOWNLOAD_DIR", dl)
    monkeypatch.setattr("app.download.controller.LIBRARY_DIR", lib)
    monkeypatch.setattr("app.qobuz.downloader.DOWNLOAD_DIR", dl)
    monkeypatch.setattr("app.library.routes.LIBRARY_DIR", lib)
    monkeypatch.setattr("app.library.scan_cache.LIBRARY_DIR", lib)
    monkeypatch.setattr("app.search.routes.LIBRARY_DIR", lib)
    monkeypatch.setattr("app.collection.routes.LIBRARY_DIR", lib)
    monkeypatch.setattr("app.models.JOBS_DIR", jobs)
    monkeypatch.setattr("app.models.DB_PATH", jobs / "jobs.db")
    monkeypatch.setattr("app.qobuz.session.TOKEN_FILE", qobuz_cfg / "qobuz_token.json")
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
    monkeypatch.setattr("app.import_upload.controller.start_worker", lambda: None)
    monkeypatch.setattr("app.qobuz.session.bootstrap_env_token", lambda: {"status": "skipped", "message": "test"})

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
    with app.test_client() as client:
        yield client


@pytest.fixture
def jobs_db(tmp_path, monkeypatch):
    """Isolated job database for tests."""
    _patch_paths(tmp_path, monkeypatch)

    from app.models import Job, JobStatus, save_job, list_jobs, delete_job, get_job
    return {"Job": Job, "JobStatus": JobStatus, "save": save_job,
            "list_all": list_jobs, "delete": delete_job, "get": get_job}
