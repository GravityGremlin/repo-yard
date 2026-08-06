"""Regression tests for concurrency fixes (ARL token race, queue-order race, SQLite)."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# (a) ARL token file race — concurrent read/write always yields valid JSON
# ---------------------------------------------------------------------------

def test_token_concurrent_read_write(tmp_path, monkeypatch):
    """Many threads writing + reading the token file — file is always valid JSON."""
    token_file = tmp_path / "deezer_token.json"
    monkeypatch.setattr("app.deezer.session.TOKEN_FILE", token_file)

    from app.deezer.session import _write_token, _read_token, _token_lock

    errors: list[str] = []
    stop = threading.Event()

    def writer(n: int):
        try:
            while not stop.is_set():
                with _token_lock:
                    _write_token({"iteration": n, "arl": "x" * 30, "user_id": n})
        except Exception as exc:
            errors.append(f"writer-{n}: {exc}")

    def reader():
        try:
            while not stop.is_set():
                with _token_lock:
                    raw = _read_token()
                # After acquiring the lock, the file must be valid JSON
                if raw and "arl" not in raw:
                    errors.append(f"reader: got unexpected keys {list(raw.keys())}")
        except (json.JSONDecodeError, OSError) as exc:
            errors.append(f"reader: {exc}")
        except Exception as exc:
            errors.append(f"reader: {exc}")

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(4)]
    threads += [threading.Thread(target=reader) for _ in range(4)]

    for t in threads:
        t.start()
    time.sleep(0.3)
    stop.set()
    for t in threads:
        t.join(timeout=5)

    assert errors == [], f"Concurrency errors: {errors}"

    # Final read outside lock confirms file is well-formed JSON
    final = json.loads(token_file.read_text())
    assert "arl" in final


# ---------------------------------------------------------------------------
# (b) Queue-order concurrency — enqueue/reorder with many threads
# ---------------------------------------------------------------------------

def test_queue_order_concurrency(tmp_path, monkeypatch):
    """Many threads enqueueing + reordering → no duplicates, no losses."""
    _patch_minimal(tmp_path, monkeypatch)

    from app.download.controller import _queued_order, _priority_lock, enqueue, reorder_queue

    # Reset queue state
    with _priority_lock:
        _queued_order.clear()

    N = 30
    ids = [f"job-{i:03d}" for i in range(N)]
    errors: list[str] = []

    def do_enqueue(job_id: str):
        try:
            enqueue(job_id)
        except Exception as exc:
            errors.append(f"enqueue {job_id}: {exc}")

    def do_reorder(job_id: str):
        try:
            reorder_queue(job_id, "up")
        except Exception as exc:
            errors.append(f"reorder {job_id}: {exc}")

    threads = []
    for jid in ids:
        threads.append(threading.Thread(target=do_enqueue, args=(jid,)))
    # Some threads reorder while enqueuing
    for jid in ids[:15]:
        threads.append(threading.Thread(target=do_reorder, args=(jid,)))

    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert errors == [], f"Concurrency errors: {errors}"

    with _priority_lock:
        queue_snapshot = list(_queued_order)

    # No duplicates
    assert len(queue_snapshot) == len(set(queue_snapshot)), "Duplicate IDs in queue"
    # All enqueued IDs present
    assert set(ids).issubset(set(queue_snapshot)), "Lost job IDs"


# ---------------------------------------------------------------------------
# (d) SQLite concurrent writes — no OperationalError
# ---------------------------------------------------------------------------

def test_sqlite_concurrent_writes(tmp_path, monkeypatch):
    """Multiple threads writing to SQLite simultaneously — no OperationalError."""
    _patch_minimal(tmp_path, monkeypatch)

    from app.models import Job, save_job, get_job

    errors: list[str] = []
    N = 30

    def write_job(idx: int):
        try:
            job = Job(id=f"conc-{idx:03d}", title=f"Job {idx}", status="queued")
            save_job(job)
        except Exception as exc:
            errors.append(f"write {idx}: {exc}")

    def read_job(idx: int):
        try:
            j = get_job(f"conc-{idx:03d}")
            # May be None if not yet written, that's fine
        except Exception as exc:
            errors.append(f"read {idx}: {exc}")

    threads = []
    for i in range(N):
        threads.append(threading.Thread(target=write_job, args=(i,)))
        threads.append(threading.Thread(target=read_job, args=(i,)))

    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert errors == [], f"SQLite concurrency errors: {errors}"

    # Verify all jobs persisted
    for i in range(N):
        j = get_job(f"conc-{i:03d}")
        assert j is not None, f"Job conc-{i:03d} not found after concurrent write"
        assert j.title == f"Job {i}"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _patch_minimal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Minimal path patching for controller / model tests."""
    dl = tmp_path / "downloads"
    lib = tmp_path / "music"
    jobs = tmp_path / "data"
    deezer_cfg = tmp_path / "deezer_config"
    import_s = tmp_path / "import_staging"

    for d in (dl, lib, jobs, deezer_cfg, import_s):
        d.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr("app.config.DOWNLOAD_DIR", dl)
    monkeypatch.setattr("app.config.LIBRARY_DIR", lib)
    monkeypatch.setattr("app.config.JOBS_DIR", jobs)
    monkeypatch.setattr("app.config.DEEZER_CONFIG_DIR", deezer_cfg)
    monkeypatch.setattr("app.config.IMPORT_STAGING_DIR", import_s)
    monkeypatch.setattr("app.config._IS_CONTAINER", False)

    monkeypatch.setattr("app.download.controller.DOWNLOAD_DIR", dl)
    monkeypatch.setattr("app.download.controller.LIBRARY_DIR", lib)
    monkeypatch.setattr("app.download.controller._QUEUE_ORDER_FILE", jobs / "queue_order.json")
    monkeypatch.setattr("app.deezer.downloader.DOWNLOAD_DIR", dl)
    monkeypatch.setattr("app.models.JOBS_DIR", jobs)
    monkeypatch.setattr("app.models.DB_PATH", jobs / "jobs.db")
    monkeypatch.setattr("app.deezer.session.TOKEN_FILE", deezer_cfg / "deezer_token.json")

    monkeypatch.setattr("app.models._db_initialized", False)
    monkeypatch.setattr("app.models._conn", None)

    # Disable background tasks
    monkeypatch.setattr("app.download.controller.start_worker_pool", lambda: None)
    monkeypatch.setattr("app.download.controller.recover_interrupted_jobs", lambda: None)
