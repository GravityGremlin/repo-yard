"""Tests for job management routes — retry, pause, resume, purge, batch retry.

Covers:
* ``POST /download/jobs/<id>/retry``     — 200 on ERROR/PAUSED, 400 on RUNNING
* ``POST /download/jobs/<id>/pause``      — 200 on QUEUED/RUNNING, 400 on COMPLETED
* ``POST /download/jobs/<id>/resume``     — 200 on PAUSED, 400 on QUEUED
* ``POST /download/jobs/purge``          — deletes terminal jobs, keeps active
* ``POST /download/jobs/retry-all-errored`` — batch retry all ERROR/CANCELLED
"""

from __future__ import annotations

from app.models import Job, JobStatus, save_job, get_job


def _make_job(job_id: str, status: str = JobStatus.QUEUED) -> Job:
    """Create and persist a minimal job for testing."""
    job = Job(
        id=job_id,
        url=f"https://deezer.com/album/{job_id}",
        title=f"Album {job_id}",
        artist="Test Artist",
        kind="album",
    )
    job.status = status
    save_job(job)
    return job


class TestRetry:
    """POST /download/jobs/<id>/retry"""

    def test_retry_errored_job(self, app_client, jobs_db):
        """Retrying an ERROR job sets it to QUEUED and returns 200."""
        _make_job("r-err", JobStatus.ERROR)
        resp = app_client.post("/download/jobs/r-err/retry")
        assert resp.status_code == 200
        assert get_job("r-err").status == JobStatus.QUEUED

    def test_retry_cancelled_job(self, app_client, jobs_db):
        """Retrying a CANCELLED job sets it to QUEUED."""
        _make_job("r-canc", JobStatus.CANCELLED)
        resp = app_client.post("/download/jobs/r-canc/retry")
        assert resp.status_code == 200
        assert get_job("r-canc").status == JobStatus.QUEUED

    def test_retry_paused_job(self, app_client, jobs_db):
        """Retrying a PAUSED job sets it to QUEUED."""
        _make_job("r-pau", JobStatus.PAUSED)
        resp = app_client.post("/download/jobs/r-pau/retry")
        assert resp.status_code == 200
        assert get_job("r-pau").status == JobStatus.QUEUED

    def test_retry_running_job_returns_400(self, app_client, jobs_db):
        """Retrying a RUNNING job returns 400."""
        _make_job("r-run", JobStatus.RUNNING)
        resp = app_client.post("/download/jobs/r-run/retry")
        assert resp.status_code == 400
        assert get_job("r-run").status == JobStatus.RUNNING

    def test_retry_completed_job_returns_400(self, app_client, jobs_db):
        """Retrying a COMPLETED job returns 400."""
        _make_job("r-done", JobStatus.COMPLETED)
        resp = app_client.post("/download/jobs/r-done/retry")
        assert resp.status_code == 400

    def test_retry_nonexistent_job_returns_404(self, app_client):
        """Retrying a non-existent job returns 404."""
        resp = app_client.post("/download/jobs/no-such-job/retry")
        assert resp.status_code == 404


class TestPause:
    """POST /download/jobs/<id>/pause"""

    def test_pause_queued_job(self, app_client, jobs_db):
        """Pausing a QUEUED job sets it to PAUSED."""
        _make_job("p-q", JobStatus.QUEUED)
        resp = app_client.post("/download/jobs/p-q/pause")
        assert resp.status_code == 200
        assert get_job("p-q").status == JobStatus.PAUSED

    def test_pause_running_job(self, app_client, jobs_db):
        """Pausing a RUNNING job sets it to PAUSED."""
        _make_job("p-run", JobStatus.RUNNING)
        resp = app_client.post("/download/jobs/p-run/pause")
        assert resp.status_code == 200
        assert get_job("p-run").status == JobStatus.PAUSED

    def test_pause_completed_job_returns_400(self, app_client, jobs_db):
        """Pausing a COMPLETED job returns 400."""
        _make_job("p-done", JobStatus.COMPLETED)
        resp = app_client.post("/download/jobs/p-done/pause")
        assert resp.status_code == 400

    def test_pause_errored_job_returns_400(self, app_client, jobs_db):
        """Pausing an ERROR job returns 400."""
        _make_job("p-err", JobStatus.ERROR)
        resp = app_client.post("/download/jobs/p-err/pause")
        assert resp.status_code == 400

    def test_pause_nonexistent_job_returns_404(self, app_client):
        """Pausing a non-existent job returns 404."""
        resp = app_client.post("/download/jobs/no-such-job/pause")
        assert resp.status_code == 404


