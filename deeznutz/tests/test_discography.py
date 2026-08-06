"""Tests for resolve_discography in app.download.discography."""

from __future__ import annotations

from app.download.discography import resolve_discography, DiscographyAlbum


# ---------------------------------------------------------------------------
# Mock session
# ---------------------------------------------------------------------------

class MockSession:
    """Minimal session object matching deezer-py's gw API surface."""

    class MockGW:
        def get_artist(self, art_id):
            return {"ART_NAME": "Test Artist"}

        def get_artist_discography(self, art_id):
            return [
                {"ALB_ID": "1", "ALB_TITLE": "Album One",
                 "DIGITAL_RELEASE_DATE": "2024-01-01",
                 "NUMBER_TRACK": 10, "EXPLICIT_LYRICS": 0},
                {"ALB_ID": "2", "ALB_TITLE": "Album One",
                 "DIGITAL_RELEASE_DATE": "2023-06-15",
                 "NUMBER_TRACK": 12, "EXPLICIT_LYRICS": 1},
                {"ALB_ID": "3", "ALB_TITLE": "Single EP",
                 "DIGITAL_RELEASE_DATE": "2024-03-01",
                 "NUMBER_TRACK": 2, "EXPLICIT_LYRICS": 0},
            ]

    gw = MockGW()


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
    # Artist name extracted from get_artist
    assert albums[0].artist_name == "Test Artist"


def test_resolve_discography_include_singles():
    """include_singles=True: singles pass filter but duplicates still merged → 2 unique albums."""
    albums = resolve_discography(MockSession(), "123", include_singles=True)

    # All 3 pass the singles filter, but the two "Album One" entries dedup to 1
    assert len(albums) == 2
    titles = {a.title for a in albums}
    assert "Single EP" in titles
    assert "Album One" in titles


def test_resolve_discography_dedup():
    """Two 'Album One' entries: prefer_explicit=True keeps the explicit version."""
    albums = resolve_discography(MockSession(), "123", include_singles=True)

    # Should have deduped Album One down to one entry
    one_albums = [a for a in albums if a.title == "Album One"]
    assert len(one_albums) == 1
    # The explicit version (ALB_ID="2", EXPLICIT_LYRICS=1) should be kept
    assert one_albums[0].explicit is True
    assert one_albums[0].album_id == "2"
