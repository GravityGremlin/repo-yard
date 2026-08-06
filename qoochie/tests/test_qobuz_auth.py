"""Tests for Qobuz auth helpers in app.qobuz.session."""

from __future__ import annotations

import json

import pytest


# ---------------------------------------------------------------------------
# token_exists()
# ---------------------------------------------------------------------------

def test_token_exists_true(tmp_path, monkeypatch):
    """token_exists() returns True when a valid token file is on disk."""
    token_file = tmp_path / "qobuz_token.json"
    token_file.write_text(json.dumps({"token": "x" * 40}))
    monkeypatch.setattr("app.qobuz.session.TOKEN_FILE", token_file)

    from app.qobuz.session import token_exists
    assert token_exists() is True


def test_token_exists_false(tmp_path, monkeypatch):
    """token_exists() returns False when no token file exists."""
    token_file = tmp_path / "qobuz_token.json"  # does not exist
    monkeypatch.setattr("app.qobuz.session.TOKEN_FILE", token_file)

    from app.qobuz.session import token_exists
    assert token_exists() is False


# ---------------------------------------------------------------------------
# get_token_expiry_info()
# ---------------------------------------------------------------------------

def test_token_expiry_valid(tmp_path, monkeypatch):
    """Valid token (length > 20) returns valid=True."""
    token_file = tmp_path / "qobuz_token.json"
    token_file.write_text(json.dumps({
        "token": "x" * 40,
        "user_id": 12345,
    }))
    monkeypatch.setattr("app.qobuz.session.TOKEN_FILE", token_file)

    from app.qobuz.session import get_token_expiry_info
    info = get_token_expiry_info()
    assert info["valid"] is True
    assert info["user_id"] == 12345


def test_token_expiry_missing_file(tmp_path, monkeypatch):
    """Missing token file returns valid=False with error."""
    token_file = tmp_path / "qobuz_token.json"  # does not exist
    monkeypatch.setattr("app.qobuz.session.TOKEN_FILE", token_file)

    from app.qobuz.session import get_token_expiry_info
    info = get_token_expiry_info()
    assert info["valid"] is False
    assert "error" in info


def test_token_expiry_empty_token(tmp_path, monkeypatch):
    """Token file with empty token returns valid=False."""
    token_file = tmp_path / "qobuz_token.json"
    token_file.write_text(json.dumps({"token": ""}))
    monkeypatch.setattr("app.qobuz.session.TOKEN_FILE", token_file)

    from app.qobuz.session import get_token_expiry_info
    info = get_token_expiry_info()
    assert info["valid"] is False


def test_token_expiry_short_token(tmp_path, monkeypatch):
    """Token shorter than 20 chars returns valid=False."""
    token_file = tmp_path / "qobuz_token.json"
    token_file.write_text(json.dumps({"token": "short"}))
    monkeypatch.setattr("app.qobuz.session.TOKEN_FILE", token_file)

    from app.qobuz.session import get_token_expiry_info
    info = get_token_expiry_info()
    assert info["valid"] is False


def test_token_expiry_corrupt_file(tmp_path, monkeypatch):
    """Corrupt JSON in token file returns valid=False with error."""
    token_file = tmp_path / "qobuz_token.json"
    token_file.write_text("not json {{{")
    monkeypatch.setattr("app.qobuz.session.TOKEN_FILE", token_file)

    from app.qobuz.session import get_token_expiry_info
    info = get_token_expiry_info()
    assert info["valid"] is False
    assert "error" in info


# ---------------------------------------------------------------------------
# _refresh_token recursion guard
# ---------------------------------------------------------------------------

def test_refresh_token_recursion_guard(tmp_path, monkeypatch):
    """A 401 on user/login during refresh does not recurse infinitely."""
    monkeypatch.setattr("app.qobuz.session.TOKEN_FILE", tmp_path / "tok.json")
    monkeypatch.setattr("app.qobuz.session._write_token", lambda d: None)
    monkeypatch.setattr("app.qobuz.session._read_token", lambda: {"token": "old"})

    from unittest.mock import patch, MagicMock, call
    from app.qobuz.session import QobuzClient
    import requests as _requests

    client = QobuzClient(token="stale_token", app_id=1, app_secret="s")

    # Every HTTP call returns 401 (including the user/login inside _refresh_token)
    resp_401 = MagicMock(status_code=401)
    resp_401.raise_for_status.side_effect = _requests.HTTPError(response=resp_401)

    with patch.object(client._http, "get", return_value=resp_401) as mock_get, \
         patch.object(client._http, "post", return_value=resp_401) as mock_post:
        # Directly call _refresh_token — it should NOT recurse infinitely
        result = client._refresh_token()

    assert result is False
    # The initial _post("user/login") call happens once, then _post sees 401 and
    # calls _refresh_token() again, but the guard blocks it.  So _http.post is
    # called exactly once — not infinitely.
    assert mock_post.call_count == 1, (
        f"_http.post should be called exactly once, was called {mock_post.call_count} times"
    )


