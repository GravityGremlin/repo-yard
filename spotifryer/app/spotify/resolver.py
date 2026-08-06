"""Spotify URL resolver and metadata fetcher."""

from __future__ import annotations

import functools
import logging
import re
import threading
import time
from collections import OrderedDict

from app.config import SPOTIFY_RESOLVER_CACHE_TTL
from app.spotify.session import get_spotify_client, get_public_spotify_client, SpotifyAuthError

logger = logging.getLogger(__name__)

# ── Read-path result cache ──────────────────────────────────────
# Search/album/playlist/artist metadata is stable for minutes-to-days; caching
# it keeps repeated discography resolution from re-hitting the Spotify API
# (the 429 Retry-After hammering was caused by burst re-fetches).
_CACHE_MAXSIZE = 10_000
_cache: OrderedDict[tuple, tuple[float, object]] = OrderedDict()
_cache_lock = threading.Lock()


def _ttl_cache(ttl: float = SPOTIFY_RESOLVER_CACHE_TTL):
    """Decorator: cache a function's return value for *ttl* seconds, keyed by
    its positional args. Failures are never cached. Thread-safe.

    The cache is bounded to ``_CACHE_MAXSIZE`` entries (LRU eviction on
    insertion when full) so it cannot grow without bound.
    """
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            key = (fn.__name__,) + tuple(args) + tuple(sorted(kwargs.items()))
            now = time.monotonic()
            with _cache_lock:
                hit = _cache.get(key)
                if hit is not None and now - hit[0] < ttl:
                    # Move to end (most-recently used) for LRU order.
                    _cache.move_to_end(key)
                    return hit[1]
            result = fn(*args, **kwargs)
            with _cache_lock:
                _cache[key] = (now, result)
                # Evict oldest entries when the cache exceeds maxsize.
                while len(_cache) > _CACHE_MAXSIZE:
                    _cache.popitem(last=False)
            return result
        return wrapper
    return deco

# ── URL patterns ────────────────────────────────────────────────
_URL_PATTERN = re.compile(
    r"https?://open\.spotify\.com/(?P<kind>track|album|playlist)/(?P<id>[A-Za-z0-9]+)"
)
_URI_PATTERN = re.compile(
    r"spotify:(?P<kind>track|album|playlist):(?P<id>[A-Za-z0-9]+)"
)


def _largest_image(images: list[dict]) -> str:
    """Return the URL of the largest image, or empty string."""
    if not images:
        return ""
    return images[0].get("url", "")


def _thumbnail_image(images: list[dict]) -> str:
    """Return a medium-size thumbnail URL, falling back to largest."""
    if not images:
        return ""
    if len(images) >= 2:
        return images[1].get("url", "")
    return images[0].get("url", "")


def resolve_url(url: str) -> tuple[str, str]:
    """Parse a Spotify URL or URI and return (kind, spotify_id).

    kind is one of 'track', 'album', 'playlist'.
    Raises ValueError if the URL is not a valid Spotify link.
    """
    m = _URL_PATTERN.search(url)
    if m:
        return m.group("kind"), m.group("id")
    m = _URI_PATTERN.search(url)
    if m:
        return m.group("kind"), m.group("id")
    raise ValueError(f"Not a valid Spotify URL or URI: {url}")


@_ttl_cache()
def fetch_track(spotify_id: str) -> dict:
    """Fetch metadata for a single track.

    Returns dict with: title, artist, album, isrc, cover_url, duration_ms,
    track_number, spotify_id, kind.
    """
    try:
        sp = get_spotify_client()
    except SpotifyAuthError:
        raise
    try:
        data = sp.track(spotify_id)
    except Exception as exc:
        raise SpotifyAuthError(f"Failed to fetch track {spotify_id}: {exc}") from exc

    artists = [a.get("name", "") for a in data.get("artists", [])]
    album_name = data.get("album", {}).get("name", "")
    album_images = data.get("album", {}).get("images", [])
    external_ids = data.get("external_ids", {})

    return {
        "title": data.get("name", ""),
        "artist": ", ".join(artists) if artists else "",
        "album": album_name,
        "isrc": external_ids.get("isrc", ""),
        "cover_url": _largest_image(album_images),
        "duration_ms": data.get("duration_ms", 0),
        "track_number": data.get("track_number", 0),
        "spotify_id": data.get("id", spotify_id),
        "kind": "track",
    }


