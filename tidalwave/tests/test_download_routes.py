"""Tests for the download routes — covers batch delete-selected.

Started with the 21 existing smoke tests as the regression floor and adds
focused coverage for ``POST /download/jobs/delete-selected``:

* JSON body ``{ids: [...]}`` deletes terminal jobs and skips active ones.
* Form body ``ids=id1,id2`` does the same (alt content-type path).
* Unknown IDs are silently ignored.
* Refreshing the jobs list returns the updated partial.
"""

from __future__ import annotations

import json

from app.models import Job, JobStatus, save_job, list_jobs


def _make_job(job_id: str, status: JobStatus) -> Job:
    job = Job(url=f"https://tidal.com/x/{job_id}", title=f"t-{job_id}",
              artist="a", kind="track", proxy_index=0, override_existing=False)
    job.id = job_id
    job.status = status
    save_job(job)
    return job


def test_delete_selected_json_deletes_terminal(app_client, jobs_db):
    a = _make_job("job-a", JobStatus.COMPLETED)
    b = _make_job("job-b", JobStatus.ERROR)
    c = _make_job("job-c", JobStatus.CANCELLED)
    _make_job("job-d", JobStatus.QUEUED)
    _make_job("job-e", JobStatus.RUNNING)

    resp = app_client.post(
        "/download/jobs/delete-selected",
        data=json.dumps({"ids": [a.id, b.id, c.id, "missing-id"]}),
        content_type="application/json",
    )

    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["deleted"] == 3
    assert payload["skipped_active"] == 0

    remaining_ids = {j.id for j in list_jobs(limit=100)}
    # Active jobs kept; terminal jobs gone.
    assert {"job-d", "job-e"} <= remaining_ids
    assert remaining_ids.isdisjoint({"job-a", "job-b", "job-c"})


def test_delete_selected_form_ids(app_client, jobs_db):
    a = _make_job("job-a", JobStatus.COMPLETED)
    b = _make_job("job-b", JobStatus.CANCELLED)

    resp = app_client.post(
        "/download/jobs/delete-selected",
        data={"ids": f"{a.id},{b.id}"},
        content_type="application/x-www-form-urlencoded",
    )

    assert resp.status_code == 200
    assert resp.get_json()["deleted"] == 2

    remaining_ids = {j.id for j in list_jobs(limit=100)}
    assert remaining_ids.isdisjoint({"job-a", "job-b"})


def test_delete_selected_skips_active(app_client, jobs_db):
    run = _make_job("job-run", JobStatus.RUNNING)
    queued = _make_job("job-queued", JobStatus.QUEUED)
    paused = _make_job("job-paused", JobStatus.PAUSED)

    resp = app_client.post(
        "/download/jobs/delete-selected",
        data=json.dumps({"ids": [run.id, queued.id, paused.id]}),
        content_type="application/json",
    )

    assert resp.status_code == 200
    assert resp.get_json()["deleted"] == 0
    assert resp.get_json()["skipped_active"] == 3
    remaining_ids = {j.id for j in list_jobs(limit=100)}
    assert remaining_ids >= {"job-run", "job-queued", "job-paused"}


def test_delete_selected_empty_returns_zero(app_client, jobs_db):
    resp = app_client.post(
        "/download/jobs/delete-selected",
        data=json.dumps({"ids": []}),
        content_type="application/json",
    )
    assert resp.status_code == 200
    assert resp.get_json()["deleted"] == 0


def test_delete_selected_htmx_returns_partial(app_client, jobs_db):
    a = _make_job("job-a", JobStatus.COMPLETED)
    resp = app_client.post(
        "/download/jobs/delete-selected",
        data=json.dumps({"ids": [a.id]}),
        content_type="application/json",
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "job-row" in body or "empty-state" in body
    assert "job-a" not in body


def test_delete_selected_existing_routes_unchanged(app_client, jobs_db):
    """Single-job delete still works for terminal jobs."""
    a = _make_job("job-a", JobStatus.COMPLETED)
    resp = app_client.post(f"/download/jobs/{a.id}/delete",
                           headers={"HX-Request": "true"})
    assert resp.status_code == 200
    remaining_ids = {j.id for j in list_jobs(limit=100)}
    assert "job-a" not in remaining_ids
