"""Tests for job management routes — enqueue/cancel/retry + pause/resume/priority."""

from __future__ import annotations

from unittest.mock import patch

from app.models import Job, JobStatus, save_job, get_job


def _make_job(job_id: str, status: str = JobStatus.QUEUED) -> Job:
    """Create and persist a minimal job for testing."""
    job = Job(
        id=job_id,
        url=f"https://open.spotify.com/track/{job_id}",
        title=f"Track {job_id}",
        artist="Test Artist",
        kind="track",
    )
    job.status = status
    save_job(job)
    return job


class TestCancel:
    """POST /download/<id>/cancel"""

    def test_cancel_queued_job(self, app_client, jobs_db):
        """Cancelling a queued job succeeds."""
        _make_job("c-1", JobStatus.QUEUED)
        resp = app_client.post("/download/c-1/cancel")
        assert resp.status_code == 200

    def test_cancel_nonexistent(self, app_client, jobs_db):
        """Cancelling a nonexistent job returns 200 with ok=false."""
        resp = app_client.post("/download/noexist/cancel")
        assert resp.status_code == 200
        assert resp.get_json()["ok"] is False


class TestRetry:
    """POST /download/<id>/retry"""

    def test_retry_errored_job(self, app_client, jobs_db):
        """Retrying an ERROR job sets it to QUEUED."""
        _make_job("r-err", JobStatus.ERROR)
        resp = app_client.post("/download/r-err/retry")
        assert resp.status_code == 200
        job = get_job("r-err")
        assert job.status == JobStatus.QUEUED

    def test_retry_cancelled_job(self, app_client, jobs_db):
        """Retrying a CANCELLED job sets it to QUEUED."""
        _make_job("r-canc", JobStatus.CANCELLED)
        resp = app_client.post("/download/r-canc/retry")
        assert resp.status_code == 200
        assert get_job("r-canc").status == JobStatus.QUEUED

    def test_retry_running_job_fails(self, app_client, jobs_db):
        """Retrying a RUNNING job returns 400."""
        _make_job("r-run", JobStatus.RUNNING)
        resp = app_client.post("/download/r-run/retry")
        assert resp.status_code == 400


class TestPause:
    """POST /download/<id>/pause"""

    def test_pause_queued_job(self, app_client, jobs_db):
        """Pausing a queued job sets it to PAUSED."""
        _make_job("p-1", JobStatus.QUEUED)
        resp = app_client.post("/download/p-1/pause")
        assert resp.status_code == 200
        assert get_job("p-1").status == JobStatus.PAUSED

    def test_pause_running_job(self, app_client, jobs_db):
        """Pausing a running job sets it to PAUSED."""
        _make_job("p-2", JobStatus.RUNNING)
        resp = app_client.post("/download/p-2/pause")
        assert resp.status_code == 200
        assert get_job("p-2").status == JobStatus.PAUSED

    def test_pause_completed_job_fails(self, app_client, jobs_db):
        """Pausing a completed job returns 400."""
        _make_job("p-3", JobStatus.COMPLETED)
        resp = app_client.post("/download/p-3/pause")
        assert resp.status_code == 400


class TestResume:
    """POST /download/<id>/resume"""

    def test_resume_paused_job(self, app_client, jobs_db):
        """Resuming a paused job sets it to QUEUED."""
        _make_job("res-1", JobStatus.PAUSED)
        resp = app_client.post("/download/res-1/resume")
        assert resp.status_code == 200
        assert get_job("res-1").status == JobStatus.QUEUED

    def test_resume_queued_job_fails(self, app_client, jobs_db):
        """Resuming a queued job returns 400."""
        _make_job("res-2", JobStatus.QUEUED)
        resp = app_client.post("/download/res-2/resume")
        assert resp.status_code == 400


class TestPriority:
    """POST /download/<id>/priority/up and /priority/down"""

    def test_priority_up(self, app_client, jobs_db):
        """Moving up shifts job forward in queue."""
        _make_job("pri-1", JobStatus.QUEUED)
        # Enqueue into the controller's in-memory queue so it's in _queued_order
        from app.download import controller
        with controller._priority_lock:
            controller._queued_order.append("pri-1")
        resp = app_client.post("/download/pri-1/priority/up")
        assert resp.status_code == 200

    def test_priority_down(self, app_client, jobs_db):
        """Moving down shifts job backward in queue."""
        _make_job("pri-2", JobStatus.QUEUED)
        from app.download import controller
        with controller._priority_lock:
            controller._queued_order.append("pri-2")
        resp = app_client.post("/download/pri-2/priority/down")
        assert resp.status_code == 200

    def test_priority_nonqueued_fails(self, app_client, jobs_db):
        """Priority on a non-queued job returns 400."""
        _make_job("pri-3", JobStatus.COMPLETED)
        resp = app_client.post("/download/pri-3/priority/up")
        assert resp.status_code == 400


class TestRetryAllErrored:
    """POST /download/jobs/retry-all-errored"""

    def test_retry_all(self, app_client, jobs_db):
        """Batch retry resets errored jobs."""
        _make_job("ra-1", JobStatus.ERROR)
        _make_job("ra-2", JobStatus.CANCELLED)
        resp = app_client.post("/download/jobs/retry-all-errored")
        assert resp.status_code == 200


class TestPurge:
    """POST /download/jobs/purge"""

    def test_purge_completed(self, app_client, jobs_db):
        """Purge deletes completed jobs."""
        _make_job("pg-1", JobStatus.COMPLETED)
        resp = app_client.post("/download/jobs/purge")
        assert resp.status_code == 200


class TestDeleteSelected:
    """POST /download/jobs/delete-selected"""

    def test_delete_selected(self, app_client, jobs_db):
        """Delete selected jobs by ID."""
        _make_job("ds-1", JobStatus.COMPLETED)
        resp = app_client.post(
            "/download/jobs/delete-selected",
            json={"ids": ["ds-1"]},
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 200
        assert get_job("ds-1") is None
