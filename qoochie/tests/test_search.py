"""Tests for _parse_search_results in app.search.routes."""

from __future__ import annotations

from app.search.routes import _parse_search_results

# ---------------------------------------------------------------------------
# Test data — matches real Qobuz API category-keyed format
# ---------------------------------------------------------------------------

TRACK_ITEM = {
    "SNG_ID": "1",
    "SNG_TITLE": "Test Track",
    "ART_NAME": "Test Artist",
    "ALB_TITLE": "Test Album",
    "DURATION": 180,
}

ALBUM_ITEM = {
    "ALB_ID": "1",
    "ALB_TITLE": "Test Album",
    "ART_NAME": "Test Artist",
}

ARTIST_ITEM = {
    "ART_ID": "1",
    "ART_NAME": "Test Artist",
}


def _response(*, tracks=None, albums=None, artists=None, playlists=None):
    """Build a synthetic response matching real qobuz gw.search() format."""
    resp = {}
    if tracks:
        resp["TRACK"] = {"data": tracks}
    if albums:
        resp["ALBUM"] = {"data": albums}
    if artists:
        resp["ARTIST"] = {"data": artists}
    if playlists:
        resp["PLAYLIST"] = {"data": playlists}
    return resp


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_parse_search_all():
    """Mixed response populates all categories."""
    raw = _response(tracks=[TRACK_ITEM], albums=[ALBUM_ITEM], artists=[ARTIST_ITEM])
    results = _parse_search_results(raw, "all")

    assert len(results["tracks"]) == 1
    assert results["tracks"][0]["id"] == "1"
    assert results["tracks"][0]["title"] == "Test Track"

    assert len(results["albums"]) == 1
    assert results["albums"][0]["id"] == "1"
    assert results["albums"][0]["title"] == "Test Album"

    assert len(results["artists"]) == 1
    assert results["artists"][0]["id"] == "1"
    assert results["artists"][0]["name"] == "Test Artist"


def test_parse_search_type_filter_track():
    """Kind='track' returns only tracks."""
    raw = _response(tracks=[TRACK_ITEM], albums=[ALBUM_ITEM])
    results = _parse_search_results(raw, "track")

    assert len(results["tracks"]) == 1
    assert len(results["albums"]) == 0
    assert len(results["artists"]) == 0


def test_parse_search_empty():
    """Empty sections produce empty results."""
    raw = _response()
    results = _parse_search_results(raw, "all")
    assert results["tracks"] == []
    assert results["albums"] == []
    assert results["artists"] == []


def test_parse_search_none():
    """None / falsy input returns empty results."""
    results = _parse_search_results(None, "all")
    assert results["tracks"] == []
    results2 = _parse_search_results({}, "all")
    assert results2["tracks"] == []


def test_parse_search_unknown_type():
    """Unknown section keys are silently ignored."""
    raw = {"OTHER": {"data": [{"id": "99"}]}}
    results = _parse_search_results(raw, "all")
    assert results["tracks"] == []
    assert results["albums"] == []
    assert results["artists"] == []
