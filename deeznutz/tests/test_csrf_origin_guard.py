"""Regression tests for CSRF Origin/Referer guard on exempt endpoints."""

from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# (c) CSRF origin guard matrix
# ---------------------------------------------------------------------------

_exempt_paths = ["/download/enqueue", "/download/discography", "/playlist/resolve"]


@pytest.mark.parametrize("path", _exempt_paths)
def test_csrf_no_origin_allowed(app_client, path):
    """Server-to-server calls (no Origin/Referer) are allowed through."""
    resp = app_client.post(path, json={"url": "https://deezer.com/track/1"})
    # Must NOT be 403 CSRF-origin — may be 400/404 for bad payload, but not 403
    assert resp.status_code != 403, f"Blocked server-to-server call to {path}"


@pytest.mark.parametrize("path", _exempt_paths)
def test_csrf_same_origin_allowed(app_client, path):
    """Same-origin requests (Origin == request Host) are allowed."""
    resp = app_client.post(
        path,
        json={"url": "https://deezer.com/track/1"},
        headers={"Origin": "http://localhost"},
    )
    assert resp.status_code != 403, f"Blocked same-origin call to {path}"


@pytest.mark.parametrize("path", _exempt_paths)
def test_csrf_relative_referer_allowed(app_client, path):
    """Relative Referer (e.g. /download/enqueue) is same-origin → allowed."""
    resp = app_client.post(
        path,
        json={"url": "https://deezer.com/track/1"},
        headers={"Referer": path},
    )
    assert resp.status_code != 403, f"Blocked relative-referrer call to {path}"


@pytest.mark.parametrize("path", _exempt_paths)
def test_csrf_allowed_internal_origin(app_client, path):
    """Known internal origins (10.8.0.10, ry.n0g.xyz) are allowed."""
    for origin in ["http://10.8.0.10", "http://ry.n0g.xyz", "http://10.8.0.10:19297"]:
        resp = app_client.post(
            path,
            json={"url": "https://deezer.com/track/1"},
            headers={"Origin": origin},
        )
        assert resp.status_code != 403, f"Blocked allowed origin {origin} on {path}"


@pytest.mark.parametrize("path", _exempt_paths)
def test_csrf_foreign_origin_rejected(app_client, path):
    """Cross-origin from an unknown host → 403."""
    for origin in ["https://evil.com", "http://attacker.example.com", "https://malware.xyz"]:
        resp = app_client.post(
            path,
            json={"url": "https://deezer.com/track/1"},
            headers={"Origin": origin},
        )
        assert resp.status_code == 403, f"Did not block foreign origin {origin} on {path}"


def test_csrf_non_exempt_path_unaffected(app_client):
    """Non-exempt paths are not affected by the origin guard (SeaSurf handles them)."""
    # GET requests are never subject to CSRF
    resp = app_client.get("/health")
    assert resp.status_code == 200
