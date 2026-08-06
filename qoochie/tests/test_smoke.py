"""Smoke test — verify the application boots and the home page renders."""

from __future__ import annotations


def test_home_page_loads(app_client):
    """GET / returns HTTP 200 and the response body mentions qoochie."""
    response = app_client.get("/")
    assert response.status_code == 200
    assert "qoochie" in response.data.decode().lower()
