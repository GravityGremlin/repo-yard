"""Database models — SQLite-backed job store using stdlib sqlite3."""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone

from app.config import JOBS_DIR

_log = logging.getLogger(__name__)

# ── SQLite ──────────────────────────────────────────────────────
DB_PATH = JOBS_DIR / "jobs.db"
_lock = threading.RLock()
_db_initialized = False
_conn: sqlite3.Connection | None = None


def _ts() -> str:
    return datetime.now(timezone.utc).isoformat()


def _migrate_add_column(db: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    """Idempotent: add a column only if it does not already exist."""
    if not re.match(r"^[a-zA-Z_][a-zA-Z0-9_]*$", table):
        raise ValueError(f"Invalid table name: {table!r}")
    existing = db.execute(f"PRAGMA table_info({table})").fetchall()
    cols = {row[1] for row in existing}
    if column not in cols:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _init_db() -> None:
    global _db_initialized
    if _db_initialized:
        return
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(DB_PATH))
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("""
        CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY,
            data TEXT NOT NULL,
            created_at TEXT,
            updated_at TEXT
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event TEXT NOT NULL,
            job_id TEXT,
            data TEXT,
            created_at TEXT
        )
    """)
    # ── Migrations (idempotent via PRAGMA table_info guard) ──────
    _migrate_add_column(db, "jobs", "override_existing", "INTEGER DEFAULT 0")
    db.commit()
    db.close()
    _db_initialized = True


def _connect() -> sqlite3.Connection:
    global _conn
    _init_db()
    if _conn is None:
        with _lock:
            if _conn is None:
                _conn = sqlite3.connect(str(DB_PATH), timeout=5, check_same_thread=False)
                _conn.row_factory = sqlite3.Row
    return _conn


# ── Job status ──────────────────────────────────────────────────
class JobStatus:
    QUEUED = "queued"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    ERROR = "error"
    CANCELLED = "cancelled"

    ACTIVE = {QUEUED, RUNNING, PAUSED}
    TERMINAL = {COMPLETED, ERROR, CANCELLED}


@dataclass
class Job:
    """A download job."""
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    url: str = ""
    title: str = ""
    artist: str = ""
    kind: str = "track"  # track | album | playlist
    group_id: str = ""  # batches a group of related jobs (e.g. one playlist)
    status: str = JobStatus.QUEUED
    progress: float = 0.0  # 0.0 → 1.0
    bytes_downloaded: int = 0
    total_bytes: int = 0
    files: list[str] = field(default_factory=list)
    error: str = ""
    proxy_index: int = 0
    override_existing: bool = False
    created_at: str = field(default_factory=_ts)
    updated_at: str = field(default_factory=_ts)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> Job:
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


# ── CRUD ─────────────────────────────────────────────────────────
def save_job(job: Job) -> None:
    """Insert or update a job in SQLite."""
    job.updated_at = _ts()
    data = json.dumps(job.to_dict())
    with _lock:
        db = _connect()
        db.execute(
            "INSERT OR REPLACE INTO jobs (id, data, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (job.id, data, job.created_at, job.updated_at),
        )
        db.commit()


def get_job(job_id: str) -> Job | None:
    with _lock:
        db = _connect()
        row = db.execute("SELECT data FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row:
            return Job.from_dict(json.loads(row["data"]))
        return None


def list_jobs(limit: int = 100) -> list[Job]:
    """Return recent jobs, newest first."""
    with _lock:
        db = _connect()
        rows = db.execute(
            "SELECT data FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [Job.from_dict(json.loads(r["data"])) for r in rows]


def list_active_jobs() -> list[Job]:
    """Return only queued or running jobs.

    Uses SQLite's json_extract to filter at the SQL level, then does a
    second pass in Python for safety in case of legacy data.
    """
    with _lock:
        db = _connect()
        rows = db.execute(
            "SELECT data FROM jobs WHERE "
            "json_extract(data, '$.status') IN ('queued', 'running') "
            "ORDER BY updated_at DESC"
        ).fetchall()
        jobs = [Job.from_dict(json.loads(r["data"])) for r in rows]
        return [j for j in jobs if j.status in JobStatus.ACTIVE]


def delete_job(job_id: str) -> None:
    with _lock:
        db = _connect()
        db.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
        db.commit()


def append_audit(event: str, job_id: str = "", data: dict | None = None) -> None:
    """Append to audit log."""
    payload = json.dumps(data or {})
    with _lock:
        db = _connect()
        db.execute(
            "INSERT INTO audit_log (event, job_id, data, created_at) VALUES (?, ?, ?, ?)",
            (event, job_id, payload, _ts()),
        )
        db.commit()


def list_audit(limit: int = 100) -> list[dict]:
    """Return recent audit log entries, newest first."""
    with _lock:
        db = _connect()
        rows = db.execute(
            "SELECT id, event, job_id, data, created_at FROM audit_log ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
