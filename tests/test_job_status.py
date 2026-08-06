"""Unit tests for the job-status proxy endpoint.

These stub outgoing HTTP calls so no network / live fleet is touched.
Run with:
    python -m pytest tests/ -q
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402
import requests as _requests  # noqa: E402
from unittest.mock import MagicMock, patch

from app import create_app  # noqa: E402
from app.search_aggregator.routes import (
    _normalize_status,
    _parse_html_status,
    _sse_read_last_event,
)
from app.search_aggregator import routes as _routes
from app.search_aggregator import fleet as _fleet
from app.search_aggregator import download as _download_mod
from app.search_aggregator import aggregator as _aggregator_mod

# All fleet HTTP goes through these shared sessions; tests stub their methods.
_FLIGHT_ROUTES_SESSION = _routes._search_session
_FLEET_SESSION = _fleet._fleet_session
_DOWNLOAD_SESSION = _download_mod._search_session
_AGGREGATOR_SESSION = _aggregator_mod._search_session

# Point every tool at a dead port so any accidental live call fails fast.
import app.search_aggregator.aggregator as agg
for _t in ("spotifryer", "qoochie", "tidalwave", "deeznutz"):
    agg.TOOLS[_t] = "http://127.0.0.1:1"


# ── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture
def client():
    app = create_app()
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


# ── Normalization ───────────────────────────────────────────────────────────

def test_normalize_status_standard_vocab():
    assert _normalize_status("queued") == "queued"
    assert _normalize_status("running") == "running"
    assert _normalize_status("completed") == "completed"
    assert _normalize_status("error") == "failed"
    assert _normalize_status("cancelled") == "cancelled"


def test_normalize_status_paused_maps_to_running():
    assert _normalize_status("paused") == "running"


def test_normalize_status_unknown_defaults_to_queued():
    assert _normalize_status("bogus") == "queued"
    assert _normalize_status("") == "queued"


def test_normalize_status_case_insensitive():
    assert _normalize_status("QUEUED") == "queued"
    assert _normalize_status("Error") == "failed"
    assert _normalize_status("COMPLETED") == "completed"


# ── HTML parser ─────────────────────────────────────────────────────────────

def test_parse_html_status_queued():
    html = '<span class="status-badge status-queued">queued</span>'
    r = _parse_html_status(html)
    assert r is not None
    assert r["status"] == "queued"
    assert r["progress"] is None
    assert r["error"] is None
    assert r["files"] is None


def test_parse_html_status_running_with_progress():
    html = (
        '<span class="status-badge status-running">running</span>'
        '<span class="progress-text">42%</span>'
    )
    r = _parse_html_status(html)
    assert r is not None and r["status"] == "running"
    assert r["progress"] == pytest.approx(0.42)


def test_parse_html_status_error():
    html = (
        '<span class="status-badge status-error">error</span>'
        '<span class="job-error">rate limit exceeded</span>'
    )
    r = _parse_html_status(html)
    assert r is not None and r["status"] == "error"
    assert r["error"] == "rate limit exceeded"


def test_parse_html_status_completed_with_files():
    html = (
        '<span class="status-badge status-completed">completed</span>'
        '<span class="job-files">5 files</span>'
    )
    r = _parse_html_status(html)
    assert r is not None and r["status"] == "completed"
    assert r["files"] == 5


def test_parse_html_status_no_match():
    assert _parse_html_status("<div>nothing here</div>") is None


# ── SSE reader ──────────────────────────────────────────────────────────────

def test_sse_read_last_event_returns_last_data(monkeypatch):
    """Simulate an SSE stream with two data events; should return the last."""
    # iter_lines(decode_unicode=True) yields str, not bytes.
    lines = [
        'data: {"type":"progress","progress":0.5}',
        '',
        ': heartbeat',
        'data: {"type":"status","status":"completed","progress":1.0}',
    ]

    mock_resp = MagicMock()
    mock_resp.iter_lines.return_value = iter(lines)
    mock_resp.close = MagicMock()

    def fake_get(url, stream=False, timeout=None):
        return mock_resp

    monkeypatch.setattr(_FLIGHT_ROUTES_SESSION, "get", fake_get)
    result = _sse_read_last_event("http://fake/events/abc")
    assert result is not None
    assert result["status"] == "completed"
    assert result["progress"] == 1.0


def test_sse_read_last_event_stops_on_terminal(monkeypatch):
    """Terminal event should stop reading immediately."""
    # iter_lines(decode_unicode=True) yields str, not bytes.
    lines = [
        'data: {"type":"status","status":"completed","progress":1.0}',
        'data: {"type":"progress","progress":0.99}',  # should never be reached
    ]

    mock_resp = MagicMock()
    mock_resp.iter_lines.return_value = iter(lines)
    mock_resp.close = MagicMock()

    def fake_get(url, stream=False, timeout=None):
        return mock_resp

    monkeypatch.setattr(_FLIGHT_ROUTES_SESSION, "get", fake_get)
    result = _sse_read_last_event("http://fake/events/abc")
    assert result["status"] == "completed"


def test_sse_read_returns_none_on_connection_error(monkeypatch):
    def fake_get(url, stream=False, timeout=None):
        raise _requests.ConnectionError("refused")

    monkeypatch.setattr(_FLIGHT_ROUTES_SESSION, "get", fake_get)
    assert _sse_read_last_event("http://fake/events/abc") is None


def test_sse_read_returns_none_on_timeout(monkeypatch):
    def fake_get(url, stream=False, timeout=None):
        raise _requests.Timeout("timed out")

    monkeypatch.setattr(_FLIGHT_ROUTES_SESSION, "get", fake_get)
    assert _sse_read_last_event("http://fake/events/abc") is None


def test_sse_read_returns_none_on_http_error(monkeypatch):
    mock_resp = MagicMock()
    mock_resp.raise_for_status.side_effect = _requests.HTTPError("500")

    def fake_get(url, stream=False, timeout=None):
        return mock_resp

    monkeypatch.setattr(_FLIGHT_ROUTES_SESSION, "get", fake_get)
    assert _sse_read_last_event("http://fake/events/abc") is None


# ── Route: invalid service ──────────────────────────────────────────────────

def test_job_status_invalid_service(client):
    resp = client.get("/download/bogus/abc123")
    assert resp.status_code == 400
    data = resp.get_json()
    assert "unknown service" in data["error"]


# ── Route: spotifryer (JSON endpoint) ──────────────────────────────────────

def test_job_status_spotifryer_happy_path(client, monkeypatch):
    """spotifryer returns JSON from /download/<id>/status."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "id": "abc123",
        "status": "running",
        "progress": 0.75,
        "files": ["a.opus", "b.opus"],
        "error": "",
    }
    mock_resp.raise_for_status = MagicMock()

    def fake_get(url, **kwargs):
        return mock_resp

    monkeypatch.setattr(_FLIGHT_ROUTES_SESSION, "get", fake_get)

    resp = client.get("/download/spotifryer/abc123")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["service"] == "spotifryer"
    assert data["job_id"] == "abc123"
    assert data["status"] == "running"
    assert data["progress"] == 0.75
    assert data["files"] == 2
    assert data["error"] is None


