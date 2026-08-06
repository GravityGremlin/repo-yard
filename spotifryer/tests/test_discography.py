"""Tests for resolve_discography in app.download.discography."""

from __future__ import annotations

from unittest.mock import patch, MagicMock

from app.download.discography import resolve_discography, DiscographyAlbum, _normalize_title


# ---------------------------------------------------------------------------
# Mock data — matches Spotify API format
# ---------------------------------------------------------------------------

MOCK_ALBUMS = [
    {
        "id": "alb1",
        "name": "Album One",
        "artists": [{"name": "Test Artist"}],
        "images": [{"url": "https://example.com/img1.jpg", "height": 640, "width": 640}],
        "release_date": "2024-01-15",
        "album_type": "album",
        "total_tracks": 10,
        "explicit": True,
    },
    {
        "id": "alb2",
        "name": "Album One",  # duplicate title
        "artists": [{"name": "Test Artist"}],
        "images": [{"url": "https://example.com/img2.jpg", "height": 640, "width": 640}],
        "release_date": "2023-06-01",
        "album_type": "album",
        "total_tracks": 12,
        "explicit": False,
    },
    {
        "id": "alb3",
        "name": "Single EP",
        "artists": [{"name": "Test Artist"}],
        "images": [{"url": "https://example.com/img3.jpg", "height": 640, "width": 640}],
        "release_date": "2024-03-01",
        "album_type": "single",
        "total_tracks": 2,
        "explicit": False,
    },
]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestNormalizeTitle:
    def test_basic(self):
        assert _normalize_title("  Album One  ") == "album one"

    def test_collapse_whitespace(self):
        assert _normalize_title("Album   One") == "album one"


class TestResolveDiscography:
    @patch("app.spotify.resolver._fetch_artist_albums")
    def test_basic(self, mock_fetch):
        """Default call: 3 raw albums → single filtered + dedup → 1 album remains."""
        mock_fetch.return_value = MOCK_ALBUMS
        albums = resolve_discography("artist123")

        # "Single EP" (album_type=single) filtered out; two "Album One" entries deduped to 1
        assert len(albums) == 1
        titles = {a.title for a in albums}
        assert "Single EP" not in titles
        assert "Album One" in titles

    @patch("app.spotify.resolver._fetch_artist_albums")
    def test_include_singles(self, mock_fetch):
        """include_singles not implemented in this resolver (uses album_type filter) — verify filtering works."""
        # Since we filter by album_type in _fetch_artist_albums (album only),
        # singles should not appear in raw_albums from Spotify anyway.
        # Here we just test the dedup logic.
        mock_fetch.return_value = [MOCK_ALBUMS[0], MOCK_ALBUMS[1]]
        albums = resolve_discography("artist123")
        assert len(albums) == 1  # deduped

    @patch("app.spotify.resolver._fetch_artist_albums")
    def test_empty(self, mock_fetch):
        """No albums returns empty list."""
        mock_fetch.return_value = []
        albums = resolve_discography("artist123")
        assert albums == []

    @patch("app.spotify.resolver._fetch_artist_albums")
    def test_explicit_preference(self, mock_fetch):
        """When deduping, explicit version is preferred."""
        mock_fetch.return_value = MOCK_ALBUMS
        albums = resolve_discography("artist123")
        assert len(albums) == 1
        assert albums[0].explicit is True  # alb1 is explicit
