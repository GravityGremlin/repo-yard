"""Deezer authentication session — ARL cookie login (no OAuth)."""

from __future__ import annotations

import json
import logging
import threading
from typing import Any

from flask import g

from app.config import DEEZER_ARL, DEEZER_CONFIG_DIR

_log = logging.getLogger(__name__)

TOKEN_FILE = DEEZER_CONFIG_DIR / "deezer_token.json"

# Serialises all token-file reads and writes so that concurrent workers
# never observe a half-written file or lose a write.
_token_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Token file helpers  (all public functions acquire _token_lock)
# ---------------------------------------------------------------------------

def token_exists() -> bool:
    """Check if a saved ARL token file exists on disk."""
    return TOKEN_FILE.exists() and TOKEN_FILE.stat().st_size > 0


def get_token_expiry_info() -> dict[str, Any]:
    """Return ARL status info (no expiry — reports valid/invalid by format)."""
    with _token_lock:
        if not token_exists():
            return {"valid": False, "error": "No token file"}
        try:
            raw = json.loads(TOKEN_FILE.read_text())
            arl = raw.get("arl", "")
            valid = bool(arl) and len(arl) > 20
            return {"valid": valid, "arl_present": bool(arl), "user": raw.get("user_id", "")}
        except (json.JSONDecodeError, OSError) as exc:
            return {"valid": False, "error": str(exc)}


def _read_token() -> dict[str, Any]:
    """Read ARL + metadata from the persisted token file."""
    if not TOKEN_FILE.exists():
        return {}
    return json.loads(TOKEN_FILE.read_text())


def _write_token(data: dict[str, Any]) -> None:
    """Persist ARL + metadata to the token file.

    Caller must hold ``_token_lock``.
    """
    DEEZER_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(json.dumps(data, indent=2))
    TOKEN_FILE.chmod(0o600)


# ---------------------------------------------------------------------------
# Session lifecycle
# ---------------------------------------------------------------------------

def _validate_arl(arl: str) -> tuple[bool, dict[str, Any]]:
    """Validate an ARL against Deezer via deezer-py.

    Returns ``(ok, user_info)``. ``user_info`` is ``{"user_id", "name"}``
    on success, empty dict on failure.
    """
    import deezer  # deezer-py

    dz = deezer.Deezer()
    if not dz.login_via_arl(arl.strip()):
        return False, {}
    user = getattr(dz, "current_user", {}) or {}
    return True, {"user_id": user.get("id"), "name": user.get("name")}


def login_via_arl(arl: str) -> dict[str, Any]:
    """Authenticate to Deezer using an ARL cookie string.

    Delegates to ``deezer.Deezer().login_via_arl(arl)`` for validation,
    then persists the ARL and basic user info locally.

    Returns ``{"status": "ok", "user": ...}`` or
    ``{"status": "error", "message": "..."}``.
    """
    arl = (arl or "").strip()
    if len(arl) < 20:
        return {"status": "error", "message": "ARL cookie is too short"}

    try:
        ok, user_info = _validate_arl(arl)
    except Exception as exc:  # network/API errors must not crash the request
        _log.error("Deezer login validation failed: %s", exc)
        return {"status": "error", "message": f"ARL validation error: {exc}"}

    if not ok:
        return {"status": "error", "message": "ARL rejected by Deezer"}

    with _token_lock:
        _write_token({"arl": arl, **user_info})
    _log.info("Deezer login OK — user %s", user_info.get("name"))
    return {"status": "ok", "user": user_info}


def bootstrap_env_arl() -> dict[str, Any]:
    """If ``DEEZER_ARL`` env var is set, attempt to auth from it on startup.

    This makes headless/container deployments configurable without the UI:
    set ``DEEZER_ARL`` and the app will persist a session if the ARL is valid.
    Failures are logged but never crash startup.

    Safe to call in test environments — no-ops when not configured.
    """
    if not DEEZER_ARL:
        return {"status": "skipped", "message": "DEEZER_ARL not set"}
    with _token_lock:
        if token_exists():
            _log.info("DEEZER_ARL provided but token file already exists — leaving as-is")
            return {"status": "skipped", "message": "token file already present"}
    result = login_via_arl(DEEZER_ARL)
    if result.get("status") == "ok":
        _log.info("DEEZER_ARL bootstrap successful")
    else:
        _log.warning("DEEZER_ARL bootstrap failed: %s", result.get("message"))
    return result


def get_session() -> object | None:
    """Return a cached Deezer API session (per-request, via Flask *g*).

    The returned object is a ``deezer.Deezer`` instance (``deezer-py``
    package). Returns ``None`` if no valid token is available.
    """
    if hasattr(g, "deezer_session"):
        return g.deezer_session
    try:
        with _token_lock:
            if not token_exists():
                return None
            raw = _read_token()
        arl = raw.get("arl", "")
        if not arl:
            return None
        import deezer
        dz = deezer.Deezer()
        if not dz.login_via_arl(arl):
            return None
        g.deezer_session = dz
        return dz
    except Exception as exc:
        _log.error("Failed to build Deezer session: %s", exc)
        return None


def init_session(proxy_url: str | None = None) -> object | None:
    """Create a fresh Deezer session for worker threads (no Flask *g*)."""
    try:
        with _token_lock:
            if not token_exists():
                return None
            raw = _read_token()
        arl = raw.get("arl", "")
        if not arl:
            return None
        import deezer
        dz = deezer.Deezer()
        if not dz.login_via_arl(arl):
            return None
        return dz
    except Exception as exc:
        _log.error("Failed to init Deezer session: %s", exc)
        return None
