"""Tests for search routes — mock the Spotify API layer."""

from __future__ import annotations

from unittest.mock import patch, MagicMock


# ---------------------------------------------------------------------------
# Mock data
# ---------------------------------------------------------------------------

MOCK_TRACK_RESULTS = [
    {
        "title": "Test Track",
        "artist": "Test Artist",
        "album": "Test Album",
        "cover_url": "https://example.com/cover.jpg",
        "spotify_id": "abc123",
        "kind": "track",
    }
]

MOCK_ALBUM_RESULTS = [
    {
        "title": "Test Album",
        "artist": "Test Artist",
        "album": "Test Album",
        "cover_url": "https://example.com/cover.jpg",
        "spotify_id": "def456",
        "kind": "album",
        "track_count": 10,
    }
]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestSearchApi:
    """GET /search/api"""

    @patch("app.search.routes.search_spotify")
    def test_search_returns_json(self, mock_search, app_client):
        """Search returns JSON results."""
        mock_search.return_value = MOCK_TRACK_RESULTS
        resp = app_client.get("/search/api?q=test&type=track")
        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data) == 1
        assert data[0]["title"] == "Test Track"

    @patch("app.search.routes.search_spotify")
    def test_search_empty_query(self, mock_search, app_client):
        """Empty query returns empty list."""
        resp = app_client.get("/search/api?q=&type=track")
        assert resp.status_code == 200
        assert resp.get_json() == []

    @patch("app.search.routes.search_spotify")
    def test_search_album(self, mock_search, app_client):
        """Album search returns results."""
        mock_search.return_value = MOCK_ALBUM_RESULTS
        resp = app_client.get("/search/api?q=test&type=album")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data[0]["kind"] == "album"


class TestSearchHtml:
    """GET /search"""

    @patch("app.search.routes.search_spotify")
    def test_search_html_returns_partial(self, mock_search, app_client):
        """HTML search returns an HTMX partial."""
        mock_search.return_value = MOCK_TRACK_RESULTS
        resp = app_client.get("/search?q=test&type=track")
        assert resp.status_code == 200
        assert b"Test Track" in resp.data

    def test_search_html_empty_query(self, app_client):
        """Empty query returns empty results."""
        resp = app_client.get("/search?q=&type=track")
        assert resp.status_code == 200
