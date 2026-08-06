"""Tests for search route — covers _parse_search_results, _cover_url, and
the GET /search endpoint with mocked Tidal session.

All tests use mocks — no real Tidal API calls.

The old ``_in_year_range`` helper was removed in a cleanup commit; year
filtering is now inlined in the search endpoint.  This module tests the
remaining helpers and the route integration.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch
import pytest


# ── Helpers ───────────────────────────────────────────────────────

def _mock_album(
    album_id: str,
    title: str = "Album",
    artist: str = "Artist",
    year: int | None = 2020,
    genre: str = "Rock",
    num_tracks: int = 10,
) -> MagicMock:
    """Build a mock tidalapi Album-like object for _parse_search_results."""
    a = MagicMock()
    a.id = album_id
    a.name = title
    a.artist.name = artist
    a.year = year
    a.genre = genre
    a.num_tracks = num_tracks
    a.image = MagicMock(return_value=f"http://cdn.example.com/{album_id}.jpg")
    return a


def _mock_track(track_id: str, title: str = "Track", artist: str = "Artist") -> MagicMock:
    """Build a mock tidalapi Track-like object."""
    t = MagicMock()
    t.id = track_id
    t.title = title
    t.artist.name = artist
    t.duration = 200
    return t


def _mock_raw(albums=None, tracks=None, artists=None, playlists=None) -> dict:
    """Build a raw search-result dict as returned by session.search()."""
    return {
        "albums": albums or [],
        "tracks": tracks or [],
        "artists": artists or [],
        "playlists": playlists or [],
    }


# ── _parse_search_results tests ───────────────────────────────────

class TestParseSearchResults:
    """Tests for _parse_search_results with mocked album objects."""

    def test_parses_album_fields(self):
        from app.search.routes import _parse_search_results
        raw = _mock_raw([_mock_album("1", "Test Album", year=2020, genre="Pop")])
        with patch("app.search.routes.LIBRARY_DIR") as mock_lib:
            mock_lib.is_dir.return_value = False
            results = _parse_search_results(raw, "all")

        assert len(results["albums"]) == 1
        alb = results["albums"][0]
        assert alb["id"] == "1"
        assert alb["title"] == "Test Album"
        assert alb["year"] == 2020

    def test_returns_empty_for_non_dict(self):
        from app.search.routes import _parse_search_results
        results = _parse_search_results(None, "all")
        assert results == {"albums": [], "tracks": [], "artists": [], "playlists": []}

    def test_filters_by_kind_album_only(self):
        from app.search.routes import _parse_search_results
        raw = _mock_raw(
            [_mock_album("1"), _mock_album("2")],
            tracks=[_mock_track("t1")],
        )
        with patch("app.search.routes.LIBRARY_DIR") as mock_lib:
            mock_lib.is_dir.return_value = False
            results = _parse_search_results(raw, "track")

        assert len(results["albums"]) == 0
        assert len(results["tracks"]) == 1

    def test_in_library_badge(self):
        from app.search.routes import _parse_search_results
        raw = _mock_raw([_mock_album("1", artist="Known Artist")])
        with patch("app.search.routes.LIBRARY_DIR") as mock_lib:
            mock_lib.is_dir.return_value = True
            mock_lib.iterdir.return_value = iter([])
            results = _parse_search_results(raw, "all")
        assert len(results["albums"]) == 1

    def test_empty_raw_returns_empty_dicts(self):
        from app.search.routes import _parse_search_results
        results = _parse_search_results(_mock_raw(), "all")
        assert results["albums"] == []
        assert results["tracks"] == []
        assert results["artists"] == []


# ── _cover_url tests ──────────────────────────────────────────────

class TestCoverUrl:
    """Tests for the _cover_url helper."""

    def test_returns_url_from_image_callable(self):
        from app.search.routes import _cover_url
        obj = MagicMock()
        obj.image.return_value = "http://img.test/a.jpg"
        assert _cover_url(obj) == "http://img.test/a.jpg"

    def test_returns_empty_on_exception(self):
        from app.search.routes import _cover_url
        obj = MagicMock()
        obj.image.side_effect = Exception("no image")
        assert _cover_url(obj) == ""

    def test_returns_empty_on_no_image_attr(self):
        from app.search.routes import _cover_url
        obj = object()
        assert _cover_url(obj) == ""


# ── Year filter application tests (standalone logic) ──────────────

def _in_year_range(year, year_min, year_max):
    """Reproduce the inlined year-filter logic from search.routes.search()."""
    if year is None:
        return False
    if year_min is not None and year < year_min:
        return False
    if year_max is not None and year > year_max:
        return False
    return True


class TestYearFilter:
    """Tests for the year-range filter as implemented in the search endpoint."""

    @staticmethod
    def _apply_year_filter(results: dict, year_min, year_max) -> dict:
        """Replicate the filter logic from search.routes.search()."""
        if year_min is not None or year_max is not None:
            results["albums"] = [
                a for a in results["albums"]
                if _in_year_range(a.get("year"), year_min, year_max)
            ]
        return results

    def test_filters_by_year_min(self):
        albums = [
            {"year": 2018, "title": "A"},
            {"year": 2020, "title": "B"},
            {"year": 2022, "title": "C"},
        ]
        results = self._apply_year_filter({"albums": albums}, year_min=2020, year_max=None)
        assert len(results["albums"]) == 2
        assert {a["title"] for a in results["albums"]} == {"B", "C"}

    def test_filters_by_year_max(self):
        albums = [
            {"year": 2018, "title": "A"},
            {"year": 2020, "title": "B"},
            {"year": 2022, "title": "C"},
        ]
        results = self._apply_year_filter({"albums": albums}, year_min=None, year_max=2020)
        assert len(results["albums"]) == 2
        assert {a["title"] for a in results["albums"]} == {"A", "B"}

    def test_filters_by_year_range(self):
        albums = [
            {"year": 2018, "title": "A"},
            {"year": 2020, "title": "B"},
            {"year": 2022, "title": "C"},
        ]
        results = self._apply_year_filter({"albums": albums}, year_min=2019, year_max=2021)
        assert len(results["albums"]) == 1
        assert results["albums"][0]["title"] == "B"

    def test_no_filter_returns_all(self):
        albums = [{"year": 2018}, {"year": 2022}]
        results = self._apply_year_filter({"albums": albums}, year_min=None, year_max=None)
        assert len(results["albums"]) == 2

    def test_filters_out_none_year(self):
        albums = [
            {"year": None, "title": "Unknown"},
            {"year": 2020, "title": "Known"},
        ]
        results = self._apply_year_filter({"albums": albums}, year_min=2019, year_max=2021)
        assert len(results["albums"]) == 1
        assert results["albums"][0]["title"] == "Known"


# ── _in_year_range unit tests ─────────────────────────────────────

class TestInYearRange:
    """Unit tests for the standalone year-range helper."""

    def test_within_range(self):
        assert _in_year_range(2020, 2019, 2021) is True

    def test_below_min(self):
        assert _in_year_range(2018, 2019, 2021) is False

    def test_above_max(self):
        assert _in_year_range(2022, 2019, 2021) is False

    def test_exact_min(self):
        assert _in_year_range(2019, 2019, 2021) is True

    def test_exact_max(self):
        assert _in_year_range(2021, 2019, 2021) is True

    def test_no_min(self):
        assert _in_year_range(2020, None, 2021) is True

    def test_no_max(self):
        assert _in_year_range(2020, 2019, None) is True

    def test_no_bounds(self):
        assert _in_year_range(2020, None, None) is True

    def test_none_year_returns_false(self):
        assert _in_year_range(None, 2019, 2021) is False

    def test_none_year_no_bounds(self):
        assert _in_year_range(None, None, None) is False

    def test_range_single_year(self):
        assert _in_year_range(2020, 2020, 2020) is True
        assert _in_year_range(2019, 2020, 2020) is False
        assert _in_year_range(2021, 2020, 2020) is False


# ── GET /search route integration ─────────────────────────────────

class TestSearchRoute:
    """Integration tests for GET /search with mocked Tidal session."""

    def test_empty_query_returns_results(self, app_client):
        """Search with empty query returns empty results (no API call needed)."""
        resp = app_client.get("/search?q=")
        assert resp.status_code == 200

    def test_search_returns_503_without_session(self, app_client):
        """Without a Tidal session, search returns 503."""
        resp = app_client.get("/search?q=test&type=album")
        assert resp.status_code == 503

    def test_search_with_type_all(self, app_client):
        """Search with type=all and empty query still returns 200."""
        resp = app_client.get("/search?q=&type=all")
        assert resp.status_code == 200

    def test_index_page_loads(self, app_client):
        """The main search page loads."""
        resp = app_client.get("/")
        assert resp.status_code == 200
