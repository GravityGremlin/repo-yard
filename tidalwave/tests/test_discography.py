"""Tests for app.download.discography.resolve_discography."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.download.discography import resolve_discography, DiscographyAlbum


def _make_album(
    id: int,
    name: str,
    num_tracks: int = 10,
    explicit: bool = True,
    year: int | None = 2020,
    artist_name: str = "Test Artist",
) -> MagicMock:
    """Create a mock tidalapi Album object matching the real object's attribute names."""
    album = MagicMock()
    album.id = str(id)
    album.name = name
    album.num_tracks = num_tracks
    album.explicit = explicit
    album.year = year
    album.cover = f"http://cdn.example.com/{id}.jpg"
    return album


class TestResolveDiscography:
    """Unit tests for resolve_discography()."""

    def test_returns_albums_from_artist(self):
        session = MagicMock()
        artist = MagicMock()
        artist.name = "Test Artist"
        artist.get_albums.return_value = [
            _make_album(1, "Album A", num_tracks=12),
            _make_album(2, "Album B", num_tracks=8),
        ]
        session.artist.return_value = artist

        result = resolve_discography(session, "123")

        assert len(result) == 2
        assert isinstance(result[0], DiscographyAlbum)
        assert result[0].album_id == "1"
        assert result[1].album_id == "2"
        assert result[0].title in ("Album A", "Album B")
        assert result[1].artist_name == "Test Artist"
        session.artist.assert_called_once_with("123")

    def test_filters_singles_when_excluded(self):
        session = MagicMock()
        artist = MagicMock()
        artist.name = "Test Artist"
        artist.get_albums.return_value = [
            _make_album(1, "Full Album", num_tracks=10),
            _make_album(2, "Single", num_tracks=1),
            _make_album(3, "Double Single", num_tracks=2),
            _make_album(4, "EP", num_tracks=5),
        ]
        session.artist.return_value = artist

        result = resolve_discography(session, "123", include_singles=False)

        # Only albums with num_tracks > 2 should be kept
        assert len(result) == 2
        titles = {a.title for a in result}
        assert titles == {"Full Album", "EP"}

    def test_includes_singles_when_included(self):
        session = MagicMock()
        artist = MagicMock()
        artist.name = "Test Artist"
        artist.get_albums.return_value = [
            _make_album(1, "Full Album", num_tracks=10),
            _make_album(2, "Single", num_tracks=1),
            _make_album(3, "Double Single", num_tracks=2),
            _make_album(4, "EP", num_tracks=5),
        ]
        session.artist.return_value = artist

        result = resolve_discography(session, "123", include_singles=True)

        assert len(result) == 4
        titles = {a.title for a in result}
        assert titles == {"Full Album", "Single", "Double Single", "EP"}

    def test_prefers_explicit_when_duplicate_titles(self):
        """When two albums have the same normalized title, prefer explicit version."""
        session = MagicMock()
        artist = MagicMock()
        artist.name = "Test Artist"
        artist.get_albums.return_value = [
            _make_album(1, "Album", num_tracks=10, explicit=False, year=2020),
            _make_album(2, "ALBUM", num_tracks=10, explicit=True, year=2021),  # same normalized name
            _make_album(3, "Album ", num_tracks=10, explicit=False, year=2019),  # same with trailing space
        ]
        session.artist.return_value = artist

        result = resolve_discography(session, "123", prefer_explicit=True)

        assert len(result) == 1
        assert result[0].album_id == "2"  # explicit version preferred
        assert result[0].explicit is True

    def test_keeps_first_when_no_explicit_preference(self):
        """When prefer_explicit=False, keep the first encountered version."""
        session = MagicMock()
        artist = MagicMock()
        artist.name = "Test Artist"
        artist.get_albums.return_value = [
            _make_album(1, "Album", num_tracks=10, explicit=False, year=2020),
            _make_album(2, "ALBUM", num_tracks=10, explicit=True, year=2021),
        ]
        session.artist.return_value = artist

        result = resolve_discography(session, "123", prefer_explicit=False)

        assert len(result) == 1
        assert result[0].album_id == "1"  # first encountered wins

    def test_sorts_newest_first_by_year(self):
        """Albums should be sorted newest-first by year."""
        session = MagicMock()
        artist = MagicMock()
        artist.name = "Test Artist"
        artist.get_albums.return_value = [
            _make_album(1, "Old Album", year=2005),
            _make_album(2, "New Album", year=2024),
            _make_album(3, "Mid Album", year=2015),
        ]
        session.artist.return_value = artist

        result = resolve_discography(session, "123")

        assert result[0].year == 2024
        assert result[1].year == 2015
        assert result[2].year == 2005
        assert result[0].title == "New Album"
        assert result[-1].title == "Old Album"

    def test_handles_none_year_puts_at_end(self):
        """Albums with None year should sort to the end (oldest)."""
        session = MagicMock()
        artist = MagicMock()
        artist.name = "Test Artist"
        artist.get_albums.return_value = [
            _make_album(1, "No Year Album", year=None),
            _make_album(2, "Has Year", year=2020),
        ]
        session.artist.return_value = artist

        result = resolve_discography(session, "123")

        assert result[0].year == 2020
        assert result[1].year is None

    def test_pagination_fetches_multiple_pages(self):
        """Paginates through artist.get_albums until fewer than limit returned."""
        session = MagicMock()
        artist = MagicMock()
        artist.name = "Prolific Artist"
        # Simulate 120 albums across 3 pages (50, 50, 20)
        page1 = [_make_album(i, f"Album {i}") for i in range(50)]
        page2 = [_make_album(i, f"Album {i}") for i in range(50, 100)]
        page3 = [_make_album(i, f"Album {i}") for i in range(100, 120)]
        artist.get_albums.side_effect = lambda limit=50, offset=0: (
            page1 if offset == 0 else page2 if offset == 50 else page3
        )
        session.artist.return_value = artist

        result = resolve_discography(session, "123", include_singles=True)

        assert len(result) == 120
        # Verify all three pages were fetched
        assert artist.get_albums.call_count == 3
        calls = artist.get_albums.call_args_list
        assert calls[0] == ((), {"limit": 50, "offset": 0})
        assert calls[1] == ((), {"limit": 50, "offset": 50})
        assert calls[2] == ((), {"limit": 50, "offset": 100})

    def test_pagination_stops_when_fewer_than_limit(self):
        """Stops paginating when fewer than limit results returned."""
        session = MagicMock()
        artist = MagicMock()
        artist.name = "Test Artist"
        # First call returns 2 albums (less than limit=50), so pagination stops
        artist.get_albums.return_value = [
            _make_album(1, "Album 1"),
            _make_album(2, "Album 2"),
        ]
        session.artist.return_value = artist

        result = resolve_discography(session, "123", include_singles=True)

        assert len(result) == 2
        # Only one call since we got fewer than 50 results
        assert artist.get_albums.call_count == 1

    def test_empty_discography_returns_empty_list(self):
        session = MagicMock()
        artist = MagicMock()
        artist.name = "Empty Artist"
        artist.get_albums.return_value = []
        session.artist.return_value = artist

        result = resolve_discography(session, "123")

        assert result == []

    def test_discography_album_fields_populated(self):
        """All DiscographyAlbum fields should be populated correctly."""
        session = MagicMock()
        artist = MagicMock()
        artist.name = "Test Artist"
        artist.get_albums.return_value = [
            _make_album(1, "Test Album", num_tracks=12, explicit=True, year=2022),
        ]
        session.artist.return_value = artist

        result = resolve_discography(session, "123")

        assert len(result) == 1
        album = result[0]
        assert album.album_id == "1"
        assert album.title == "Test Album"
        assert album.artist_name == "Test Artist"
        assert album.year == 2022
        assert album.num_tracks == 12
        assert album.explicit is True
        assert album.cover_url == "http://cdn.example.com/1.jpg"

    def test_dedup_normalizes_title_case_insensitive_and_whitespace(self):
        """Dedup should normalize by stripping whitespace and lowercasing."""
        session = MagicMock()
        artist = MagicMock()
        artist.name = "Test Artist"
        artist.get_albums.return_value = [
            _make_album(1, "Album", num_tracks=10, explicit=False),
            _make_album(2, "  album  ", num_tracks=10, explicit=True),
        ]
        session.artist.return_value = artist

        result = resolve_discography(session, "123", prefer_explicit=True)

        assert len(result) == 1
        assert result[0].explicit is True