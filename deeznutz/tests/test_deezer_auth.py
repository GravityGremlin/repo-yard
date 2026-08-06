"""Tests for Deezer auth helpers in app.deezer.session."""

from __future__ import annotations

import json

import pytest


# ---------------------------------------------------------------------------
# token_exists()
# ---------------------------------------------------------------------------

def test_token_exists_true(tmp_path, monkeypatch):
    """token_exists() returns True when a valid token file is on disk."""
    token_file = tmp_path / "deezer_token.json"
    token_file.write_text(json.dumps({"arl": "a" * 20}))
    monkeypatch.setattr("app.deezer.session.TOKEN_FILE", token_file)

    from app.deezer.session import token_exists
    assert token_exists() is True


def test_token_exists_false(tmp_path, monkeypatch):
    """token_exists() returns False when no token file exists."""
    token_file = tmp_path / "deezer_token.json"  # does not exist
    monkeypatch.setattr("app.deezer.session.TOKEN_FILE", token_file)

    from app.deezer.session import token_exists
    assert token_exists() is False


# ---------------------------------------------------------------------------
# get_token_expiry_info()
# ---------------------------------------------------------------------------

def test_token_expiry_valid(tmp_path, monkeypatch):
    """Valid ARL (length > 20) returns valid=True."""
    token_file = tmp_path / "deezer_token.json"
    token_file.write_text(json.dumps({
        "arl": "x" * 40,
        "user_id": 12345,
    }))
    monkeypatch.setattr("app.deezer.session.TOKEN_FILE", token_file)

    from app.deezer.session import get_token_expiry_info
    info = get_token_expiry_info()
    assert info["valid"] is True
    assert info["user"] == 12345


def test_token_expiry_invalid(tmp_path, monkeypatch):
    """Short ARL (<= 20 chars) returns valid=False."""
    token_file = tmp_path / "deezer_token.json"
    token_file.write_text(json.dumps({"arl": "short"}))
    monkeypatch.setattr("app.deezer.session.TOKEN_FILE", token_file)

    from app.deezer.session import get_token_expiry_info
    info = get_token_expiry_info()
    assert info["valid"] is False
    assert info["arl_present"] is True


# ---------------------------------------------------------------------------
# bootstrap_env_arl()
# ---------------------------------------------------------------------------

def test_bootstrap_env_no_env(tmp_path, monkeypatch):
    """bootstrap_env_arl() returns skipped when DEEZER_ARL is unset."""
    monkeypatch.setattr("app.deezer.session.TOKEN_FILE", tmp_path / "token.json")
    monkeypatch.setattr("app.deezer.session.DEEZER_ARL", "")
    monkeypatch.delenv("DEEZER_ARL", raising=False)

    from app.deezer.session import bootstrap_env_arl
    result = bootstrap_env_arl()
    assert result["status"] == "skipped"


def test_bootstrap_env_invalid(tmp_path, monkeypatch):
    """bootstrap_env_arl() with a bad ARL delegates to login_via_arl, which errors."""
    token_file = tmp_path / "token.json"
    monkeypatch.setattr("app.deezer.session.TOKEN_FILE", token_file)
    monkeypatch.setattr("app.deezer.session.DEEZER_ARL", "x" * 30)

    # Mock login_via_arl to avoid network calls
    monkeypatch.setattr(
        "app.deezer.session.login_via_arl",
        lambda arl: {"status": "error", "message": "mock"},
    )

    from app.deezer.session import bootstrap_env_arl
    result = bootstrap_env_arl()
    assert result["status"] == "error"
    assert result["message"] == "mock"
