"""Unit tests for service URL detection (playlist/album/track/artist URLs)."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.search_aggregator.url_detect import detect_url


def test_tidal_playlist_url():
    d = detect_url("https://tidal.com/playlist/fb4fa8cb-cff0-4c42-b827-7c96074e2f59")
    assert d is not None
    assert d.service == "tidalwave" and d.kind == "playlist"
    assert d.id == "fb4fa8cb-cff0-4c42-b827-7c96074e2f59"


def test_tidal_without_scheme():
    d = detect_url("tidal.com/album/123456")
    assert d is not None and d.service == "tidalwave" and d.kind == "album"


def test_spotify_playlist_with_query():
    d = detect_url("https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M?si=abc&utm=1")
    assert d is not None and d.service == "spotifryer" and d.kind == "playlist"
    assert d.id == "37i9dQZF1DXcBWIGoYBM5M"
    assert "?" not in d.url and "&" not in d.url


def test_qobuz_locale_subdomain():
    d = detect_url("https://www.qobuz.com/us-en/album/0060244561205")
    # www.qobuz.com is a known host; kind=album matches
    assert d is not None and d.service == "qoochie" and d.kind == "album"


def test_deezer_track():
    d = detect_url("https://deezer.com/track/3135556")
    assert d is not None and d.service == "deeznutz" and d.kind == "track"


def test_plain_text_is_not_url():
    assert detect_url("skrillex") is None
    assert detect_url("search the library") is None


def test_unknown_host():
    assert detect_url("https://youtube.com/playlist/abc") is None


def test_wrong_kind_for_host():
    # tidal.com doesn't have a "mix" kind
    assert detect_url("https://tidal.com/mix/xyz") is None


def test_known_host_unknown_service_kept():
    # open.spotify.com is spotifryer; artist URL resolves
    d = detect_url("https://open.spotify.com/artist/7dGJo4pcD2V6oG8kP0tJRR")
    assert d is not None and d.service == "spotifryer" and d.kind == "artist"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
    print("all url_detect tests passed")
