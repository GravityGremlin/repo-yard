"""Discography resolution — fetch artist albums from Spotify, deduplicate, return structured list."""
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
    artist_id: str,
    include_singles: bool = False,
    prefer_explicit: bool = True,
) -> list[DiscographyAlbum]:
    """Fetch and filter an artist's discography via Spotify API.

    Returns albums sorted newest-first, deduplicated by name
    (preferring explicit versions), with singles excluded by default.
    """
    from app.spotify.resolver import _fetch_artist_albums

    logger.info("Resolving discography for artist_id=%s", artist_id)

    raw_albums = _fetch_artist_albums(artist_id)
    if not raw_albums:
        logger.info("No albums found for artist_id=%s", artist_id)
        return []

    artist_name = "Unknown Artist"
    albums: list[DiscographyAlbum] = []

    for item in raw_albums:
        title = item.get("name", "")
        album_type = item.get("album_type", "album")
        num_tracks = item.get("total_tracks", 0)
        release_date = item.get("release_date", "")
        explicit = item.get("explicit", False)

        # Filter out singles/compilations unless requested
        if album_type in ("single", "compilation") and not include_singles:
            logger.debug("Skipping %s (type=%s, tracks=%d)", title, album_type, num_tracks)
            continue

        # Filter out very short releases (singles disguised as albums)
        if num_tracks <= 2 and not include_singles:
            logger.debug("Skipping short release: %s (tracks=%d)", title, num_tracks)
            continue

        # Extract year from release_date
        year = None
        if release_date:
            try:
                year = int(release_date[:4])
            except (ValueError, IndexError):
                pass

        # Get artist name from the first album artist
        artists = item.get("artists", [])
        if artists:
            artist_name = artists[0].get("name", artist_name)

        # Get largest cover image
        images = item.get("images", [])
        cover_url = images[0].get("url", "") if images else None

        albums.append(DiscographyAlbum(
            album_id=item.get("id", ""),
            title=title,
            artist_name=artist_name,
            year=year,
            num_tracks=num_tracks,
            explicit=explicit,
            cover_url=cover_url,
        ))

    # Deduplicate by normalized title, preferring explicit versions
    seen: dict[str, int] = {}  # normalized title → index in deduped list
    deduped: list[DiscographyAlbum] = []
    for album in sorted(albums, key=lambda a: a.year or 0, reverse=True):
        key = _normalize_title(album.title)
        if key in seen:
            existing = deduped[seen[key]]
            if prefer_explicit and album.explicit and not existing.explicit:
                deduped[seen[key]] = album
        else:
            seen[key] = len(deduped)
            deduped.append(album)

    # Sort newest first
    deduped.sort(key=lambda a: a.year or 0, reverse=True)
    logger.info("Resolved %d albums for artist %s", len(deduped), artist_name)
    return deduped