def _fetch_album_meta(sp, album_id: str) -> dict:
    """Fetch album-level metadata (name, artist, images, release_date)."""
    data = sp.album(album_id)
    artists = [a.get("name", "") for a in data.get("artists", [])]
    return {
        "album_name": data.get("name", ""),
        "album_artist": ", ".join(artists) if artists else "",
        "album_images": data.get("images", []),
        "release_date": data.get("release_date", ""),
    }


@_ttl_cache()
def fetch_album_tracks(spotify_id: str) -> list[dict]:
    """Fetch all tracks from an album.

    Returns list of track dicts with: title, artist, album, isrc, cover_url,
    duration_ms, track_number, spotify_id, kind.
    """
    try:
        sp = get_spotify_client()
    except SpotifyAuthError:
        raise

    try:
        album_meta = _fetch_album_meta(sp, spotify_id)
    except Exception as exc:
        raise SpotifyAuthError(f"Failed to fetch album {spotify_id}: {exc}") from exc

    tracks: list[dict] = []
    try:
        results = sp.album_tracks(spotify_id)
        items = results.get("items", [])
        while results.get("next"):
            results = sp.next(results)
            items.extend(results.get("items", []))

        for idx, item in enumerate(items, 1):
            artists = [a.get("name", "") for a in item.get("artists", [])]
            # album_tracks doesn't include ISRC or full images; merge from album
            # If the track has its own album ref, use that; otherwise use album_meta
            track_images = item.get("album", {}).get("images", []) or album_meta["album_images"]

            tracks.append({
                "title": item.get("name", ""),
                "artist": ", ".join(artists) if artists else "",
                "album": album_meta["album_name"],
                "isrc": "",  # album_tracks endpoint doesn't provide ISRC
                "cover_url": _largest_image(track_images),
                "duration_ms": item.get("duration_ms", 0),
                "track_number": item.get("track_number", idx),
                "spotify_id": item.get("id", ""),
                "kind": "track",
            })
    except SpotifyAuthError:
        raise
    except Exception as exc:
        raise SpotifyAuthError(f"Failed to fetch album tracks for {spotify_id}: {exc}") from exc

    return tracks


@_ttl_cache()
def fetch_playlist_tracks(spotify_id: str) -> tuple[list[dict], str]:
    """Fetch tracks from a playlist.

    Returns (tracks_list, playlist_name). Each track dict has: title, artist,
    album, isrc, cover_url, duration_ms, track_number, spotify_id, kind.
    Handles pagination up to 500 tracks.
    """
    try:
        sp = get_spotify_client()
    except SpotifyAuthError:
        raise

    try:
        playlist_data = sp.playlist(spotify_id)
        playlist_name = playlist_data.get("name", "")
    except SpotifyAuthError:
        raise
    except Exception as exc:
        raise SpotifyAuthError(f"Failed to fetch playlist {spotify_id}: {exc}") from exc

    tracks: list[dict] = []
    position = 0
    try:
        # spotipy v3 breaks when fields parameter is used with playlist_items;
        # fields return empty track objects. Omit fields and process full response.
        results = sp.playlist_items(spotify_id)
        items = results.get("items", [])
        while results.get("next") and len(tracks) < 500:
            results = sp.next(results)
            items.extend(results.get("items", []))

        for item in items[:500]:
            # spotipy v3 uses 'item' not 'track' for playlist track items
            track_data = item.get("item")
            if not track_data or track_data.get("is_local", False):
                continue

            position += 1
            artists = [a.get("name", "") for a in track_data.get("artists", [])]
            track_album = track_data.get("album", {})
            track_images = track_album.get("images", [])

            tracks.append({
                "title": track_data.get("name", ""),
                "artist": ", ".join(artists) if artists else "",
                "album": track_album.get("name", ""),
                "isrc": "",  # playlist_items doesn't include ISRC
                "cover_url": _largest_image(track_images),
                "duration_ms": track_data.get("duration_ms", 0),
                "track_number": position,
                "spotify_id": track_data.get("id", ""),
                "kind": "track",
            })
    except SpotifyAuthError:
        raise
    except Exception as exc:
        raise SpotifyAuthError(f"Failed to fetch playlist tracks for {spotify_id}: {exc}") from exc

    return tracks, playlist_name


