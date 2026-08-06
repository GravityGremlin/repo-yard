"""Smoke test — verify the application boots and the home page renders."""

from __future__ import annotations


def test_home_page_loads(app_client):
    """GET / returns HTTP 200 and the response body mentions spotifryer."""
    response = app_client.get("/")
    assert response.status_code == 200
    assert b"spotifryer" in response.data.lower()


def test_health_check(app_client):
    """GET /health returns 200 with status ok."""
    resp = app_client.get("/health")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["status"] == "ok"


def test_downloads_page(app_client):
    """GET /download/jobs returns 200."""
    resp = app_client.get("/download/jobs")
    assert resp.status_code == 200


def test_library_page(app_client):
    """GET /library/ returns 200."""
    resp = app_client.get("/library/")
    assert resp.status_code == 200


def test_stats_page(app_client):
    """GET /stats/ returns 200."""
    resp = app_client.get("/stats/")
    assert resp.status_code == 200
