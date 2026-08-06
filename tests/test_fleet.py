"""Unit tests for the fleet adapter and job-control-plane routes.

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
from app.search_aggregator import fleet as _fleet  # noqa: E402
from app.search_aggregator import routes as _routes  # noqa: E402
from app.search_aggregator.fleet import (  # noqa: E402
    _parse_audit_html,
    _parse_stats_html,
    _normalize_job,
    _normalize_status,
    _parse_browse_html,
    _parse_jobs_html,
    _parse_recent_html,
    fetch_all_jobs,
    fetch_all_audit,
    fetch_all_stats,
    fetch_audit,
    fetch_auth_status,
    fetch_browse,
    fetch_browse_all,
    fetch_jobs,
    fetch_lyrics,
    fetch_recent,
    fetch_stats,
    fleet_action,
    job_action,
    library_serve_url,
    validate_credential,
)
import app.search_aggregator.aggregator as agg  # noqa: E402
from app.search_aggregator.aggregator import TOOLS  # noqa: E402

# Pooled HTTP session used at runtime; tests patch its .get/.post methods
# instead of the module-level requests.get because the fleet code path now
# routes through it for connection reuse.
_FLEET_SESSION = _fleet._fleet_session

# Point every tool at a dead port so any accidental live call fails fast.
for _t in ("spotifryer", "qoochie", "tidalwave", "deeznutz"):
    agg.TOOLS[_t] = "http://127.0.0.1:1"


# ── Fixtures ────────────────────────────────────────────────────────────

@pytest.fixture
def client():
    app = create_app()
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


# ── Status normalisation ────────────────────────────────────────────────

def test_fleet_normalize_status_standard_vocab():
    assert _normalize_status("queued") == "queued"
    assert _normalize_status("running") == "running"
    assert _normalize_status("completed") == "completed"
    assert _normalize_status("error") == "failed"
    assert _normalize_status("cancelled") == "cancelled"


def test_fleet_normalize_status_paused_maps_to_running():
    assert _normalize_status("paused") == "running"


def test_fleet_normalize_status_unknown_defaults_to_queued():
    assert _normalize_status("bogus") == "queued"
    assert _normalize_status("") == "queued"


def test_fleet_normalize_status_case_insensitive():
    assert _normalize_status("QUEUED") == "queued"
    assert _normalize_status("Error") == "failed"
    assert _normalize_status("COMPLETED") == "completed"


# ── HTML parsing ────────────────────────────────────────────────────────

def test_parse_jobs_html_single_running_job():
    html = (
        '<div class="jobs-list">'
        '<div id="job-abc" class="job-row-wrapper">'
        '<div class="job-row job-running" data-job-id="abc">'
        '<span class="job-title">Cool Track</span>'
        '<span class="status-badge status-running">running</span>'
        '<span class="progress-text">42%</span>'
        '</div></div></div>'
    )
    jobs = _parse_jobs_html(html)
    assert len(jobs) == 1
    j = jobs[0]
    assert j["id"] == "abc"
    assert j["title"] == "Cool Track"
    assert j["status"] == "running"
    assert j["progress"] == 0.42
    assert j["error"] is None
    assert j["files"] is None


def test_parse_jobs_html_completed_with_files():
    html = (
        '<div id="job-xyz" class="job-row-wrapper">'
        '<div class="job-row job-completed" data-job-id="xyz">'
        '<span class="job-title">Album</span>'
        '<span class="status-badge status-completed">completed</span>'
        '<span class="job-files">5 files</span>'
        '</div></div>'
    )
    jobs = _parse_jobs_html(html)
    assert len(jobs) == 1
    j = jobs[0]
    assert j["id"] == "xyz"
    assert j["status"] == "completed"
    assert j["files"] == 5


def test_parse_jobs_html_error_job():
    html = (
        '<div id="job-err1" class="job-row-wrapper">'
        '<div class="job-row job-error" data-job-id="err1">'
        '<span class="job-title">Broken</span>'
        '<span class="status-badge status-error">error</span>'
        '<span class="job-error">rate limit exceeded</span>'
        '</div></div>'
    )
    jobs = _parse_jobs_html(html)
    assert len(jobs) == 1
    j = jobs[0]
    assert j["id"] == "err1"
    assert j["status"] == "error"
    assert j["error"] == "rate limit exceeded"


def test_parse_jobs_html_multiple_jobs():
    html = (
        '<div class="jobs-list">'
        '<div id="job-1" class="job-row-wrapper">'
        '<div class="job-row job-queued" data-job-id="1">'
        '<span class="job-title">Track A</span>'
        '<span class="status-badge status-queued">queued</span>'
        '</div></div>'
        '<div id="job-2" class="job-row-wrapper">'
        '<div class="job-row job-running" data-job-id="2">'
        '<span class="job-title">Track B</span>'
        '<span class="status-badge status-running">running</span>'
        '<span class="progress-text">80%</span>'
        '</div></div>'
        '</div>'
    )
    jobs = _parse_jobs_html(html)
    assert len(jobs) == 2
    assert jobs[0]["id"] == "1"
    assert jobs[0]["status"] == "queued"
    assert jobs[1]["id"] == "2"
    assert jobs[1]["progress"] == 0.8


def test_parse_jobs_html_empty():
    assert _parse_jobs_html("") == []
    assert _parse_jobs_html("<div>nothing here</div>") == []


# ── Normalization: actions per status ────────────────────────────────────

def _make_raw_spotifryer_job(**overrides):
    """Build a raw dict matching spotifryer's JSON list shape."""
    base = {
        "id": "abc123",
        "url": "https://open.spotify.com/track/123",
        "title": "Test Track",
        "artist": "Test Artist",
        "kind": "track",
        "status": "running",
        "progress": 0.5,
        "error": "",
        "files": ["a.opus", "b.opus"],
        "phase": None,
        "created_at": "2026-01-01T00:00:00Z",
    }
    base.update(overrides)
    return base


def test_normalize_job_spotifryer_running():
    job = _normalize_job(_make_raw_spotifryer_job(), "spotifryer")
    assert job["id"] == "abc123"
    assert job["title"] == "Test Track"
    assert job["status"] == "running"
    assert job["progress"] == 0.5
    assert job["files"] == 2  # len(["a.opus", "b.opus"])
    assert job["error"] is None
    assert job["actions"] == ["pause", "cancel", "delete"]


def test_normalize_job_spotifryer_queued_has_priority():
    job = _normalize_job(
        _make_raw_spotifryer_job(status="queued", progress=0, files=[], error=""),
        "spotifryer",
    )
    assert job["status"] == "queued"
    assert "priority_up" in job["actions"]
    assert "priority_down" in job["actions"]
    assert "cancel" in job["actions"]
    assert "delete" in job["actions"]
    assert "pause" not in job["actions"]


def test_normalize_job_spotifryer_failed():
    job = _normalize_job(
        _make_raw_spotifryer_job(status="error", error="auth expired"),
        "spotifryer",
    )
    assert job["status"] == "failed"
    assert job["error"] == "auth expired"
    assert job["actions"] == ["retry", "delete"]


def test_normalize_job_spotifryer_completed():
    job = _normalize_job(
        _make_raw_spotifryer_job(status="completed", progress=1.0),
        "spotifryer",
    )
    assert job["status"] == "completed"
    assert job["actions"] == ["delete"]


def test_normalize_job_spotifryer_cancelled():
    job = _normalize_job(
        _make_raw_spotifryer_job(status="cancelled"),
        "spotifryer",
    )
    assert job["status"] == "cancelled"
    assert job["actions"] == ["retry", "delete"]


