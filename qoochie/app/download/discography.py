"""Discography resolution — fetch artist albums from Qobuz, filter, return structured list."""
from __future__ import annotations

import logging
from dataclasses import dataclass

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
    session,
    artist_id: str,
    include_singles: bool = False,
    prefer_explicit: bool = True,
) -> list[DiscographyAlbum]:
    """Fetch and filter an artist's discography via Qobuz API.

    Returns albums sorted newest-first, deduplicated by name
    (preferring explicit versions), with singles excluded by default.
    """
    logger.info("Resolving discography for artist_id=%s", artist_id)

    # Fetch artist metadata
    artist_data = session.get_artist(int(artist_id))
    artist_name = "Unknown Artist"
    if isinstance(artist_data, dict):
        artist_name = artist_data.get("name", "Unknown Artist")
    logger.info("Artist name: %s", artist_name)

    # Fetch all albums (paginated, limit=500)
    raw_albums = session.get_artist_discography(int(artist_id))
    if not raw_albums or not isinstance(raw_albums, list):
        logger.info("No albums found for artist_id=%s", artist_id)
        return []

    albums: list[DiscographyAlbum] = []
    for item in raw_albums:
        title = item.get("title", "")
        if not title:
            continue

        # Parse release date → year
        release_date = item.get("release_date", item.get("DIGITAL_RELEASE_DATE", ""))
        year = None
        if release_date and len(str(release_date)) >= 4:
            try:
                year = int(str(release_date)[:4])
            except (ValueError, TypeError):
                pass

        # Track count
        num_tracks = item.get("tracks_count", item.get("NUMBER_TRACK", 0))

        # Singles filter: if ≤ 2 tracks and not include_singles, skip
        if not include_singles and num_tracks <= 2:
            logger.debug("Skipping single/EP: %s (%d tracks)", title, num_tracks)
            continue

        # Explicit flag
        explicit = bool(item.get("parental_warning", item.get("EXPLICIT_LYRICS", 0)))

        # Cover URL
        img = item.get("image", {})
        cover_url = None
        if isinstance(img, dict):
            cover_url = img.get("large", img.get("medium", ""))

        albums.append(DiscographyAlbum(
            # Qobuz album/get accepts the alphanumeric "id" hash (ALB_ID is
            # absent and qobuz_id 404s on album/get) — keep the raw id.
            album_id=str(item.get("id") or item.get("ALB_ID") or ""),
            title=title,
            artist_name=artist_name,
            year=year,
            num_tracks=num_tracks,
            explicit=explicit,
            cover_url=cover_url or None,
        ))

    # Dedup by normalized title, preferring explicit + newest
    seen: dict[str, DiscographyAlbum] = {}
    for alb in albums:
        key = _normalize_title(alb.title)
        if key in seen:
            existing = seen[key]
            if prefer_explicit and alb.explicit and not existing.explicit:
                seen[key] = alb
            elif alb.year and (not existing.year or alb.year > existing.year):
                seen[key] = alb
        else:
            seen[key] = alb

    # Sort newest first
    result = sorted(seen.values(), key=lambda a: a.year or 0, reverse=True)
    logger.info("Resolved %d albums for artist_id=%s", len(result), artist_id)
    return result
