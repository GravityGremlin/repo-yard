"""Discography resolution — fetch artist albums from Tidal, filter, return structured list."""
from __future__ import annotations

import logging
from dataclasses import dataclass

from tidalapi.session import Session
from tidalapi.album import Album

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
    session: Session,
    artist_id: str,
    include_singles: bool = False,
    prefer_explicit: bool = True,
) -> list[DiscographyAlbum]:
    """Fetch and filter an artist's discography.

    Returns albums sorted newest-first, deduplicated by name
    (preferring explicit versions), with singles excluded by default.
    """
    logger.info("Resolving discography for artist_id=%s", artist_id)

    artist = session.artist(artist_id)
    artist_name = getattr(artist, "name", "Unknown Artist")
    logger.info("Artist name: %s", artist_name)

    all_albums: list[Album] = []
    offset = 0
    limit = 50
    _MAX_PAGES = 10  # safety cap: 10 × 50 = 500 albums

    # Paginate through all albums
    for page in range(_MAX_PAGES):
        logger.debug("Fetching albums offset=%d limit=%d", offset, limit)
        albums = artist.get_albums(limit=limit, offset=offset)
        if not albums:
            break
        all_albums.extend(albums)
        if len(albums) < limit:
            break
        offset += limit
    else:
        logger.warning(
            "Discography pagination hit cap (%d pages) for artist_id=%s — results may be truncated",
            _MAX_PAGES, artist_id,
        )

    logger.info("Fetched %d total albums for artist_id=%s", len(all_albums), artist_id)

    if not all_albums:
        logger.info("No albums found for artist_id=%s", artist_id)
        return []

    # Filter out singles/EPs if requested
    if not include_singles:
        before = len(all_albums)
        all_albums = [a for a in all_albums if getattr(a, "num_tracks", 0) > 2]
        logger.info("Filtered out %d singles/EPs (num_tracks <= 2)", before - len(all_albums))

    # Deduplicate by normalized title, preferring explicit versions
    deduped: dict[str, Album] = {}
    for album in all_albums:
        title = getattr(album, "name", "") or ""
        normalized = _normalize_title(title)
        if not normalized:
            continue

        is_explicit = getattr(album, "explicit", False)
        existing = deduped.get(normalized)

        if existing is None:
            deduped[normalized] = album
        elif prefer_explicit and is_explicit and not getattr(existing, "explicit", False):
            # Replace with explicit version
            deduped[normalized] = album
            logger.debug("Preferred explicit version of %r", title)
        # If existing is already explicit or we don't prefer explicit, keep existing

    deduped_albums = list(deduped.values())
    logger.info("Deduplicated to %d albums (prefer_explicit=%s)", len(deduped_albums), prefer_explicit)

    # Build structured output and sort newest first
    result: list[DiscographyAlbum] = []
    for album in deduped_albums:
        album_id = str(getattr(album, "id", "")) or ""
        title = getattr(album, "name", "") or ""
        year = getattr(album, "year", None)
        num_tracks = getattr(album, "num_tracks", 0) or 0
        explicit = getattr(album, "explicit", False) or False
        cover_url = getattr(album, "cover", None)

        result.append(DiscographyAlbum(
            album_id=album_id,
            title=title,
            artist_name=artist_name,
            year=year,
            num_tracks=num_tracks,
            explicit=explicit,
            cover_url=cover_url,
        ))

    # Sort newest first (year descending, None last)
    # Use negative year for descending order, with None as -inf (sorts last)
    result.sort(key=lambda a: (a.year is None, -(a.year or 0)))

    logger.info("Resolved %d albums for artist_id=%s (include_singles=%s, prefer_explicit=%s)",
                len(result), artist_id, include_singles, prefer_explicit)

    return result