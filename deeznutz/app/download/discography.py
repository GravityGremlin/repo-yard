"""Discography resolution — fetch artist albums from Deezer, filter, return structured list."""
from __future__ import annotations

import logging
from dataclasses import dataclass

# deezer-py's Deezer instance is the session (returned by get_session/init_session)

logger = logging.getLogger(__name__)


@dataclass
class DiscographyAlbum:
    """Album info ready for Job creation."""
    album_id: str
    title: str
    artist_name: str
    year: int | None
    num_tracks: int
    explicit: bool
    cover_url: str | None = None


def _normalize_title(title: str) -> str:
    """Normalize album title for dedup comparison: strip, lower, collapse whitespace."""
    return " ".join(title.strip().lower().split())


def resolve_discography(
    session,   # deezer.Deezer — session from get_session() or init_session()
    artist_id: str,
    include_singles: bool = False,
    prefer_explicit: bool = True,
) -> list[DiscographyAlbum]:
    """Fetch and filter an artist's discography via deezer-py.

    Returns albums sorted newest-first, deduplicated by name
    (preferring explicit versions), with singles excluded by default.
    """
    logger.info("Resolving discography for artist_id=%s", artist_id)

    # Fetch artist metadata
    artist_data = session.gw.get_artist(int(artist_id))
    artist_name = (artist_data or {}).get("ART_NAME", "Unknown Artist")
    logger.info("Artist name: %s", artist_name)

    # Fetch all albums (may return list of dicts)
    raw_albums = session.gw.get_artist_discography(int(artist_id))
    if not raw_albums or not isinstance(raw_albums, list):
        logger.info("No albums found for artist_id=%s", artist_id)
        return []

    logger.info("Fetched %d albums for artist_id=%s", len(raw_albums), artist_id)

    # Map raw dicts to DiscographyAlbum entries
    all_albums: list[DiscographyAlbum] = []
    for a in raw_albums:
        if not isinstance(a, dict):
            continue
        a_id = a.get("ALB_ID", "") or a.get("id", "")
        a_title = a.get("ALB_TITLE", "") or a.get("title", "")
        a_year_str = a.get("DIGITAL_RELEASE_DATE", "") or a.get("ORIGINAL_RELEASE_DATE", "")
        a_num_tracks = int(a.get("NUMBER_TRACK", 0) or a.get("NB_TRACK", 0) or 0)
        a_explicit = bool(a.get("EXPLICIT_LYRICS", 0) or a.get("explicit_lyrics", 0))
        a_cover = a.get("ALB_PICTURE", "") or a.get("cover_url", "")
        # Parse year from release date
        year: int | None = None
        if a_year_str and len(str(a_year_str)) >= 4:
            try:
                year = int(str(a_year_str)[:4])
            except (ValueError, TypeError):
                pass
        all_albums.append(DiscographyAlbum(
            album_id=str(a_id),
            title=a_title or "Unknown Album",
            artist_name=artist_name,
            year=year,
            num_tracks=a_num_tracks,
            explicit=a_explicit,
            cover_url=a_cover or None,
        ))

    if not all_albums:
        return []

    # Filter out singles/EPs if requested
    if not include_singles:
        before = len(all_albums)
        all_albums = [a for a in all_albums if a.num_tracks > 2]
        logger.info("Filtered out %d singles/EPs (num_tracks <= 2)", before - len(all_albums))

    if not all_albums:
        return []

    # Deduplicate by normalized title, preferring explicit versions
    deduped: dict[str, DiscographyAlbum] = {}
    for album in all_albums:
        normalized = _normalize_title(album.title)
        if not normalized:
            continue
        existing = deduped.get(normalized)
        if existing is None:
            deduped[normalized] = album
        elif prefer_explicit and album.explicit and not existing.explicit:
            deduped[normalized] = album
            logger.debug("Preferred explicit version of %r", album.title)

    deduped_albums = list(deduped.values())
    logger.info("Deduplicated to %d albums (prefer_explicit=%s)", len(deduped_albums), prefer_explicit)

    # Sort newest first (year descending, None last)
    deduped_albums.sort(key=lambda a: (a.year is None, -(a.year or 0)))

    logger.info("Resolved %d albums for artist_id=%s (include_singles=%s, prefer_explicit=%s)",
                len(deduped_albums), artist_id, include_singles, prefer_explicit)
    return deduped_albums