def test_job_status_spotifryer_not_found(client, monkeypatch):
    mock_resp = MagicMock()
    mock_resp.status_code = 404

    def fake_get(url, **kwargs):
        return mock_resp

    monkeypatch.setattr(_FLIGHT_ROUTES_SESSION, "get", fake_get)

    resp = client.get("/download/spotifryer/nope")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["status"] == "failed"
    assert data["error"] == "job not found"


def test_job_status_spotifryer_error_maps_to_failed(client, monkeypatch):
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "status": "error",
        "progress": 0.3,
        "files": [],
        "error": "auth expired",
    }
    mock_resp.raise_for_status = MagicMock()

    def fake_get(url, **kwargs):
        return mock_resp

    monkeypatch.setattr(_FLIGHT_ROUTES_SESSION, "get", fake_get)

    resp = client.get("/download/spotifryer/xyz")
    data = resp.get_json()
    assert data["status"] == "failed"
    assert data["error"] == "auth expired"


# ── Route: SSE tools (qoochie/tidalwave/deeznutz) ──────────────────────────

def test_job_status_sse_tool_terminal_event(client, monkeypatch):
    """tidalwave/deeznutz SSE returns a terminal event immediately."""
    # iter_lines(decode_unicode=True) yields str, not bytes.
    lines = [
        'data: {"type":"status","status":"completed","progress":1.0}',
    ]

    mock_resp = MagicMock()
    mock_resp.iter_lines.return_value = iter(lines)
    mock_resp.close = MagicMock()

    def fake_get(url, stream=False, timeout=None):
        return mock_resp

    monkeypatch.setattr(_FLIGHT_ROUTES_SESSION, "get", fake_get)

    resp = client.get("/download/tidalwave/job1")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["service"] == "tidalwave"
    assert data["job_id"] == "job1"
    assert data["status"] == "completed"
    assert data["progress"] == 1.0


