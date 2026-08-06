"""Tests for resolve_discography in app.download.discography."""

from __future__ import annotations

from app.download.discography import resolve_discography, DiscographyAlbum


# ---------------------------------------------------------------------------
# Mock session matching QobuzClient API surface
# ---------------------------------------------------------------------------

class MockSession:
    """Minimal session object matching QobuzClient's discography API."""

    def get_artist(self, art_id):
        return {"name": "Test Artist"}

    def get_artist_discography(self, art_id):
        return [
            {"id": "1", "title": "Album One",
             "release_date": "2024-01-01",
             "tracks_count": 10, "parental_warning": False},
            {"id": "2", "title": "Album One",
             "release_date": "2023-06-15",
             "tracks_count": 12, "parental_warning": True},
            {"id": "3", "title": "Single EP",
             "release_date": "2024-03-01",
             "tracks_count": 2, "parental_warning": False},
        ]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_resolve_discography_basic():
    """Default call: 3 raw albums → singles filtered + dedup → 1 album remains."""
    albums = resolve_discography(MockSession(), "123")

    # "Single EP" (num_tracks=2) filtered out; two "Album One" entries deduped to 1
    assert len(albums) == 1
    titles = {a.title for a in albums}
    assert "Single EP" not in titles
    assert "Album One" in titles


def test_resolve_discography_include_singles():
    """include_singles=True keeps the EP."""
    albums = resolve_discography(MockSession(), "123", include_singles=True)
    titles = {a.title for a in albums}
    assert "Single EP" in titles
    assert "Album One" in titles


def test_resolve_discography_dedup():
    """Dedup prefers the explicit version when prefer_explicit=True."""
    albums = resolve_discography(MockSession(), "123", include_singles=True, prefer_explicit=True)

    # Album One appears twice — deduped to 1, preferring explicit (id=2)
    album_ones = [a for a in albums if a.title == "Album One"]
    assert len(album_ones) == 1
    assert album_ones[0].explicit is True
