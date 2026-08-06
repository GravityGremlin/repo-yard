"""Spotify OAuth PKCE session management — token persistence, auto-refresh.

Also hosts the client-side rate limiter: every Spotify API call (search,
metadata, pagination) funnels through one throttled requests session so bursts
of discography/search work can never trip Spotify's 429 Retry-After ban.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

import requests
import spotipy
from spotipy.oauth2 import SpotifyOAuth

from app.config import (
    SPOTIFY_CLIENT_ID,
    SPOTIFY_CLIENT_SECRET,
    SPOTIFY_REDIRECT_URI,
    SPOTIFY_CONFIG_DIR,
    SPOTIFY_MIN_REQUEST_INTERVAL,
)

logger = logging.getLogger(__name__)

SCOPES = "user-library-read playlist-read-private playlist-read-collaborative"

_token_file: Path = SPOTIFY_CONFIG_DIR / "spotify_token.json"


class _ThrottledSession(requests.Session):
    """requests session enforcing a global minimum interval between calls.

    Shared by every Spotify client in the process, so all worker threads and
    request handlers serialize their API traffic through one gate.
    """

    _lock = threading.Lock()
    _last_request = 0.0

    def request(self, method, url, **kwargs):  # noqa: A003
        with self._lock:
            now = time.monotonic()
            wait = self._last_request + SPOTIFY_MIN_REQUEST_INTERVAL - now
            if wait > 0:
                time.sleep(wait)
            self._last_request = time.monotonic()
        return super().request(method, url, **kwargs)


_throttled_session = _ThrottledSession()


class SpotifyAuthError(Exception):
    """Raised when Spotify authentication fails or is not configured."""


def _auth_manager() -> SpotifyOAuth | None:
    """Build a SpotifyOAuth auth manager with token persistence."""
    if not SPOTIFY_CLIENT_ID:
        return None
    return SpotifyOAuth(
        client_id=SPOTIFY_CLIENT_ID,
        client_secret=SPOTIFY_CLIENT_SECRET or None,
        redirect_uri=SPOTIFY_REDIRECT_URI,
        scope=SCOPES,
        cache_path=str(_token_file),
        open_browser=False,
    )


def is_authenticated() -> bool:
    """Check if a valid (non-expired) token exists."""
    am = _auth_manager()
    if am is None:
        return False
    try:
        token_info = am.get_cached_token()
        return token_info is not None and not am.is_token_expired(token_info)
    except Exception:
        logger.warning("Failed to check Spotify auth status", exc_info=True)
        return False


def get_auth_url() -> str:
    """Return the OAuth authorization URL the user must visit."""
    am = _auth_manager()
    if am is None:
        raise SpotifyAuthError("Spotify client_id is not configured")
    return am.get_authorize_url()


def exchange_code(code: str) -> dict:
    """Exchange an authorization code for access/refresh tokens.

    Returns the token dict on success. Raises SpotifyAuthError on failure.
    """
    am = _auth_manager()
    if am is None:
        raise SpotifyAuthError("Spotify client_id is not configured")
    try:
        token_info = am.get_access_token(code, as_dict=True, check_cache=False)
        if not token_info or "access_token" not in token_info:
            raise SpotifyAuthError("Token exchange returned no access token")
        return token_info
    except SpotifyAuthError:
        raise
    except Exception as exc:
        raise SpotifyAuthError(f"Token exchange failed: {exc}") from exc


def get_public_spotify_client() -> spotipy.Spotify:
    """Return a spotipy client using client-credentials (app-only) auth.

    Does NOT require user OAuth — only client_id + client_secret from config.
    Works for public data: search, public playlists, public tracks.
    Raises SpotifyAuthError if client_id is not configured.
    """
    if not SPOTIFY_CLIENT_ID:
        raise SpotifyAuthError("Spotify client_id is not configured")
    try:
        from spotipy.oauth2 import SpotifyClientCredentials
        ccm = SpotifyClientCredentials(
            client_id=SPOTIFY_CLIENT_ID,
            client_secret=SPOTIFY_CLIENT_SECRET or None,
        )
        return spotipy.Spotify(client_credentials_manager=ccm,
                               requests_session=_throttled_session)
    except Exception as exc:
        raise SpotifyAuthError(f"Failed to create public client: {exc}") from exc


def get_spotify_client() -> spotipy.Spotify:
    """Return an authenticated spotipy.Spotify client.

    Raises SpotifyAuthError if not configured or not authenticated.
    """
    am = _auth_manager()
    if am is None:
        raise SpotifyAuthError("Spotify client_id is not configured")
    try:
        token_info = am.get_cached_token()
        if token_info is None:
            raise SpotifyAuthError("No Spotify token — authenticate first")
    except SpotifyAuthError:
        raise
    except Exception as exc:
        raise SpotifyAuthError(f"Failed to read cached token: {exc}") from exc

    return spotipy.Spotify(auth_manager=am, requests_session=_throttled_session)
