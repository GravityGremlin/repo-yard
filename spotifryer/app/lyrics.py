"""Lyrics fetcher for Spotify tracks via Lrclib.

Spotify has no lyrics API. This module queries lrclib.net's public API
using artist, title, album, and duration to find plain/synced lyrics.
"""
from __future__ import annotations

import logging

import requests

logger = logging.getLogger(__name__)

_LRCLIB_BASE = "https://lrclib.net/api"


def fetch_lyrics(
    artist: str,
    title: str,
    album: str = "",
    duration: int = 0,
) -> dict | None:
    """Fetch lyrics for a track via Lrclib.

    Returns ``{"plainLyrics": "...", "syncedLyrics": "...", "source": "lrclib"}``
    or ``None`` if no lyrics are available.
    """
    params: dict = {
        "artist_name": artist,
        "track_name": title,
    }
    if album:
        params["album_name"] = album
    if duration:
        params["duration"] = duration

    try:
        resp = requests.get(f"{_LRCLIB_BASE}/get", params=params, timeout=10)
        if resp.status_code == 404:
            logger.debug("No lyrics found for %s - %s", artist, title)
            return None
        resp.raise_for_status()
        data = resp.json()
        if data.get("plainLyrics") or data.get("syncedLyrics"):
            return {
                "plainLyrics": data.get("plainLyrics", ""),
                "syncedLyrics": data.get("syncedLyrics", ""),
                "source": "lrclib",
            }
        return None
    except requests.RequestException:
        logger.warning("Lrclib request failed for %s - %s", artist, title, exc_info=True)
        return None
    except Exception:
        logger.warning("Unexpected error fetching lyrics for %s - %s", artist, title, exc_info=True)
        return None
