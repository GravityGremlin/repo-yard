"""Tidal session management — OAuth device login, token persistence, auto-refresh."""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import tidalapi
from flask import g

from app.config import TIDAL_CONFIG_DIR, TIDAL_QUALITY
from app.proxy import apply_proxy_to_requests_session

logger = logging.getLogger(__name__)

_config_dir = TIDAL_CONFIG_DIR
_token_file: Path = _config_dir / "token.json"

# ── Pending logins (device code → session) ─────────────────────
_pending: dict[str, dict] = {}
_pending_lock = threading.Lock()

# Terminal outcomes from device-login background threads.
# Swept by _sweep_stale_pending. Holds (outcome, created_timestamp).
_terminal_outcomes: dict[str, tuple[str, float]] = {}

# Serializes token read→refresh→write across worker threads so Tidal's
# rotating refresh tokens are not clobbered by concurrent refreshes.
_token_lock = threading.Lock()


def _unbox(val: object) -> object:
    """tidal-dl-ng wraps values in {"data": val} — unwrap if needed."""
    if isinstance(val, dict) and "data" in val:
        return val["data"]
    return val


def _jwt_expiry(token: str) -> float | None:
    """Extract Unix expiry from a JWT payload."""
    try:
        payload = token.split(".")[1]
        pad = 4 - len(payload) % 4
        if pad != 4:
            payload += "=" * pad
        data = json.loads(__import__("base64").urlsafe_b64decode(payload))
        return data.get("exp")
    except Exception:
        return None


def _write_token_file(session: tidalapi.Session, old_refresh: str | None = None) -> None:
    """Persist tokens to token.json."""
    data = {
        "access_token": session.access_token,
        "refresh_token": session.refresh_token or old_refresh,
        "token_type": session.token_type,
    }
    if session.expiry_time is not None:
        data["expiry_time"] = session.expiry_time.replace(tzinfo=timezone.utc).timestamp()
    _token_file.parent.mkdir(parents=True, exist_ok=True)
    _token_file.write_text(json.dumps(data, indent=2))


def token_exists() -> bool:
    """Check if a token file exists."""
    return _token_file.exists()


def get_token_expiry_info() -> dict:
    """Read token expiry without creating a session."""
    if not _token_file.exists():
        return {"expires_at": None, "expires_in": None, "valid": False}
    try:
        raw = json.loads(_token_file.read_text())
        token_data = {k: _unbox(v) for k, v in raw.items()}
        expiry_ts = token_data.get("expiry_time") or _jwt_expiry(token_data.get("access_token", ""))
        if expiry_ts is None:
            return {"expires_at": None, "expires_in": None, "valid": False}
        now = time.time()
        remaining = float(expiry_ts) - now
        valid = remaining > 0
        return {"expires_at": expiry_ts, "expires_in": remaining if valid else 0, "valid": valid}
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Failed to read token file: %s", exc)
        return {"expires_at": None, "expires_in": None, "valid": False}
    except Exception:
        logger.debug("Unexpected error reading token expiry", exc_info=True)
        return {"expires_at": None, "expires_in": None, "valid": False}