def test_refresh_token_normal_success(tmp_path, monkeypatch):
    """Normal refresh path: _post succeeds, token is updated."""
    monkeypatch.setattr("app.qobuz.session.TOKEN_FILE", tmp_path / "tok.json")
    monkeypatch.setattr("app.qobuz.session._write_token", lambda d: None)
    monkeypatch.setattr("app.qobuz.session._read_token", lambda: {"token": "old"})

    from unittest.mock import patch, MagicMock
    from app.qobuz.session import QobuzClient

    client = QobuzClient(token="old_token", app_id=1, app_secret="s")

    login_resp = MagicMock()
    login_resp.status_code = 200
    login_resp.json.return_value = {"user_auth_token": "new_fresh_token"}

    with patch.object(client, "_post", return_value={"user_auth_token": "new_fresh_token"}) as mock_post:
        result = client._refresh_token()

    assert result is True
    assert client.token == "new_fresh_token"


# ---------------------------------------------------------------------------
# Re-sign after refresh: _get and _post rebuild fresh ts+sig
# ---------------------------------------------------------------------------

def test_get_resigns_after_refresh(tmp_path, monkeypatch):
    """After a successful refresh, _get rebuilds request_ts/request_sig from base_params."""
    monkeypatch.setattr("app.qobuz.session.TOKEN_FILE", tmp_path / "tok.json")
    monkeypatch.setattr("app.qobuz.session._write_token", lambda d: None)
    monkeypatch.setattr("app.qobuz.session._read_token", lambda: {"token": "old"})

    from unittest.mock import patch, MagicMock, call
    from app.qobuz.session import QobuzClient
    import requests as _requests
    import time as _time

    client = QobuzClient(token="old_token", app_id=1, app_secret="secret")

    # First GET: 401. Second GET (after refresh): 200.
    resp_401 = MagicMock(status_code=401)
    resp_401.raise_for_status.side_effect = _requests.HTTPError(response=resp_401)
    resp_200 = MagicMock(status_code=200)
    resp_200.json.return_value = {"ok": True}

    # Mock time.time to return different values so timestamps differ
    time_counter = [1000000]
    def fake_time():
        val = time_counter[0]
        time_counter[0] += 1
        return val

    with patch.object(client._http, "get", side_effect=[resp_401, resp_200]) as mock_get, \
         patch.object(client, "_refresh_token", return_value=True), \
         patch("app.qobuz.session.time.time", side_effect=fake_time):
        result = client._get("track/get", params={"track_id": "42"}, signed=True)

    assert result == {"ok": True}
    # Both calls should have received a dict with request_ts and request_sig
    first_params = mock_get.call_args_list[0][1]["params"]
    second_params = mock_get.call_args_list[1][1]["params"]
    assert "request_ts" in first_params and "request_sig" in first_params
    assert "request_ts" in second_params and "request_sig" in second_params
    # Fresh signing should produce a different timestamp
    assert first_params["request_ts"] != second_params["request_ts"]


def test_post_resigns_after_refresh(tmp_path, monkeypatch):
    """After a successful refresh, _post rebuilds request_ts/request_sig from base data."""
    monkeypatch.setattr("app.qobuz.session.TOKEN_FILE", tmp_path / "tok.json")
    monkeypatch.setattr("app.qobuz.session._write_token", lambda d: None)
    monkeypatch.setattr("app.qobuz.session._read_token", lambda: {"token": "old"})

    from unittest.mock import patch, MagicMock
    from app.qobuz.session import QobuzClient
    import requests as _requests

    client = QobuzClient(token="old_token", app_id=1, app_secret="secret")

    resp_401 = MagicMock(status_code=401)
    resp_401.raise_for_status.side_effect = _requests.HTTPError(response=resp_401)
    resp_200 = MagicMock(status_code=200)
    resp_200.json.return_value = {"ok": True}

    time_counter = [1000000]
    def fake_time():
        val = time_counter[0]
        time_counter[0] += 1
        return val

    with patch.object(client._http, "post", side_effect=[resp_401, resp_200]) as mock_post, \
         patch.object(client, "_refresh_token", return_value=True), \
         patch("app.qobuz.session.time.time", side_effect=fake_time):
        result = client._post("some/endpoint", data={"key": "val"}, signed=True)

    assert result == {"ok": True}
    first_data = mock_post.call_args_list[0][1]["data"]
    second_data = mock_post.call_args_list[1][1]["data"]
    assert "request_ts" in first_data and "request_sig" in first_data
    assert "request_ts" in second_data and "request_sig" in second_data
    # Timestamp must be fresh (different call → different ts)
    assert first_data["request_ts"] != second_data["request_ts"]
