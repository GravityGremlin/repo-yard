"""Qobuz authentication session — token-based login with signed API requests."""

from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import Any

import requests
from flask import g

from app.config import (
    QOBUZ_TOKEN, QOBUZ_USER_ID, QOBUZ_APP_ID, QOBUZ_APP_SECRET,
    QOBUZ_CONFIG_DIR, BROWSER_UA,
)

_log = logging.getLogger(__name__)

TOKEN_FILE = QOBUZ_CONFIG_DIR / "qobuz_token.json"
API_BASE = "https://www.qobuz.com/api.json/0.2"
USER_AGENT = BROWSER_UA


# ---------------------------------------------------------------------------
# Token file helpers
# ---------------------------------------------------------------------------

def token_exists() -> bool:
    """Check if a saved token file exists on disk."""
    return TOKEN_FILE.exists() and TOKEN_FILE.stat().st_size > 0


def get_token_expiry_info() -> dict[str, Any]:
    """Return token status info."""
    if not token_exists():
        return {"valid": False, "error": "No token file"}
    try:
        raw = json.loads(TOKEN_FILE.read_text())
        token = raw.get("token", "")
        valid = bool(token) and len(token) > 20
        return {"valid": valid, "token_present": bool(token),
                "user_id": raw.get("user_id", ""),
                "app_id": raw.get("app_id", "")}
    except (json.JSONDecodeError, OSError) as exc:
        return {"valid": False, "error": str(exc)}