def test_normalize_job_qoochie_no_priority():
    """qoochie never offers priority actions regardless of status."""
    job = _normalize_job(
        {"id": "q1", "title": "Track", "status": "queued"},
        "qoochie",
    )
    assert job["status"] == "queued"
    assert "priority_up" not in job["actions"]
    assert "priority_down" not in job["actions"]
    assert "cancel" in job["actions"]


def test_normalize_job_tidalwave_has_priority():
    job = _normalize_job(
        {"id": "t1", "title": "Track", "status": "queued"},
        "tidalwave",
    )
    assert "priority_up" in job["actions"]
    assert "priority_down" in job["actions"]


def test_normalize_job_empty_files_list():
    job = _normalize_job(
        _make_raw_spotifryer_job(files=[]),
        "spotifryer",
    )
    assert job["files"] is None


def test_normalize_job_empty_error_string():
    job = _normalize_job(
        _make_raw_spotifryer_job(error=""),
        "spotifryer",
    )
    assert job["error"] is None


def test_normalize_job_missing_title_falls_back_to_id():
    job = _normalize_job({"id": "fallback-1", "status": "queued"}, "qoochie")
    assert job["title"] == "fallback-1"


def test_normalize_job_paused_maps_to_running():
    job = _normalize_job(
        {"id": "p1", "title": "Track", "status": "paused"},
        "spotifryer",
    )
    assert job["status"] == "running"


# ── fetch_jobs: service responses ───────────────────────────────────────

def _mock_json_response(data, status_code=200):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = data
    resp.content = json.dumps(data).encode()
    resp.raise_for_status = MagicMock()
    if status_code >= 400:
        resp.raise_for_status.side_effect = _requests.HTTPError(f"{status_code}")
    return resp


def _mock_html_response(html, status_code=200):
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = html
    resp.content = html.encode()
    resp.raise_for_status = MagicMock()
    if status_code >= 400:
        resp.raise_for_status.side_effect = _requests.HTTPError(f"{status_code}")
    return resp


def test_fetch_jobs_spotifryer_json(monkeypatch):
    """spotifryer returns JSON from /download/jobs/data."""
    raw_jobs = [
        {"id": "a1", "title": "Track A", "status": "running", "progress": 0.6,
         "files": ["x.opus"], "error": ""},
        {"id": "a2", "title": "Track B", "status": "completed", "progress": 1.0,
         "files": ["y.opus", "z.opus"], "error": ""},
    ]
    monkeypatch.setattr(
        _FLEET_SESSION, "get", lambda url, timeout=None: _mock_json_response(raw_jobs),
    )
    result = fetch_jobs("spotifryer")
    assert result["error"] is None
    assert len(result["jobs"]) == 2
    assert result["jobs"][0]["id"] == "a1"
    assert result["jobs"][0]["status"] == "running"
    assert result["jobs"][0]["files"] == 1
    assert result["jobs"][1]["status"] == "completed"
    assert result["jobs"][1]["files"] == 2


def test_fetch_jobs_html_service(monkeypatch):
    """qoochie/tidalwave/deeznutz return HTML list partials."""
    html = (
        '<div class="jobs-list">'
        '<div id="job-q1" class="job-row-wrapper">'
        '<div class="job-row job-queued" data-job-id="q1">'
        '<span class="job-title">Hello</span>'
        '<span class="status-badge status-queued">queued</span>'
        '</div></div>'
        '</div>'
    )
    monkeypatch.setattr(
        _FLEET_SESSION, "get", lambda url, timeout=None: _mock_html_response(html),
    )
    result = fetch_jobs("qoochie")
    assert result["error"] is None
    assert len(result["jobs"]) == 1
    assert result["jobs"][0]["id"] == "q1"
    assert result["jobs"][0]["status"] == "queued"


def test_fetch_jobs_unreachable(monkeypatch):
    """Unreachable tool → graceful error, no raise."""
    def fake_get(url, timeout=None):
        raise _requests.ConnectionError("refused")

    monkeypatch.setattr(_FLEET_SESSION, "get", fake_get)
    for svc in ("spotifryer", "qoochie", "tidalwave", "deeznutz"):
        result = fetch_jobs(svc)
        assert result["jobs"] == []
        assert result["error"] is not None


def test_fetch_jobs_timeout(monkeypatch):
    """Timeout → graceful error."""
    def fake_get(url, timeout=None):
        raise _requests.Timeout("timed out")

    monkeypatch.setattr(_FLEET_SESSION, "get", fake_get)
    result = fetch_jobs("spotifryer")
    assert result["jobs"] == []
    assert result["error"] is not None


def test_fetch_jobs_auth_expired(monkeypatch):
    """401 → auth_expired error."""
    monkeypatch.setattr(
        _FLEET_SESSION, "get", lambda url, timeout=None: _mock_json_response({}, status_code=401),
    )
    result = fetch_jobs("spotifryer")
    assert result["jobs"] == []
    assert result["error"] == "auth_expired"


def test_fetch_jobs_unknown_service():
    result = fetch_jobs("bogus")
    assert result["jobs"] == []
    assert "unknown service" in result["error"]


def test_fetch_jobs_empty_list(monkeypatch):
    """Empty job list → empty array, no error."""
    monkeypatch.setattr(
        _FLEET_SESSION, "get", lambda url, timeout=None: _mock_json_response([]),
    )
    result = fetch_jobs("spotifryer")
    assert result["jobs"] == []
    assert result["error"] is None


def test_fetch_jobs_non_list_json(monkeypatch):
    """If JSON is not a list (e.g. an error dict), treat as empty."""
    monkeypatch.setattr(
        _FLEET_SESSION, "get", lambda url, timeout=None: _mock_json_response({"error": "bad"}),
    )
    result = fetch_jobs("spotifryer")
    assert result["jobs"] == []


# ── fetch_all_jobs ──────────────────────────────────────────────────────

def test_fetch_all_jobs_parallel(monkeypatch):
    """fetch_all_jobs returns results for all four services."""
    call_log = []

    def fake_fetch(service):
        call_log.append(service)
        return {"jobs": [{"id": f"{service}-1", "title": "T", "status": "queued"}], "error": None}

    monkeypatch.setattr("app.search_aggregator.fleet.fetch_jobs", fake_fetch)
    result = fetch_all_jobs()
    assert "services" in result
    assert set(result["services"].keys()) == {"spotifryer", "qoochie", "tidalwave", "deeznutz"}
    for svc in ("spotifryer", "qoochie", "tidalwave", "deeznutz"):
        assert result["services"][svc]["error"] is None
        assert len(result["services"][svc]["jobs"]) == 1


def test_fetch_all_jobs_one_unreachable(monkeypatch):
    """If one service fails, others still succeed."""
    def fake_fetch(service):
        if service == "qoochie":
            return {"jobs": [], "error": "unreachable"}
        return {"jobs": [{"id": f"{service}-1", "title": "T", "status": "queued"}], "error": None}

    monkeypatch.setattr("app.search_aggregator.fleet.fetch_jobs", fake_fetch)
    result = fetch_all_jobs()
    assert result["services"]["qoochie"]["error"] == "unreachable"
    assert len(result["services"]["spotifryer"]["jobs"]) == 1
    assert len(result["services"]["tidalwave"]["jobs"]) == 1
    assert len(result["services"]["deeznutz"]["jobs"]) == 1


# ── job_action ──────────────────────────────────────────────────────────

def test_job_action_spotifryer_flat_url(monkeypatch):
    """spotifryer uses flat URL: /download/<job_id>/cancel."""
    captured_urls = []

    def fake_post(url, timeout=None):
        captured_urls.append(url)
        return _mock_json_response({"ok": True})

    monkeypatch.setattr(_FLEET_SESSION, "post", fake_post)
    result = job_action("spotifryer", "j123", "cancel")
    assert result["ok"] is True
    assert captured_urls[0].endswith("/download/j123/cancel")