@_ttl_cache()
def search_spotify(query: str, kind: str = "track", limit: int = 10) -> list[dict]:
    """Search Spotify and return simplified result dicts.

    kind is one of 'track', 'album', 'artist'.
    Returns list of dicts with: title, artist, album, cover_url, spotify_id, kind.
    Uses public client — no user auth required.
    """
    try:
        sp = get_public_spotify_client()
    except SpotifyAuthError:
        raise

    try:
        results = sp.search(q=query, type=kind, limit=limit)
    except SpotifyAuthError:
        raise
    except Exception as exc:
        raise SpotifyAuthError(f"Spotify search failed: {exc}") from exc

    items_key = f"{kind}s"
    items = results.get(items_key, {}).get("items", [])
    output: list[dict] = []

    for item in items:
        if item is None:
            continue
        if kind == "track":
            artists = [a.get("name", "") for a in item.get("artists", [])]
            album_data = item.get("album", {})
            output.append({
                "title": item.get("name", ""),
                "artist": ", ".join(artists) if artists else "",
                "album": album_data.get("name", ""),
                "cover_url": _thumbnail_image(album_data.get("images", [])),
                "spotify_id": item.get("id", ""),
                "url": f"https://open.spotify.com/track/{item.get('id', '')}",
                "kind": "track",
            })
        elif kind == "album":
            artists = [a.get("name", "") for a in item.get("artists", [])]
            output.append({
                "title": item.get("name", ""),
                "artist": ", ".join(artists) if artists else "",
                "album": item.get("name", ""),
                "cover_url": _thumbnail_image(item.get("images", [])),
                "spotify_id": item.get("id", ""),
                "url": f"https://open.spotify.com/album/{item.get('id', '')}",
                "kind": "album",
            })
        elif kind == "artist":
            genres = (item.get("genres") or [])[:3]
            followers_val = item.get("followers") or {}
            followers = followers_val.get("total", 0)
            if followers >= 1_000_000:
                followers_str = f"{followers / 1_000_000:.1f}M"
            elif followers >= 1_000:
                followers_str = f"{followers / 1_000:.1f}K"
            else:
                followers_str = f"{followers:,}"
            output.append({
                "title": item.get("name", ""),
                "artist": ", ".join(genres) if genres else "",
                "spotify_id": item.get("id", ""),
                "cover_url": _largest_image(item.get("images", [])),
                "url": item.get("external_urls", {}).get("spotify", ""),
                "followers": followers_str,
                "kind": "artist",
            })
        elif kind == "playlist":
            owner = item.get("owner", {})
            output.append({
                "title": item.get("name", ""),
                "artist": owner.get("display_name", ""),
                "spotify_id": item.get("id", ""),
                "cover_url": _largest_image(item.get("images", [])),
                "url": item.get("external_urls", {}).get("spotify", ""),
                "track_count": item.get("tracks", {}).get("total", 0),
                "kind": "playlist",
            })

    return output


def resolve_artist_name_to_id(artist_name: str) -> str:
    """Resolve an artist name to a Spotify artist URI.

    Searches Spotify for the artist and returns the artist URI
    (spotify:artist:<id>). Raises ValueError if no matching artist is found.
    """
    results = search_spotify(artist_name, kind="artist", limit=5)
    if not results:
        raise ValueError(f"No artist found matching '{artist_name}'")
    # Return the URI of the best match (highest followers already sorted by Spotify)
    best = results[0]
    artist_id = best.get("spotify_id", "")
    if not artist_id:
        raise ValueError(f"Could not resolve artist ID for '{artist_name}'")
    return f"spotify:artist:{artist_id}"


@_ttl_cache()
def _fetch_artist_albums(artist_id: str) -> list[dict]:
    """Fetch all albums for an artist. Used by discography resolution.

    Returns list of album dicts from the Spotify API (with standard fields:
    id, name, artists, images, release_date, album_type, total_tracks, explicit).
    Handles pagination up to 50 albums.
    """
    try:
        sp = get_spotify_client()
    except SpotifyAuthError:
        raise

    try:
        results = sp.artist_albums(
            artist_id,
            album_type="album",
            country="US",
            limit=10,  # Spotify rejects limit>10 for this client ("Invalid limit")
        )
        items = results.get("items", [])
        while results.get("next") and len(items) < 200:
            results = sp.next(results)
            items.extend(results.get("items", []))
        return items
    except SpotifyAuthError:
        raise
    except Exception as exc:
        raise SpotifyAuthError(f"Failed to fetch artist albums for {artist_id}: {exc}") from exc
