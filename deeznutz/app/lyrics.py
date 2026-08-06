"""Lyrics fetcher for Deezer tracks.

Deezer provides synchronised lyrics (LRC format) through its API.
This module wraps the deezer-py gw.get_track_lyrics() call.
"""

from __future__ import annotations

import logging

_log = logging.getLogger(__name__)


def fetch_lyrics(track_id: int | str) -> dict | None:
    """Fetch lyrics for a Deezer track via deezer-py's get_track_lyrics.

    Returns ``{"text": "...", "lrc": "...", "synced": true/false}``
    or ``None`` if no lyrics are available.
    """
    from app.deezer.session import get_session

    session = get_session()
    if not session:
        return None
    try:
        raw = session.gw.get_track_lyrics(int(track_id))
        if raw and raw.get("LYRICS_TEXT"):
            return {
                "text": raw["LYRICS_TEXT"],
                "lrc": raw.get("LYRICS_SYNC_JSON"),
                "synced": bool(raw.get("LYRICS_SYNC_JSON")),
            }
    except Exception:
        _log.debug("No lyrics available for track %s", track_id)
    return None