def test_job_action_nested_url(monkeypatch):
    """qoochie/tidalwave/deeznutz use nested URL: /download/jobs/<job_id>/cancel."""
    captured_urls = []

    def fake_post(url, timeout=None):
        captured_urls.append(url)
        return _mock_json_response({"status": "ok"})

    monkeypatch.setattr(_FLEET_SESSION, "post", fake_post)
    for svc in ("qoochie", "tidalwave", "deeznutz"):
        captured_urls.clear()
        result = job_action(svc, "j456", "retry")
        assert result["ok"] is True
        assert f"/download/jobs/j456/retry" in captured_urls[0]


def test_job_action_priority_spotifryer_flat(monkeypatch):
    """spotifryer priority URL: /download/<job_id>/priority/up."""
    captured_urls = []

    def fake_post(url, timeout=None):
        captured_urls.append(url)
        return _mock_json_response({"status": "ok"})

    monkeypatch.setattr(_FLEET_SESSION, "post", fake_post)
    result = job_action("spotifryer", "j789", "priority_up")
    assert result["ok"] is True
    assert captured_urls[0].endswith("/download/j789/priority/up")


def test_job_action_priority_nested(monkeypatch):
    """tidalwave priority URL: /download/jobs/<job_id>/priority/down."""
    captured_urls = []

    def fake_post(url, timeout=None):
        captured_urls.append(url)
        return _mock_json_response({"status": "ok"})

    monkeypatch.setattr(_FLEET_SESSION, "post", fake_post)
    result = job_action("tidalwave", "j101", "priority_down")
    assert result["ok"] is True
    assert "/download/jobs/j101/priority/down" in captured_urls[0]


def test_job_action_not_found(monkeypatch):
    """404 → error envelope."""
    monkeypatch.setattr(
        _FLEET_SESSION, "post", lambda url, timeout=None: _mock_json_response({}, status_code=404),
    )
    result = job_action("spotifryer", "ghost", "cancel")
    assert result["ok"] is False
    assert result["error"] == "job not found"


def test_job_action_server_error(monkeypatch):
    """500 → error envelope."""
    monkeypatch.setattr(
        _FLEET_SESSION, "post", lambda url, timeout=None: _mock_json_response(
            {"error": "internal boom"}, status_code=500,
        ),
    )
    result = job_action("qoochie", "j1", "delete")
    assert result["ok"] is False
    assert result["error"] == "internal boom"


def test_job_action_unreachable(monkeypatch):
    """Connection error → graceful error."""
    def fake_post(url, timeout=None):
        raise _requests.ConnectionError("refused")

    monkeypatch.setattr(_FLEET_SESSION, "post", fake_post)
    result = job_action("tidalwave", "j1", "pause")
    assert result["ok"] is False
    assert result["error"] is not None


def test_job_action_unknown_service():
    result = job_action("bogus", "j1", "cancel")
    assert result["ok"] is False
    assert "unknown service" in result["error"]


def test_job_action_invalid_action():
    result = job_action("spotifryer", "j1", "explode")
    assert result["ok"] is False
    assert "invalid action" in result["error"]


# ── fleet_action ────────────────────────────────────────────────────────

def test_fleet_action_purge(monkeypatch):
    captured_urls = []

    def fake_post(url, timeout=None):
        captured_urls.append(url)
        return _mock_json_response({"deleted": 5})

    monkeypatch.setattr(_FLEET_SESSION, "post", fake_post)
    result = fleet_action("qoochie", "purge")
    assert result["ok"] is True
    assert result["count"] == 5
    assert captured_urls[0].endswith("/download/jobs/purge")


def test_fleet_action_retry_all(monkeypatch):
    def fake_post(url, timeout=None):
        return _mock_json_response({"retried": 3})

    monkeypatch.setattr(_FLEET_SESSION, "post", fake_post)
    result = fleet_action("tidalwave", "retry_all")
    assert result["ok"] is True
    assert result["count"] == 3


def test_fleet_action_unreachable(monkeypatch):
    def fake_post(url, timeout=None):
        raise _requests.ConnectionError("refused")

    monkeypatch.setattr(_FLEET_SESSION, "post", fake_post)
    result = fleet_action("deeznutz", "purge")
    assert result["ok"] is False
    assert result["error"] is not None


def test_fleet_action_unknown_service():
    result = fleet_action("bogus", "purge")
    assert result["ok"] is False
    assert "unknown service" in result["error"]


def test_fleet_action_invalid_action():
    result = fleet_action("spotifryer", "explode_all")
    assert result["ok"] is False
    assert "invalid fleet action" in result["error"]


def test_fleet_action_server_error(monkeypatch):
    monkeypatch.setattr(
        _FLEET_SESSION, "post", lambda url, timeout=None: _mock_json_response(
            {"error": "locked"}, status_code=423,
        ),
    )
    result = fleet_action("qoochie", "purge")
    assert result["ok"] is False
    assert result["error"] == "locked"


# ── Route tests ─────────────────────────────────────────────────────────

def test_route_job_action_invalid_service(client):
    resp = client.post("/jobs/bogus/abc123/cancel")
    assert resp.status_code == 400
    data = resp.get_json()
    assert "unknown service" in data["error"]


def test_route_job_action_invalid_action(client):
    resp = client.post("/jobs/spotifryer/abc123/explode")
    assert resp.status_code == 400
    data = resp.get_json()
    assert "invalid action" in data["error"]


def test_route_job_action_success(client, monkeypatch):
    monkeypatch.setattr(
        "app.search_aggregator.routes.job_action",
        lambda s, j, a: {"ok": True, "status": None, "error": None},
    )
    resp = client.post("/jobs/spotifryer/j1/cancel")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True


