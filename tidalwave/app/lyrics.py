"""Lyrics — fetch and display lyrics for Tidal tracks."""
from __future__ import annotations

import logging
from app.tidal.session import get_session

logger = logging.getLogger(__name__)

def get_lyrics(track_id: str) -> dict:
    """Fetch lyrics for a track. Returns {'text': str, 'source': str} or {} if unavailable."""
    session = get_session()
    if not session:
        return {}
    try:
        track = session.track(track_id)
        lyrics = track.lyrics()
        if lyrics:
            return {"text": lyrics.text or "", "source": "Tidal"}
    except Exception as exc:
        logger.warning("Lyrics fetch failed for track %s: %s", track_id, exc)
    return {}