def test_job_status_sse_tool_error_event(client, monkeypatch):
    """SSE event with status=error → normalized to failed."""
    # iter_lines(decode_unicode=True) yields str, not bytes.
    lines = [
        'data: {"type":"status","status":"error","progress":0.2}',
    ]

    mock_resp = MagicMock()
    mock_resp.iter_lines.return_value = iter(lines)
    mock_resp.close = MagicMock()

    def fake_get(url, stream=False, timeout=None):
        return mock_resp

    monkeypatch.setattr(_FLIGHT_ROUTES_SESSION, "get", fake_get)

    resp = client.get("/download/deeznutz/job2")
    data = resp.get_json()
    assert data["status"] == "failed"


def test_job_status_sse_tool_error_type_event(client, monkeypatch):
    """SSE event with type=error and no status key."""
    # iter_lines(decode_unicode=True) yields str, not bytes.
    lines = [
        'data: {"type":"error","error":"network failure"}',
    ]

    mock_resp = MagicMock()
    mock_resp.iter_lines.return_value = iter(lines)
    mock_resp.close = MagicMock()

    def fake_get(url, stream=False, timeout=None):
        return mock_resp

    monkeypatch.setattr(_FLIGHT_ROUTES_SESSION, "get", fake_get)

    resp = client.get("/download/qoochie/job3")
    data = resp.get_json()
    assert data["status"] == "failed"
    assert data["error"] == "network failure"


def test_job_status_sse_tool_falls_back_to_html(client, monkeypatch):
    """When SSE yields nothing (qoochie terminal job), fall back to HTML parse."""
    call_count = [0]

    def fake_get(url, stream=False, timeout=None):
        call_count[0] += 1
        mock_resp = MagicMock()
        if "events/" in url:
            # SSE: no data events, just heartbeats (empty iterator)
            mock_resp.iter_lines.return_value = iter([': heartbeat'])
            mock_resp.close = MagicMock()
            return mock_resp
        else:
            # HTML fallback
            mock_resp.status_code = 200
            mock_resp.text = (
                '<span class="status-badge status-completed">completed</span>'
                '<span class="job-files">3 files</span>'
            )
            mock_resp.raise_for_status = MagicMock()
            return mock_resp

    monkeypatch.setattr(_FLIGHT_ROUTES_SESSION, "get", fake_get)

    resp = client.get("/download/qoochie/job4")
    data = resp.get_json()
    assert data["status"] == "completed"
    assert data["files"] == 3
    assert call_count[0] == 2  # SSE + HTML fallback


def test_job_status_sse_tool_html_404(client, monkeypatch):
    """SSE fails, HTML returns 404 → job not found."""
    def fake_get(url, stream=False, timeout=None):
        mock_resp = MagicMock()
        if "events/" in url:
            mock_resp.iter_lines.return_value = iter([])
            mock_resp.close = MagicMock()
            return mock_resp
        else:
            mock_resp.status_code = 404
            mock_resp.raise_for_status = MagicMock()
            return mock_resp

    monkeypatch.setattr(_FLIGHT_ROUTES_SESSION, "get", fake_get)

    resp = client.get("/download/tidalwave/ghost")
    data = resp.get_json()
    assert data["status"] == "failed"
    assert data["error"] == "job not found"


# ── Route: graceful error on dead port ──────────────────────────────────────

def test_job_status_dead_port_returns_graceful_error(client):
    """When the tool is unreachable (dead port), return 200 + error JSON."""
    resp = client.get("/download/spotifryer/anyjob")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["status"] == "failed"
    assert data["error"] in ("unreachable", "timeout", None)


def test_job_status_sse_tool_dead_port(client):
    """SSE tool on dead port → graceful error, no 500."""
    resp = client.get("/download/qoochie/anyjob")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["status"] == "failed"