def test_route_job_action_hx_request(client, monkeypatch):
    monkeypatch.setattr(
        "app.search_aggregator.routes.job_action",
        lambda s, j, a: {"ok": True, "status": None, "error": None},
    )
    resp = client.post(
        "/jobs/tidalwave/j2/retry",
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 200
    assert b"dl-chip ok" in resp.data


def test_route_job_action_hx_request_error(client, monkeypatch):
    monkeypatch.setattr(
        "app.search_aggregator.routes.job_action",
        lambda s, j, a: {"ok": False, "status": None, "error": "job not found"},
    )
    resp = client.post(
        "/jobs/qoochie/ghost/delete",
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 200
    assert b"dl-chip err" in resp.data
    assert b"job not found" in resp.data


def test_route_fleet_action_invalid_service(client):
    resp = client.post("/jobs/bogus/purge")
    assert resp.status_code == 400
    assert "unknown service" in resp.get_json()["error"]


def test_route_fleet_action_invalid_action(client):
    resp = client.post("/jobs/spotifryer/explode_all")
    assert resp.status_code == 400
    assert "invalid fleet action" in resp.get_json()["error"]


def test_route_fleet_action_success(client, monkeypatch):
    monkeypatch.setattr(
        "app.search_aggregator.routes.fleet_action",
        lambda s, a: {"ok": True, "count": 7, "error": None},
    )
    resp = client.post("/jobs/deeznutz/purge")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["count"] == 7


def test_route_fleet_action_hx_request(client, monkeypatch):
    monkeypatch.setattr(
        "app.search_aggregator.routes.fleet_action",
        lambda s, a: {"ok": True, "count": 3, "error": None},
    )
    resp = client.post(
        "/jobs/qoochie/retry_all",
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 200
    assert b"dl-chip ok" in resp.data
    assert b"3 affected" in resp.data


# ══════════════════════════════════════════════════════════════════════════
# Library adapter tests
# ══════════════════════════════════════════════════════════════════════════

# ── HTML browse parsing ─────────────────────────────────────────────────

def test_parse_browse_html_dirs_and_files():
    html = (
        '<table class="library-table"><tbody>'
        '<tr class="dir">'
        '  <td><a href="#">Artist One</a></td>'
        '  <td>—</td>'
        '  <td>2026-01-01</td>'
        '  <td></td>'
        '</tr>'
        '<tr class="file">'
        '  <td><a href="/library/serve/Artist%20One/Album/track.opus">Track One</a></td>'
        '  <td>4.2 MB</td>'
        '  <td>2026-01-02</td>'
        '  <td></td>'
        '</tr>'
        '</tbody></table>'
    )
    items = _parse_browse_html(html)
    assert len(items) == 2
    assert items[0]["kind"] == "dir"
    assert items[0]["name"] == "Artist One"
    assert items[1]["kind"] == "file"
    assert items[1]["name"] == "Track One"


def test_parse_browse_html_empty():
    assert _parse_browse_html("<div>nothing</div>") == []
    assert _parse_browse_html("") == []


def test_parse_browse_html_size_parsing():
    html = (
        '<tr class="file">'
        '<td><a href="#">Song</a></td>'
        '<td>4.2 MB</td>'
        '<td>2026-01-01</td>'
        '<td></td>'
        '</tr>'
    )
    items = _parse_browse_html(html)
    assert len(items) == 1
    assert items[0]["size"] is not None
    assert items[0]["size"] > 4_000_000  # ~4.2 MB


# ── Recent HTML parsing ─────────────────────────────────────────────────

def test_parse_recent_html():
    html = (
        '<tr class="file">'
        '<td><a href="/library/serve/Artist/Album/track.opus">Track</a></td>'
        '<td>3.1 MB</td>'
        '<td>2026-03-01</td>'
        '<td></td>'
        '</tr>'
    )
    items = _parse_recent_html(html)
    assert len(items) == 1
    assert items[0]["name"] == "Track"
    assert items[0]["kind"] == "file"
    assert items[0]["path"] == "Artist/Album/track.opus"


def test_parse_recent_html_empty():
    assert _parse_recent_html("") == []


# ── fetch_browse ────────────────────────────────────────────────────────

def test_fetch_browse_spotifryer_json(monkeypatch):
    """spotifryer returns JSON from /library/browse."""
    raw = {
        "path": "Pink Floyd",
        "items": [
            {"name": "The Dark Side of the Moon", "is_dir": True, "size": None, "mtime": 0, "ext": None},
            {"name": "track.opus", "is_dir": False, "size": 5000000, "mtime": 0, "ext": ".opus"},
        ],
    }
    monkeypatch.setattr(
        _FLEET_SESSION, "get", lambda url, timeout=None: _mock_json_response(raw),
    )
    result = fetch_browse("spotifryer", "Pink Floyd")
    assert result["error"] is None
    assert len(result["items"]) == 2
    assert result["items"][0]["kind"] == "dir"
    assert result["items"][0]["name"] == "The Dark Side of the Moon"
    assert result["items"][1]["kind"] == "file"
    assert result["items"][1]["size"] == 5000000
    # Path is relative to browse path.
    assert result["items"][0]["path"] == "Pink Floyd/The Dark Side of the Moon"
    assert result["items"][1]["path"] == "Pink Floyd/track.opus"


def test_fetch_browse_spotifryer_root(monkeypatch):
    """Root browse: path items use bare names."""
    raw = {"path": "", "items": [{"name": "Artist", "is_dir": True, "size": None, "mtime": 0, "ext": None}]}
    monkeypatch.setattr(
        _FLEET_SESSION, "get", lambda url, timeout=None: _mock_json_response(raw),
    )
    result = fetch_browse("spotifryer", "/")
    assert result["items"][0]["path"] == "Artist"


def test_fetch_browse_html_service(monkeypatch):
    """qoochie/tidalwave/deeznutz return HTML browse partials."""
    html = (
        '<tr class="dir">'
        '<td><a href="#">My Artist</a></td>'
        '<td>—</td><td>2026-01-01</td><td></td>'
        '</tr>'
    )
    monkeypatch.setattr(
        _FLEET_SESSION, "get", lambda url, timeout=None: _mock_html_response(html),
    )
    result = fetch_browse("qoochie", "/")
    assert result["error"] is None
    assert len(result["items"]) == 1
    assert result["items"][0]["name"] == "My Artist"
    assert result["items"][0]["kind"] == "dir"


def test_fetch_browse_unreachable(monkeypatch):
    def fake_get(url, timeout=None):
        raise _requests.ConnectionError("refused")

    monkeypatch.setattr(_FLEET_SESSION, "get", fake_get)
    result = fetch_browse("tidalwave", "/")
    assert result["items"] == []
    assert result["error"] is not None


def test_fetch_browse_unknown_service():
    result = fetch_browse("bogus", "/")
    assert result["items"] == []
    assert "unknown service" in result["error"]


def test_fetch_browse_404(monkeypatch):
    monkeypatch.setattr(
        _FLEET_SESSION, "get", lambda url, timeout=None: _mock_json_response({}, status_code=404),
    )
    result = fetch_browse("spotifryer", "nonexistent")
    assert result["items"] == []
    assert result["error"] == "not found"


# ── fetch_browse_all ────────────────────────────────────────────────────

def test_fetch_browse_all_parallel(monkeypatch):
    def fake_fetch(service, path="/"):
        return {"items": [{"name": f"{service}-item", "path": "", "kind": "dir", "size": None}], "error": None}

    monkeypatch.setattr("app.search_aggregator.fleet.fetch_browse", fake_fetch)
    result = fetch_browse_all("/")
    assert "services" in result
    assert set(result["services"].keys()) == {"spotifryer", "qoochie", "tidalwave", "deeznutz"}
    for svc in TOOLS:
        assert len(result["services"][svc]["items"]) == 1


# ── fetch_recent ────────────────────────────────────────────────────────

def test_fetch_recent_spotifryer_json(monkeypatch):
    """spotifryer recent returns JSON job dicts."""
    raw = [
        {"id": "j1", "title": "Cool Song", "files": ["cool.opus"], "status": "completed"},
        {"id": "j2", "title": "Another", "files": ["a.opus", "b.opus"], "status": "completed"},
    ]
    monkeypatch.setattr(
        _FLEET_SESSION, "get", lambda url, timeout=None: _mock_json_response(raw),
    )
    result = fetch_recent("spotifryer")
    assert result["error"] is None
    assert len(result["items"]) == 2
    assert result["items"][0]["name"] == "Cool Song"
    assert result["items"][0]["path"] == "cool.opus"
    assert result["items"][1]["name"] == "Another"


def test_fetch_recent_html_service(monkeypatch):
    html = (
        '<tr class="file">'
        '<td><a href="/library/serve/A/B/song.opus">Song</a></td>'
        '<td>2.5 MB</td><td>2026-04-01</td><td></td>'
        '</tr>'
    )
    monkeypatch.setattr(
        _FLEET_SESSION, "get", lambda url, timeout=None: _mock_html_response(html),
    )
    result = fetch_recent("deeznutz")
    assert result["error"] is None
    assert len(result["items"]) == 1
    assert result["items"][0]["name"] == "Song"
    assert result["items"][0]["path"] == "A/B/song.opus"


def test_fetch_recent_unreachable(monkeypatch):
    def fake_get(url, timeout=None):
        raise _requests.ConnectionError("refused")

    monkeypatch.setattr(_FLEET_SESSION, "get", fake_get)
    result = fetch_recent("qoochie")
    assert result["items"] == []
    assert result["error"] is not None


def test_fetch_recent_unknown_service():
    result = fetch_recent("bogus")
    assert result["items"] == []
    assert "unknown service" in result["error"]


# ── library_serve_url ───────────────────────────────────────────────────

def test_library_serve_url_spotifryer():
    url = library_serve_url("spotifryer", "Pink Floyd/Album/track.opus")
    assert url == "http://127.0.0.1:1/library/serve/Pink Floyd/Album/track.opus"


def test_library_serve_url_unknown():
    assert library_serve_url("bogus", "path") == ""


# ── Route tests: library ────────────────────────────────────────────────

def test_route_library_serve_invalid_service(client):
    resp = client.get("/library/serve/bogus/some/file.opus")
    assert resp.status_code == 404
    assert "unknown service" in resp.get_json()["error"]


def test_route_library_serve_streams_bytes(client, monkeypatch):
    """Serve route streams bytes from the upstream tool."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.headers = {"Content-Type": "audio/ogg", "Content-Length": "1234"}
    mock_resp.iter_content.return_value = [b"audio-bytes-here"]
    mock_resp.close = MagicMock()

    def fake_get(url, timeout=None, stream=False):
        return mock_resp

    monkeypatch.setattr(_routes._search_session, "get", fake_get)

    resp = client.get("/library/serve/spotifryer/Artist/Album/track.opus")
    assert resp.status_code == 200
    assert resp.data == b"audio-bytes-here"
    assert resp.content_type == "audio/ogg"


def test_route_library_serve_upstream_404(client, monkeypatch):
    mock_resp = MagicMock()
    mock_resp.status_code = 404

    def fake_get(url, timeout=None, stream=False):
        return mock_resp

    monkeypatch.setattr(_routes._search_session, "get", fake_get)
    resp = client.get("/library/serve/qoochie/no/such/file.opus")
    assert resp.status_code == 404
    assert "not found" in resp.get_json()["error"]


def test_route_library_serve_upstream_unreachable(client, monkeypatch):
    def fake_get(url, timeout=None, stream=False):
        raise _requests.ConnectionError("refused")

    monkeypatch.setattr(_routes._search_session, "get", fake_get)
    resp = client.get("/library/serve/tidalwave/file.opus")
    assert resp.status_code == 502


# ══════════════════════════════════════════════════════════════════════════
# Phase 3 — Auth status + credential validation
# ══════════════════════════════════════════════════════════════════════════

# ── fetch_auth_status ────────────────────────────────────────────────────

def test_fetch_auth_status_spotifryer_ok(monkeypatch):
    """spotifryer returns JSON auth status."""
    monkeypatch.setattr(
        _FLEET_SESSION, "get", lambda url, timeout=None: _mock_json_response({
            "status": "ok",
            "label": "Spotify Free",
            "last_updated": "2026-01-15T10:00:00Z",
            "error": None,
        }),
    )
    result = fetch_auth_status("spotifryer")
    assert result["service"] == "spotifryer"
    assert result["status"] == "ok"
    assert result["label"] == "Spotify Free"
    assert result["last_updated"] == "2026-01-15T10:00:00Z"
    assert result["error"] is None


def test_fetch_auth_status_qoochie_expired(monkeypatch):
    """qoochie reports expired auth."""
    monkeypatch.setattr(
        _FLEET_SESSION, "get", lambda url, timeout=None: _mock_json_response({
            "status": "expired",
            "label": "Qobuz",
            "last_updated": None,
            "error": None,
        }),
    )
    result = fetch_auth_status("qoochie")
    assert result["status"] == "expired"
    assert result["label"] == "Qobuz"


def test_fetch_auth_status_tidalwave_valid_alias(monkeypatch):
    """tidalwave returns 'valid' which maps to 'ok'."""
    monkeypatch.setattr(
        _FLEET_SESSION, "get", lambda url, timeout=None: _mock_json_response({
            "status": "valid",
            "label": "TIDAL HiFi",
            "updated_at": "2026-02-01T00:00:00Z",
            "error": None,
        }),
    )
    result = fetch_auth_status("tidalwave")
    assert result["status"] == "ok"
    assert result["last_updated"] == "2026-02-01T00:00:00Z"


def test_fetch_auth_status_deeznutz_missing(monkeypatch):
    """deeznutz reports missing credentials."""
    monkeypatch.setattr(
        _FLEET_SESSION, "get", lambda url, timeout=None: _mock_json_response({
            "status": "missing",
            "label": "",
            "error": None,
        }),
    )
    result = fetch_auth_status("deeznutz")
    assert result["status"] == "missing"
    assert result["label"] == ""


def test_fetch_auth_status_401_maps_to_expired(monkeypatch):
    """HTTP 401 from the status endpoint → expired."""
    monkeypatch.setattr(
        _FLEET_SESSION, "get", lambda url, timeout=None: _mock_json_response({}, status_code=401),
    )
    result = fetch_auth_status("spotifryer")
    assert result["status"] == "expired"
    assert result["error"] == "auth expired"


def test_fetch_auth_status_unreachable(monkeypatch):
    def fake_get(url, timeout=None):
        raise _requests.ConnectionError("refused")

    monkeypatch.setattr(_FLEET_SESSION, "get", fake_get)
    result = fetch_auth_status("qoochie")
    assert result["status"] == "unknown"
    assert result["error"] is not None


def test_fetch_auth_status_unknown_service():
    result = fetch_auth_status("bogus")
    assert result["status"] == "unknown"
    assert "unknown service" in result["error"]


def test_fetch_auth_status_non_json_response(monkeypatch):
    """If response isn't JSON, graceful parse_error."""
    resp = MagicMock()
    resp.status_code = 200
    resp.json.side_effect = ValueError("not json")
    resp.raise_for_status = MagicMock()

    monkeypatch.setattr(_FLEET_SESSION, "get", lambda url, timeout=None: resp)
    result = fetch_auth_status("spotifryer")
    assert result["status"] == "unknown"
    assert result["error"] == "parse_error"


# ── validate_credential ──────────────────────────────────────────────────

def test_validate_credential_qoochie_token(monkeypatch):
    monkeypatch.setattr(
        _FLEET_SESSION, "post", lambda url, json=None, timeout=None: _mock_json_response({"ok": True}),
    )
    result = validate_credential("qoochie", token="my_qobuz_token")
    assert result["ok"] is True
    assert result["error"] is None


def test_validate_credential_tidalwave_token(monkeypatch):
    monkeypatch.setattr(
        _FLEET_SESSION, "post", lambda url, json=None, timeout=None: _mock_json_response({"ok": True}),
    )
    result = validate_credential("tidalwave", token="tidal_token_123")
    assert result["ok"] is True


def test_validate_credential_deeznutz_arl(monkeypatch):
    monkeypatch.setattr(
        _FLEET_SESSION, "post", lambda url, json=None, timeout=None: _mock_json_response({"ok": True}),
    )
    result = validate_credential("deeznutz", arl="arl_abc123")
    assert result["ok"] is True


def test_validate_credential_spotifryer_not_supported():
    result = validate_credential("spotifryer", token="x")
    assert result["ok"] is False
    assert "does not support" in result["error"]


def test_validate_credential_401_invalid(monkeypatch):
    monkeypatch.setattr(
        _FLEET_SESSION, "post", lambda url, json=None, timeout=None: _mock_json_response({}, status_code=401),
    )
    result = validate_credential("qoochie", token="bad_token")
    assert result["ok"] is False
    assert result["error"] == "invalid credential"


def test_validate_credential_server_error(monkeypatch):
    monkeypatch.setattr(
        _FLEET_SESSION, "post", lambda url, json=None, timeout=None: _mock_json_response(
            {"error": "service down"}, status_code=500,
        ),
    )
    result = validate_credential("tidalwave", token="tok")
    assert result["ok"] is False
    assert result["error"] == "service down"


def test_validate_credential_unreachable(monkeypatch):
    def fake_post(url, json=None, timeout=None):
        raise _requests.ConnectionError("refused")

    monkeypatch.setattr(_FLEET_SESSION, "post", fake_post)
    result = validate_credential("deeznutz", arl="arl")
    assert result["ok"] is False
    assert result["error"] is not None


def test_validate_credential_unknown_service():
    result = validate_credential("bogus", token="x")
    assert result["ok"] is False
    assert "unknown service" in result["error"]


def test_validate_credential_missing_credential():
    """Calling with no token/arl → error."""
    result = validate_credential("qoochie")
    assert result["ok"] is False
    assert "missing credential" in result["error"]


def test_validate_credential_response_with_ok_false(monkeypatch):
    """Tool returns {"ok": false, "error": "..."}."""
    monkeypatch.setattr(
        _FLEET_SESSION, "post", lambda url, json=None, timeout=None: _mock_json_response(
            {"ok": False, "error": "expired token"},
        ),
    )
    result = validate_credential("qoochie", token="tok")
    assert result["ok"] is False
    assert result["error"] == "expired token"


# ══════════════════════════════════════════════════════════════════════════
# Phase 3 — Stats proxy
# ══════════════════════════════════════════════════════════════════════════

# ── _parse_stats_html ────────────────────────────────────────────────────

def test_parse_stats_html_data_field_attrs():
    """data-field attributes are the most reliable extraction path."""
    html = (
        '<div class="stats-grid">'
        '<div class="stat-card" data-field="downloads_today">'
        '  <span class="stat-value">5</span>'
        '  <span class="stat-label">Downloads Today</span>'
        '</div>'
        '<div class="stat-card" data-field="files_managed">'
        '  <span class="stat-value">1,234</span>'
        '  <span class="stat-label">Files Managed</span>'
        '</div>'
        '<div class="stat-card" data-field="jobs_active">'
        '  <span class="stat-value">2</span>'
        '  <span class="stat-label">Active Jobs</span>'
        '</div>'
        '<div class="stat-card" data-field="jobs_queued">'
        '  <span class="stat-value">7</span>'
        '  <span class="stat-label">Queued Jobs</span>'
        '</div>'
        '<div class="stat-card" data-field="library_size_bytes">'
        '  <span class="stat-value">50 GB</span>'
        '  <span class="stat-label">Library Size</span>'
        '</div>'
        '</div>'
    )
    stats = _parse_stats_html(html)
    assert stats["downloads_today"] == 5
    assert stats["files_managed"] == 1234
    assert stats["jobs_active"] == 2
    assert stats["jobs_queued"] == 7
    assert stats["library_size_bytes"] > 50_000_000_000  # 50 GB in bytes


def test_parse_stats_html_label_fallback():
    """Label-based matching as fallback when no data-field attributes."""
    html = (
        '<div class="stat-card">'
        '  <span class="stat-value">3</span>'
        '  <span class="stat-label">Downloads Today</span>'
        '</div>'
        '<div class="stat-card">'
        '  <span class="stat-value">42</span>'
        '  <span class="stat-label">Queued Jobs</span>'
        '</div>'
    )
    stats = _parse_stats_html(html)
    assert stats["downloads_today"] == 3
    assert stats["jobs_queued"] == 42


def test_parse_stats_html_empty():
    assert _parse_stats_html("") == {}
    assert _parse_stats_html("<div>nothing</div>") == {}


def test_parse_stats_html_human_readable_size():
    """Library size in human-readable format is converted to bytes."""
    html = (
        '<div class="stat-card" data-field="library_size_bytes">'
        '  <span class="stat-value">4.2 GB</span>'
        '</div>'
    )
    stats = _parse_stats_html(html)
    assert stats["library_size_bytes"] > 4_000_000_000


# ── fetch_stats ──────────────────────────────────────────────────────────

def test_fetch_stats_spotifryer_json(monkeypatch):
    """spotifryer returns JSON stats."""
    monkeypatch.setattr(
        _FLEET_SESSION, "get", lambda url, timeout=None: _mock_json_response({
            "downloads_today": 5,
            "files_managed": 1200,
            "jobs_active": 2,
            "jobs_queued": 3,
            "library_size_bytes": 50_000_000_000,
        }),
    )
    result = fetch_stats("spotifryer")
    assert result["error"] is None
    assert result["service"] == "spotifryer"
    assert result["downloads_today"] == 5
    assert result["files_managed"] == 1200
    assert result["jobs_active"] == 2
    assert result["jobs_queued"] == 3
    assert result["library_size_bytes"] == 50_000_000_000


def test_fetch_stats_html_service(monkeypatch):
    """qoochie/tidalwave/deeznutz return HTML stats partials."""
    html = (
        '<div class="stats-grid">'
        '<div class="stat-card" data-field="downloads_today">'
        '  <span class="stat-value">10</span>'
        '  <span class="stat-label">Downloads Today</span>'
        '</div>'
        '<div class="stat-card" data-field="files_managed">'
        '  <span class="stat-value">500</span>'
        '  <span class="stat-label">Files Managed</span>'
        '</div>'
        '</div>'
    )
    monkeypatch.setattr(
        _FLEET_SESSION, "get", lambda url, timeout=None: _mock_html_response(html),
    )
    result = fetch_stats("qoochie")
    assert result["error"] is None
    assert result["downloads_today"] == 10
    assert result["files_managed"] == 500


def test_fetch_stats_unreachable(monkeypatch):
    def fake_get(url, timeout=None):
        raise _requests.ConnectionError("refused")

    monkeypatch.setattr(_FLEET_SESSION, "get", fake_get)
    result = fetch_stats("tidalwave")
    assert result["downloads_today"] == 0
    assert result["error"] is not None


def test_fetch_stats_unknown_service():
    result = fetch_stats("bogus")
    assert result["downloads_today"] == 0
    assert "unknown service" in result["error"]


def test_fetch_stats_401(monkeypatch):
    monkeypatch.setattr(
        _FLEET_SESSION, "get", lambda url, timeout=None: _mock_json_response({}, status_code=401),
    )
    result = fetch_stats("spotifryer")
    assert result["error"] == "auth_expired"


def test_fetch_stats_parse_error(monkeypatch):
    """JSON parse failure → graceful error."""
    resp = MagicMock()
    resp.status_code = 200
    resp.json.side_effect = ValueError("bad json")
    resp.raise_for_status = MagicMock()
    monkeypatch.setattr(_FLEET_SESSION, "get", lambda url, timeout=None: resp)
    result = fetch_stats("spotifryer")
    assert result["error"] == "parse_error"
    assert result["downloads_today"] == 0


# ── fetch_all_stats ──────────────────────────────────────────────────────

def test_fetch_all_stats_parallel(monkeypatch):
    def fake_fetch(service):
        return {
            "service": service,
            "downloads_today": 1,
            "files_managed": 100,
            "jobs_active": 0,
            "jobs_queued": 0,
            "library_size_bytes": 0,
            "error": None,
        }

    monkeypatch.setattr("app.search_aggregator.fleet.fetch_stats", fake_fetch)
    result = fetch_all_stats()
    assert "services" in result
    assert set(result["services"].keys()) == {"spotifryer", "qoochie", "tidalwave", "deeznutz"}
    for svc in ("spotifryer", "qoochie", "tidalwave", "deeznutz"):
        assert result["services"][svc]["error"] is None
        assert result["services"][svc]["downloads_today"] == 1


def test_fetch_all_stats_one_unreachable(monkeypatch):
    def fake_fetch(service):
        if service == "deeznutz":
            return {
                "service": service,
                "downloads_today": 0,
                "files_managed": 0,
                "jobs_active": 0,
                "jobs_queued": 0,
                "library_size_bytes": 0,
                "error": "unreachable",
            }
        return {
            "service": service,
            "downloads_today": 5,
            "files_managed": 100,
            "jobs_active": 0,
            "jobs_queued": 0,
            "library_size_bytes": 0,
            "error": None,
        }

    monkeypatch.setattr("app.search_aggregator.fleet.fetch_stats", fake_fetch)
    result = fetch_all_stats()
    assert result["services"]["deeznutz"]["error"] == "unreachable"
    assert result["services"]["spotifryer"]["error"] is None
    assert result["services"]["spotifryer"]["downloads_today"] == 5


# ══════════════════════════════════════════════════════════════════════════
# Phase 3 — Audit proxy
# ══════════════════════════════════════════════════════════════════════════

# ── _parse_audit_html ────────────────────────────────────────────────────

def test_parse_audit_html_div_layout():
    html = (
        '<div class="audit-list">'
        '<div class="audit-entry" data-job-id="abc">'
        '  <span class="audit-time">2026-01-15 10:30</span>'
        '  <span class="audit-action">download</span>'
        '  <span class="audit-detail">Cool Track by Artist</span>'
        '</div>'
        '<div class="audit-entry" data-job-id="def">'
        '  <span class="audit-time">2026-01-15 11:00</span>'
        '  <span class="audit-action">delete</span>'
        '  <span class="audit-detail">Old Album</span>'
        '</div>'
        '</div>'
    )
    entries = _parse_audit_html(html)
    assert len(entries) == 2
    assert entries[0]["job_id"] == "abc"
    assert entries[0]["time"] == "2026-01-15 10:30"
    assert entries[0]["action"] == "download"
    assert entries[0]["detail"] == "Cool Track by Artist"
    assert entries[1]["job_id"] == "def"
    assert entries[1]["action"] == "delete"


def test_parse_audit_html_table_layout():
    html = (
        '<table class="audit-table">'
        '<tr class="audit-row" data-job-id="x1">'
        '  <td class="audit-time">2026-02-01 09:00</td>'
        '  <td class="audit-action">retry</td>'
        '  <td class="audit-detail">Failed Track</td>'
        '</tr>'
        '</table>'
    )
    entries = _parse_audit_html(html)
    assert len(entries) == 1
    assert entries[0]["job_id"] == "x1"
    assert entries[0]["action"] == "retry"


def test_parse_audit_html_empty():
    assert _parse_audit_html("") == []
    assert _parse_audit_html("<div>nothing</div>") == []


def test_parse_audit_html_no_job_id():
    """Entries without a data-job-id get job_id=None."""
    html = (
        '<div class="audit-entry">'
        '  <span class="audit-time">2026-03-01</span>'
        '  <span class="audit-action">refresh</span>'
        '  <span class="audit-detail">Collection refreshed</span>'
        '</div>'
    )
    entries = _parse_audit_html(html)
    assert len(entries) == 1
    assert entries[0]["job_id"] is None
    assert entries[0]["action"] == "refresh"


# ── fetch_audit ──────────────────────────────────────────────────────────

def test_fetch_audit_spotifryer(monkeypatch):
    html = (
        '<div class="audit-entry" data-job-id="j1">'
        '  <span class="audit-time">2026-01-15</span>'
        '  <span class="audit-action">download</span>'
        '  <span class="audit-detail">Track</span>'
        '</div>'
    )
    monkeypatch.setattr(
        _FLEET_SESSION, "get", lambda url, timeout=None: _mock_html_response(html),
    )
    result = fetch_audit("spotifryer")
    assert result["error"] is None
    assert len(result["entries"]) == 1
    assert result["entries"][0]["service"] == "spotifryer"
    assert result["entries"][0]["job_id"] == "j1"


def test_fetch_audit_offset_param(monkeypatch):
    """Offset is passed as query param."""
    captured_urls = []
    html = '<div class="audit-entry"><span class="audit-time">T</span><span class="audit-action">A</span><span class="audit-detail">D</span></div>'

    def fake_get(url, timeout=None):
        captured_urls.append(url)
        return _mock_html_response(html)

    monkeypatch.setattr(_FLEET_SESSION, "get", fake_get)
    result = fetch_audit("qoochie", offset=100)
    assert result["error"] is None
    assert "offset=100" in captured_urls[0]


def test_fetch_audit_unreachable(monkeypatch):
    def fake_get(url, timeout=None):
        raise _requests.ConnectionError("refused")

    monkeypatch.setattr(_FLEET_SESSION, "get", fake_get)
    result = fetch_audit("tidalwave")
    assert result["entries"] == []
    assert result["error"] is not None


def test_fetch_audit_unknown_service():
    result = fetch_audit("bogus")
    assert result["entries"] == []
    assert "unknown service" in result["error"]


def test_fetch_audit_401(monkeypatch):
    monkeypatch.setattr(
        _FLEET_SESSION, "get", lambda url, timeout=None: _mock_json_response({}, status_code=401),
    )
    result = fetch_audit("deeznutz")
    assert result["entries"] == []
    assert result["error"] == "auth_expired"


# ── fetch_all_audit ──────────────────────────────────────────────────────

def test_fetch_all_audit_parallel(monkeypatch):
    def fake_fetch(service, offset=0):
        return {
            "entries": [{"time": "2026-01-01", "action": "download", "detail": f"{service} track", "job_id": None, "service": service}],
            "error": None,
        }

    monkeypatch.setattr("app.search_aggregator.fleet.fetch_audit", fake_fetch)
    result = fetch_all_audit()
    assert result["error"] is None
    assert len(result["entries"]) == 4
    services_seen = {e["service"] for e in result["entries"]}
    assert services_seen == {"spotifryer", "qoochie", "tidalwave", "deeznutz"}


def test_fetch_all_audit_sorted_by_time(monkeypatch):
    """Entries are sorted newest-first."""
    def fake_fetch(service, offset=0):
        if service == "spotifryer":
            return {"entries": [{"time": "2026-01-01", "action": "a", "detail": "", "job_id": None, "service": service}], "error": None}
        elif service == "qoochie":
            return {"entries": [{"time": "2026-06-01", "action": "a", "detail": "", "job_id": None, "service": service}], "error": None}
        return {"entries": [], "error": None}

    monkeypatch.setattr("app.search_aggregator.fleet.fetch_audit", fake_fetch)
    result = fetch_all_audit()
    assert result["entries"][0]["time"] == "2026-06-01"
    assert result["entries"][1]["time"] == "2026-01-01"


def test_fetch_all_audit_one_error(monkeypatch):
    def fake_fetch(service, offset=0):
        if service == "deeznutz":
            return {"entries": [], "error": "unreachable"}
        return {"entries": [{"time": "T", "action": "A", "detail": "D", "job_id": None, "service": service}], "error": None}

    monkeypatch.setattr("app.search_aggregator.fleet.fetch_audit", fake_fetch)
    result = fetch_all_audit()
    assert result["error"] == "unreachable"
    assert len(result["entries"]) == 3


# ══════════════════════════════════════════════════════════════════════════
# Phase 3 — Lyrics proxy (spotifryer only)
# ══════════════════════════════════════════════════════════════════════════

def test_fetch_lyrics_spotifryer_ok(monkeypatch):
    monkeypatch.setattr(
        _FLEET_SESSION, "get", lambda url, timeout=None: _mock_json_response({
            "lyrics": "La la la\nSecond line",
        }),
    )
    result = fetch_lyrics("spotifryer", "track_abc123")
    assert result["error"] is None
    assert "La la la" in result["lyrics"]


def test_fetch_lyrics_spotifryer_text_field(monkeypatch):
    """Some endpoints use 'text' instead of 'lyrics'."""
    monkeypatch.setattr(
        _FLEET_SESSION, "get", lambda url, timeout=None: _mock_json_response({
            "text": "Lyrics via text field",
        }),
    )
    result = fetch_lyrics("spotifryer", "track_xyz")
    assert result["error"] is None
    assert result["lyrics"] == "Lyrics via text field"


def test_fetch_lyrics_spotifryer_404(monkeypatch):
    monkeypatch.setattr(
        _FLEET_SESSION, "get", lambda url, timeout=None: _mock_json_response({}, status_code=404),
    )
    result = fetch_lyrics("spotifryer", "nonexistent")
    assert result["lyrics"] == ""
    assert result["error"] == "lyrics not found"


def test_fetch_lyrics_spotifryer_plain_text(monkeypatch):
    """Non-JSON response is used as plain text."""
    resp = MagicMock()
    resp.status_code = 200
    resp.json.side_effect = ValueError("not json")
    resp.text = "Plain lyrics text"
    monkeypatch.setattr(_FLEET_SESSION, "get", lambda url, timeout=None: resp)
    result = fetch_lyrics("spotifryer", "track_id")
    assert result["lyrics"] == "Plain lyrics text"
    assert result["error"] is None


def test_fetch_lyrics_non_spotifryer():
    result = fetch_lyrics("qoochie", "track_123")
    assert result["lyrics"] == ""
    assert "does not support" in result["error"]


def test_fetch_lyrics_unreachable(monkeypatch):
    def fake_get(url, timeout=None):
        raise _requests.ConnectionError("refused")

    monkeypatch.setattr(_FLEET_SESSION, "get", fake_get)
    result = fetch_lyrics("spotifryer", "track_id")
    assert result["lyrics"] == ""
    assert result["error"] is not None


def test_fetch_lyrics_unknown_service():
    result = fetch_lyrics("bogus", "track_id")
    assert result["lyrics"] == ""
    assert "unknown service" in result["error"]


# ══════════════════════════════════════════════════════════════════════════
# Phase 3 — Route tests
# ══════════════════════════════════════════════════════════════════════════

# ── Auth routes ──────────────────────────────────────────────────────────

def test_route_auth_status_all(client, monkeypatch):
    monkeypatch.setattr(
        "app.search_aggregator.routes.fetch_auth_status",
        lambda svc: {"service": svc, "status": "ok", "label": "", "last_updated": None, "error": None},
    )
    resp = client.get("/auth/status")
    assert resp.status_code == 200
    data = resp.get_json()
    assert "spotifryer" in data
    assert data["spotifryer"]["status"] == "ok"


def test_route_auth_status_one(client, monkeypatch):
    monkeypatch.setattr(
        "app.search_aggregator.routes.fetch_auth_status",
        lambda svc: {"service": svc, "status": "expired", "label": "", "last_updated": None, "error": None},
    )
    resp = client.get("/auth/status/qoochie")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["status"] == "expired"


def test_route_auth_status_invalid_service(client):
    resp = client.get("/auth/status/bogus")
    assert resp.status_code == 400
    assert "unknown service" in resp.get_json()["error"]


def test_route_auth_validate_ok(client, monkeypatch):
    monkeypatch.setattr(
        "app.search_aggregator.routes.validate_credential",
        lambda svc, token=None, arl=None: {"ok": True, "error": None},
    )
    resp = client.post("/auth/validate", data={"service": "qoochie", "token": "mytok"})
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True


def test_route_auth_validate_invalid_service(client):
    resp = client.post("/auth/validate", data={"service": "bogus"})
    assert resp.status_code == 400
    assert "unknown service" in resp.get_json()["error"]


def test_route_auth_validate_via_query_params(client, monkeypatch):
    """Token can come from query params too."""
    monkeypatch.setattr(
        "app.search_aggregator.routes.validate_credential",
        lambda svc, token=None, arl=None: {"ok": True, "error": None},
    )
    resp = client.post("/auth/validate?service=deeznutz&arl=abc123")
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True


# ── Stats routes ─────────────────────────────────────────────────────────

def test_route_stats_all(client, monkeypatch):
    fake = {
        "spotifryer": {"service": "spotifryer", "downloads_today": 1, "files_managed": 0, "jobs_active": 0, "jobs_queued": 0, "library_size_bytes": 0, "error": None},
        "qoochie": {"service": "qoochie", "downloads_today": 0, "files_managed": 0, "jobs_active": 0, "jobs_queued": 0, "library_size_bytes": 0, "error": None},
        "tidalwave": {"service": "tidalwave", "downloads_today": 0, "files_managed": 0, "jobs_active": 0, "jobs_queued": 0, "library_size_bytes": 0, "error": None},
        "deeznutz": {"service": "deeznutz", "downloads_today": 0, "files_managed": 0, "jobs_active": 0, "jobs_queued": 0, "library_size_bytes": 0, "error": None},
    }
    monkeypatch.setattr(
        "app.search_aggregator.routes.fetch_all_stats",
        lambda: {"services": fake},
    )
    resp = client.get("/stats/json")
    assert resp.status_code == 200
    data = resp.get_json()
    assert "services" in data
    assert data["services"]["spotifryer"]["downloads_today"] == 1


def test_route_stats_one(client, monkeypatch):
    svc_stats = {"service": "qoochie", "downloads_today": 3, "files_managed": 100, "jobs_active": 1, "jobs_queued": 2, "library_size_bytes": 5000, "error": None}
    monkeypatch.setattr(
        "app.search_aggregator.routes.fetch_all_stats",
        lambda: {"services": {"qoochie": svc_stats}},
    )
    resp = client.get("/stats/qoochie")
    assert resp.status_code == 200
    assert resp.get_json()["downloads_today"] == 3


def test_route_stats_invalid_service(client):
    resp = client.get("/stats/bogus")
    assert resp.status_code == 400
    assert "unknown service" in resp.get_json()["error"]


# ── Audit routes ─────────────────────────────────────────────────────────

def test_route_audit_all(client, monkeypatch):
    monkeypatch.setattr(
        "app.search_aggregator.routes.fetch_all_audit",
        lambda offset=0: {"entries": [{"time": "T", "action": "A", "detail": "D", "job_id": None, "service": "spotifryer"}], "error": None},
    )
    resp = client.get("/audit/json")
    assert resp.status_code == 200
    data = resp.get_json()
    assert len(data["entries"]) == 1


def test_route_audit_one(client, monkeypatch):
    monkeypatch.setattr(
        "app.search_aggregator.routes.fetch_audit",
        lambda svc, offset=0: {"entries": [{"time": "T", "action": "A", "detail": "D", "job_id": None, "service": svc}], "error": None},
    )
    resp = client.get("/audit/tidalwave")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["entries"][0]["service"] == "tidalwave"


def test_route_audit_with_offset(client, monkeypatch):
    captured = []
    def fake_fetch(svc, offset=0):
        captured.append(offset)
        return {"entries": [], "error": None}

    monkeypatch.setattr("app.search_aggregator.routes.fetch_audit", fake_fetch)
    resp = client.get("/audit/deeznutz?offset=50")
    assert resp.status_code == 200
    assert captured[0] == 50


def test_route_audit_invalid_service(client):
    resp = client.get("/audit/bogus")
    assert resp.status_code == 400
    assert "unknown service" in resp.get_json()["error"]


# ── Lyrics routes ────────────────────────────────────────────────────────

def test_route_lyrics_ok(client, monkeypatch):
    monkeypatch.setattr(
        "app.search_aggregator.routes.fetch_lyrics",
        lambda svc, tid: {"lyrics": "Song lyrics here", "error": None},
    )
    resp = client.get("/lyrics/spotifryer/track_123")
    assert resp.status_code == 200
    assert resp.get_json()["lyrics"] == "Song lyrics here"


def test_route_lyrics_invalid_service(client):
    resp = client.get("/lyrics/bogus/track_123")
    assert resp.status_code == 400
    assert "unknown service" in resp.get_json()["error"]