def _read_token() -> dict[str, Any]:
    """Read token + metadata from the persisted token file."""
    if not token_exists():
        return {}
    try:
        return json.loads(TOKEN_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _write_token(data: dict[str, Any]) -> None:
    """Persist token + metadata to the token file."""
    QOBUZ_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(json.dumps(data, indent=2))
    TOKEN_FILE.chmod(0o600)


# ---------------------------------------------------------------------------
# Qobuz API signing
# ---------------------------------------------------------------------------

def _sign(endpoint: str, params: dict[str, Any], timestamp: str, secret: str) -> str:
    """Compute request_sig for a Qobuz API request.

    Spec (verified against qopy/music-assistant/qobuz-proxy):
    md5( path_without_slashes + sorted "keyvalue" pairs of params
         + request_ts + app_secret )
    The timestamp is appended ONCE at the end — NOT after each pair.
    Keys app_id/request_ts/request_sig are excluded from the signing string.
    """
    ep_clean = endpoint.replace("/", "")
    signing = ep_clean
    for key in sorted(params.keys()):
        if key in ("app_id", "request_ts", "request_sig"):
            continue
        signing += f"{key}{params[key]}"
    signing += timestamp + secret
    return hashlib.md5(signing.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Qobuz API client (per-request, stored in Flask g)
# ---------------------------------------------------------------------------

class QobuzClient:
    """Thin wrapper around Qobuz JSON API with auth headers and signing."""

    def __init__(self, token: str, user_id: int = 0,
                 app_id: int = 0, app_secret: str = ""):
        self.token = token
        self.user_id = user_id or QOBUZ_USER_ID
        self.app_id = app_id or QOBUZ_APP_ID
        self.app_secret = app_secret or QOBUZ_APP_SECRET
        self._http = requests.Session()
        self._http.headers.update({
            "X-App-Id": str(self.app_id),
            "X-User-Auth-Token": self.token,
            "User-Agent": USER_AGENT,
        })
        self._last_getfile_ts: float = 0.0
        self._refreshing: bool = False

    # -- low-level -----------------------------------------------------------

    def _signed_params(self, endpoint: str, params: dict[str, Any]) -> dict[str, str]:
        """Return params dict augmented with request_ts + request_sig."""
        ts = str(int(time.time()))
        sig = _sign(endpoint, params, ts, self.app_secret)
        p = dict(params)
        p["request_ts"] = ts
        p["request_sig"] = sig
        p["app_id"] = str(self.app_id)
        return p

    def _get(self, endpoint: str, params: dict[str, Any] | None = None,
             signed: bool = False) -> Any:
        """GET request. If *signed*, add ts+sig+app_id."""
        base_params = dict(params or {})
        if signed:
            req_params = self._signed_params(endpoint, base_params)
        else:
            req_params = base_params
        url = f"{API_BASE}/{endpoint}"
        resp = self._http.get(url, params=req_params, timeout=30)
        if resp.status_code in (401, 403):
            # Try token refresh once
            if self._refresh_token():
                if signed:
                    req_params = self._signed_params(endpoint, base_params)
                resp = self._http.get(url, params=req_params, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def _post(self, endpoint: str, data: dict[str, Any] | None = None,
              signed: bool = False) -> Any:
        """POST request. If *signed*, add ts+sig+app_id to form data."""
        base_data = dict(data or {})
        if signed:
            ts = str(int(time.time()))
            sig = _sign(endpoint, base_data, ts, self.app_secret)
            req_data = dict(base_data)
            req_data["request_ts"] = ts
            req_data["request_sig"] = sig
            req_data["app_id"] = str(self.app_id)
        else:
            req_data = base_data
        url = f"{API_BASE}/{endpoint}"
        resp = self._http.post(url, data=req_data, timeout=30)
        if resp.status_code in (401, 403):
            if self._refresh_token():
                if signed:
                    ts = str(int(time.time()))
                    sig = _sign(endpoint, base_data, ts, self.app_secret)
                    req_data = dict(base_data)
                    req_data["request_ts"] = ts
                    req_data["request_sig"] = sig
                    req_data["app_id"] = str(self.app_id)
                resp = self._http.post(url, data=req_data, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def _throttle_getfile(self) -> None:
        """Enforce ~1 req/sec for getFileUrl to avoid 429."""
        elapsed = time.time() - self._last_getfile_ts
        if elapsed < 1.0:
            time.sleep(1.0 - elapsed)
        self._last_getfile_ts = time.time()

    # -- token refresh -------------------------------------------------------

    def _refresh_token(self) -> bool:
        """Attempt to refresh the user_auth_token via user/login extra=partner."""
        if self._refreshing:
            _log.warning("Token refresh already in progress — skipping re-entrant call")
            return False
        self._refreshing = True
        try:
            resp_data = self._post("user/login", data={"extra": "partner"})
            new_token = resp_data.get("user_auth_token") or resp_data.get("token", "")
            if new_token and new_token != self.token:
                self.token = new_token
                self._http.headers["X-User-Auth-Token"] = new_token
                # Persist the refreshed token
                stored = _read_token()
                stored["token"] = new_token
                _write_token(stored)
                _log.info("Token refreshed successfully")
                return True
        except Exception as exc:
            _log.warning("Token refresh failed: %s", exc)
        finally:
            self._refreshing = False
        return False

    # -- validation ----------------------------------------------------------

    def validate(self) -> dict[str, Any]:
        """Validate the token with a lightweight API call."""
        try:
            data = self._get("user/login", params={"extra": "partner"})
            uid = data.get("user", {}).get("id") or data.get("user_id", 0)
            return {"status": "ok", "user_id": uid, "valid": True}
        except requests.HTTPError as exc:
            return {"status": "error", "message": f"Validation failed: {exc}"}
        except Exception as exc:
            return {"status": "error", "message": f"Validation error: {exc}"}

    # -- search --------------------------------------------------------------

    def search(self, query: str, limit: int = 25) -> dict:
        """Search all types. Returns {TRACK: {data: [...]}, ALBUM: {...},
        ARTIST: {...}, PLAYLIST: {...}} — the shape app/search/routes.py expects."""
        result: dict[str, dict] = {}
        type_endpoints = (
            ("TRACK", "track/search", "tracks"),
            ("ALBUM", "album/search", "albums"),
            ("ARTIST", "artist/search", "artists"),
            ("PLAYLIST", "playlist/search", "playlists"),
        )
        for key, endpoint, resp_key in type_endpoints:
            try:
                data = self._get(endpoint, params={"query": query, "limit": str(limit)})
                result[key] = {"data": (data.get(resp_key) or {}).get("items", [])}
            except requests.HTTPError as exc:
                _log.warning("Qobuz search type %s failed: %s", key, exc)
                result[key] = {"data": []}
        return result

    def search_music(self, query: str, type: str = "TRACK", limit: int = 25) -> dict:
        """Search a specific type (TRACK/ALBUM/ARTIST). Returns {data: [...]}."""
        endpoint = f"{type.lower()}/search"
        resp_key = f"{type.lower()}s"
        data = self._get(endpoint, params={"query": query, "limit": str(limit)})
        return {"data": (data.get(resp_key) or {}).get("items", [])}

    # -- album ---------------------------------------------------------------

    def get_album_tracks(self, album_id) -> list[dict]:
        """Return list of track dicts for an album.

        The Qobuz album/get response embeds ``tracks.items`` directly; the
        ``extra=tracks`` param now returns 400, so it is not sent.
        """
        data = self._get("album/get", params={"album_id": str(album_id)})
        tracks = data.get("tracks", {})
        return tracks.get("items", []) if isinstance(tracks, dict) else tracks if isinstance(tracks, list) else []

    # -- playlist ------------------------------------------------------------

    def get_playlist(self, playlist_id: str) -> dict:
        """Return playlist metadata + tracks."""
        return self._get("playlist/get", params={"playlist_id": playlist_id, "extra": "tracks", "limit": "500"})

    # -- artist / discography ------------------------------------------------

    def get_artist(self, artist_id: int) -> dict:
        """Return artist metadata."""
        return self._get("artist/get", params={"artist_id": str(artist_id)})

    def get_artist_discography(self, artist_id: int) -> list[dict]:
        """Return list of album dicts for an artist (paginated up to 500)."""
        data = self._get("artist/get", params={
            "artist_id": str(artist_id),
            "extra": "albums",
            "limit": "500",
            "offset": "0",
        })
        albums = data.get("albums", {})
        if isinstance(albums, dict):
            return albums.get("items", [])
        if isinstance(albums, list):
            return albums
        return []

    # -- track ----------------------------------------------------------------

    def get_track(self, track_id: int) -> dict:
        """Return track metadata."""
        return self._get("track/get", params={"track_id": str(track_id)})

    # -- stream URL -----------------------------------------------------------

    def get_file_url(self, track_id: int, format_id: int,
                     intent: str = "stream") -> dict:
        """Signed getFileUrl — returns dict with 'url' or 'key'."""
        self._throttle_getfile()
        return self._get("track/getFileUrl", params={
            "track_id": str(track_id),
            "format_id": str(format_id),
            "intent": intent,
        }, signed=True)


# ---------------------------------------------------------------------------
# Flask g-scoped session accessors
# ---------------------------------------------------------------------------

def get_session() -> QobuzClient | None:
    """Return the current QobuzClient from Flask g, or None."""
    return getattr(g, "_qobuz_client", None)


def _set_session(client: QobuzClient) -> None:
    """Store QobuzClient in Flask g."""
    g._qobuz_client = client


def init_session(token: str = "", user_id: int = 0, app_id: int = 0,
                 app_secret: str = "") -> QobuzClient:
    """Build a QobuzClient from provided (or stored) credentials.

    Does NOT hit the network — just creates the client object.
    """
    data = _read_token()
    use_token = token or data.get("token", QOBUZ_TOKEN)
    use_uid = user_id or data.get("user_id", QOBUZ_USER_ID)
    if not use_token:
        return None  # type: ignore[return-value]
    client = QobuzClient(
        token=use_token,
        user_id=use_uid,
        app_id=app_id or data.get("app_id", QOBUZ_APP_ID),
        app_secret=app_secret or data.get("app_secret", QOBUZ_APP_SECRET),
    )
    try:
        _set_session(client)
    except RuntimeError:
        # Outside request context — caller will handle
        pass
    return client


def login_via_token(token: str) -> dict[str, Any]:
    """Validate and persist a Qobuz token."""
    data = _read_token()
    client = QobuzClient(
        token=token,
        user_id=data.get("user_id", QOBUZ_USER_ID),
        app_id=data.get("app_id", QOBUZ_APP_ID),
        app_secret=data.get("app_secret", QOBUZ_APP_SECRET),
    )
    result = client.validate()
    if result.get("status") == "ok":
        _write_token({
            "token": token,
            "user_id": result.get("user_id", QOBUZ_USER_ID),
            "app_id": QOBUZ_APP_ID,
            "app_secret": QOBUZ_APP_SECRET,
        })
    return result


def bootstrap_env_token() -> dict[str, Any]:
    """Bootstrap from QOBUZ_TOKEN env var — persist if no file exists yet."""
    if QOBUZ_TOKEN and not token_exists():
        _write_token({
            "token": QOBUZ_TOKEN,
            "user_id": QOBUZ_USER_ID,
            "app_id": QOBUZ_APP_ID,
            "app_secret": QOBUZ_APP_SECRET,
        })
        _log.info("Bootstrapped Qobuz token from environment")
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Before-request hook for Flask (registers session from stored token)
# ---------------------------------------------------------------------------

def ensure_session():
    """Flask before_request hook: create QobuzClient from persisted token."""
    if get_session() is not None:
        return
    data = _read_token()
    token = data.get("token", QOBUZ_TOKEN)
    if not token:
        return
    client = QobuzClient(
        token=token,
        user_id=data.get("user_id", QOBUZ_USER_ID),
        app_id=data.get("app_id", QOBUZ_APP_ID),
        app_secret=data.get("app_secret", QOBUZ_APP_SECRET),
    )
    _set_session(client)