def _create_session(proxy_url: str | None = None) -> tidalapi.Session | None:
    """Create a tidalapi Session from saved token, with auto-refresh."""
    if not _token_file.exists():
        logger.info("No token.json — not connected to Tidal")
        return None
    try:
        raw = json.loads(_token_file.read_text())
        token_data = {k: _unbox(v) for k, v in raw.items()}

        expiry_ts = token_data.get("expiry_time") or _jwt_expiry(token_data.get("access_token", ""))
        if expiry_ts is None:
            logger.warning("Token file has no expiry")
            return None
        expiry = datetime.fromtimestamp(float(expiry_ts), tz=timezone.utc)
        refresh = token_data.get("refresh_token")

        session = tidalapi.Session()
        session.config.quality = TIDAL_QUALITY
        if proxy_url:
            apply_proxy_to_requests_session(session.request_session, proxy_url)
        session.load_oauth_session(
            token_type=token_data.get("token_type", "Bearer"),
            access_token=token_data.get("access_token", ""),
            refresh_token=refresh,
            expiry_time=expiry,
        )
        # Auto-refresh if expired or about to expire.
        # Serialize under _token_lock so concurrent callers don't race
        # on Tidal's rotating refresh tokens.
        if refresh:
            with _token_lock:
                now = datetime.now(timezone.utc)
                # Another thread may have refreshed while we waited, rotating
                # the refresh token. Re-read the file and, if it changed, adopt
                # the fresh session data instead of refreshing a stale token.
                try:
                    fresh_raw = json.loads(_token_file.read_text())
                    fresh_data = {k: _unbox(v) for k, v in fresh_raw.items()}
                    fresh_refresh = fresh_data.get("refresh_token")
                    if fresh_refresh != refresh:
                        fresh_expiry_ts = fresh_data.get("expiry_time") or _jwt_expiry(
                            fresh_data.get("access_token", "")
                        )
                        if fresh_expiry_ts is not None:
                            refresh = fresh_refresh
                            expiry = datetime.fromtimestamp(float(fresh_expiry_ts), tz=timezone.utc)
                            session.load_oauth_session(
                                token_type=fresh_data.get("token_type", "Bearer"),
                                access_token=fresh_data.get("access_token", ""),
                                refresh_token=refresh,
                                expiry_time=expiry,
                            )
                except (json.JSONDecodeError, OSError):
                    pass  # fall through and refresh with the values we already have
                if now >= expiry or (expiry - now).total_seconds() < 300:
                    if session.token_refresh(refresh):
                        _write_token_file(session, refresh)
                        logger.info("Tidal token refreshed")
        if not session.check_login():
            logger.warning("Tidal session check_login() returned False")
            return None
        logger.debug("Tidal session created successfully")
        return session
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Tidal token file is malformed or unreadable: %s", exc)
        return None
    except Exception:
        logger.warning("Unexpected error loading Tidal session", exc_info=True)
        return None


def get_session() -> tidalapi.Session | None:
    """Per-request cached Tidal session."""
    if not hasattr(g, "_tidal_session"):
        g._tidal_session = _create_session()
    return g._tidal_session


def init_session(proxy_url: str | None = None) -> tidalapi.Session | None:
    """Non-request context session init (for background threads)."""
    return _create_session(proxy_url=proxy_url)


# ── Device login flow ─────────────────────────────────────────


def _ensure_scheme(url: str) -> str:
    """Tidal returns schemeless URLs (e.g. 'link.tidal.com/CODE')."""
    if url and not url.startswith(("http://", "https://")):
        return "https://" + url
    return url


def _sweep_stale_pending() -> None:
    """Drop device-login entries older than their own expiry window."""
    now = time.time()
    with _pending_lock:
        stale = [
            code for code, entry in _pending.items()
            if now - entry["created"] > max(entry["link"].expires_in, 300)
        ]
        for code in stale:
            logger.debug("Dropping stale pending login %s", code)
            _pending.pop(code, None)
        stale_outcomes = [
            code for code, (_, created) in _terminal_outcomes.items()
            if now - created > 300
        ]
        for code in stale_outcomes:
            _terminal_outcomes.pop(code, None)


def start_device_login() -> dict:
    """Start a Tidal OAuth device login flow.

    Returns:
        {"device_code": str, "url": str, "expires_in": int}
    """
    _sweep_stale_pending()
    session = tidalapi.Session()
    session.config.quality = TIDAL_QUALITY
    link, future = session.login_oauth()
    device_code = link.user_code or link.device_code

    with _pending_lock:
        _pending[device_code] = {
            "session": session,
            "link": link,
            "future": future,
            "created": time.time(),
        }

    # Start a background thread to save the token when login completes
    def _on_login_complete():
        ok = False
        try:
            future.result(timeout=link.expires_in)
            _write_token_file(session)
            ok = token_exists()
            if ok:
                logger.info("Tidal device login completed for code=%s", device_code)
            else:
                logger.warning("Token file missing after write for code=%s", device_code)
        except Exception as exc:
            logger.warning("Device login failed for code=%s: %s", device_code, exc)
        finally:
            outcome = "completed" if ok else "failed"
            with _pending_lock:
                entry = _pending.pop(device_code, None)
                if entry is not None:
                    _terminal_outcomes[device_code] = (outcome, time.time())

    t = threading.Thread(target=_on_login_complete, daemon=True)
    t.start()

    return {
        "device_code": device_code,
        "url": _ensure_scheme(link.verification_uri_complete),
        "expires_in": link.expires_in,
    }


def check_login_status(device_code: str) -> dict:
    """Check if a pending device login has completed."""
    with _pending_lock:
        entry = _pending.get(device_code)
        if entry is not None:
            return {"status": "pending", "expires_in": entry["link"].expires_in}

        outcome = _terminal_outcomes.get(device_code)
        if outcome is not None:
            return {"status": outcome[0]}

        if _token_file.exists():
            return {"status": "completed"}
        return {"status": "failed"}