class TestResume:
    """POST /download/jobs/<id>/resume"""

    def test_resume_paused_job(self, app_client, jobs_db):
        """Resuming a PAUSED job sets it to QUEUED."""
        _make_job("s-pau", JobStatus.PAUSED)
        resp = app_client.post("/download/jobs/s-pau/resume")
        assert resp.status_code == 200
        assert get_job("s-pau").status == JobStatus.QUEUED

    def test_resume_queued_job_returns_400(self, app_client, jobs_db):
        """Resuming a QUEUED (not paused) job returns 400."""
        _make_job("s-q", JobStatus.QUEUED)
        resp = app_client.post("/download/jobs/s-q/resume")
        assert resp.status_code == 400

    def test_resume_running_job_returns_400(self, app_client, jobs_db):
        """Resuming a RUNNING job returns 400."""
        _make_job("s-run", JobStatus.RUNNING)
        resp = app_client.post("/download/jobs/s-run/resume")
        assert resp.status_code == 400

    def test_resume_completed_job_returns_400(self, app_client, jobs_db):
        """Resuming a COMPLETED job returns 400."""
        _make_job("s-done", JobStatus.COMPLETED)
        resp = app_client.post("/download/jobs/s-done/resume")
        assert resp.status_code == 400

    def test_resume_nonexistent_job_returns_404(self, app_client):
        """Resuming a non-existent job returns 404."""
        resp = app_client.post("/download/jobs/no-such-job/resume")
        assert resp.status_code == 404


class TestPurge:
    """POST /download/jobs/purge"""

    def test_purge_removes_terminal_jobs(self, app_client, jobs_db):
        """Purge deletes COMPLETED, ERROR, and CANCELLED jobs."""
        for i, st in enumerate([JobStatus.COMPLETED, JobStatus.ERROR, JobStatus.CANCELLED]):
            _make_job(f"purge-term-{i}", st)
        resp = app_client.post("/download/jobs/purge")
        assert resp.status_code == 200
        for i in range(3):
            assert get_job(f"purge-term-{i}") is None

    def test_purge_keeps_active_jobs(self, app_client, jobs_db):
        """Purge preserves QUEUED and RUNNING jobs."""
        _make_job("purge-keep-q", JobStatus.QUEUED)
        _make_job("purge-keep-r", JobStatus.RUNNING)
        _make_job("purge-remove", JobStatus.COMPLETED)
        resp = app_client.post("/download/jobs/purge")
        assert resp.status_code == 200
        assert get_job("purge-keep-q") is not None
        assert get_job("purge-keep-r") is not None
        assert get_job("purge-remove") is None

    def test_purge_returns_deleted_count(self, app_client, jobs_db):
        """Purge JSON response includes a count of deleted jobs."""
        _make_job("purge-cnt-1", JobStatus.COMPLETED)
        _make_job("purge-cnt-2", JobStatus.ERROR)
        resp = app_client.post("/download/jobs/purge")
        assert resp.get_json()["deleted"] == 2


class TestRetryAllErrored:
    """POST /download/jobs/retry-all-errored"""

    def test_retry_all_resets_errored_and_cancelled(self, app_client, jobs_db):
        """Batch retry sets ERROR and CANCELLED jobs back to QUEUED."""
        _make_job("ba-err", JobStatus.ERROR)
        _make_job("ba-canc", JobStatus.CANCELLED)
        _make_job("ba-done", JobStatus.COMPLETED)
        resp = app_client.post("/download/jobs/retry-all-errored")
        assert resp.status_code == 200
        assert get_job("ba-err").status == JobStatus.QUEUED
        assert get_job("ba-canc").status == JobStatus.QUEUED
        assert get_job("ba-done").status == JobStatus.COMPLETED  # untouched

    def test_retry_all_returns_count(self, app_client, jobs_db):
        """Batch retry response includes count of retried jobs."""
        _make_job("bc-1", JobStatus.ERROR)
        _make_job("bc-2", JobStatus.CANCELLED)
        _make_job("bc-3", JobStatus.COMPLETED)
        resp = app_client.post("/download/jobs/retry-all-errored")
        assert resp.get_json()["retried"] == 2

    def test_retry_all_with_no_errored_jobs(self, app_client, jobs_db):
        """Batch retry with no errored jobs returns count 0."""
        _make_job("bn-1", JobStatus.COMPLETED)
        _make_job("bn-2", JobStatus.QUEUED)
        resp = app_client.post("/download/jobs/retry-all-errored")
        assert resp.get_json()["retried"] == 0
