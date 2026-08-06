"""Unit tests for the download dispatcher (repo-yard app side).

These stub the outgoing HTTP calls so no network / live fleet is touched.
Run with:
    python -m pytest tests/ -q
    # or, without pytest installed:
    python tests/test_download.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from app.search_aggregator.download import dispatch_download, _enqueue_payload
from app.search_aggregator import download as _download_mod

# Point every tool at a dead port so any accidental live call fails fast
# rather than hitting the real fleet during tests.
import app.search_aggregator.aggregator as agg
for _t in ("spotifryer", "qoochie", "tidalwave", "deeznutz"):
    agg.TOOLS[_t] = "http://127.0.0.1:1"


def _fake_post_factory(monkeypatch, envelope):
    calls = {}

    def _fake_post(url, json=None, params=None, timeout=None):
        calls[url] = json or {}
        class _R:
            ok = True
            content = b'{}'
            def raise_for_status(self):
                pass
            def json(self):
                return envelope
        return _R()

    monkeypatch.setattr(_download_mod._search_session, "post", _fake_post)
    return calls


def test_enqueue_payload_spotifryer_uses_kind():
    assert _enqueue_payload("spotifryer", "http://x", "album") == {"url": "http://x", "kind": "album"}


def test_enqueue_payload_others_use_type():
    assert _enqueue_payload("qoochie", "http://x", "track") == {"url": "http://x", "type": "track"}


def test_dispatch_track_uses_enqueue(monkeypatch):
    calls = _fake_post_factory(monkeypatch, {"job_id": 7})
    out = dispatch_download("tidalwave", "track", "http://tidal/track/1")
    assert out["job_id"] == 7
    url, body = list(calls.items())[0]
    assert url.endswith("/download/enqueue")
    assert body == {"url": "http://tidal/track/1", "type": "track"}


def test_dispatch_playlist_uses_enqueue_for_all_tools(monkeypatch):
    calls = _fake_post_factory(monkeypatch, {"job_id": 8})
    dispatch_download("tidalwave", "playlist", "http://tidal/pl/2")
    url, body = list(calls.items())[0]
    assert url.endswith("/download/enqueue")
    assert body == {"url": "http://tidal/pl/2", "type": "playlist"}


def test_dispatch_playlist_spotify_falls_back_to_enqueue(monkeypatch):
    calls = _fake_post_factory(monkeypatch, {"job_id": 9})
    dispatch_download("spotifryer", "playlist", "http://spotify/pl/3")
    url, body = list(calls.items())[0]
    assert url.endswith("/download/enqueue")
    assert body == {"url": "http://spotify/pl/3", "kind": "playlist"}


def test_dispatch_artist_bare_id_prefixed_for_spotifryer(monkeypatch):
    """Bare spotify id from /search/json → prefixed to a URI the route expects."""
    calls = _fake_post_factory(monkeypatch, {"job_ids": [1], "count": 1})
    dispatch_download(
        "spotifryer", "artist", "",
        artist_id="4K6blsYUqx2YH18D8uLw2n", artist_name="Skrillex",
        include_singles=False, prefer_explicit=True,
    )
    url, body = list(calls.items())[0]
    assert url.endswith("/download/discography")
    assert body["artist_id"] == "spotify:artist:4K6blsYUqx2YH18D8uLw2n"
    assert "artist_name" not in body
    assert body["include_singles"] == "false"
    assert body["prefer_explicit"] == "true"


def test_dispatch_artist_uri_passthrough_for_spotifryer(monkeypatch):
    """Already-URI id is passed through untouched."""
    calls = _fake_post_factory(monkeypatch, {"job_ids": [1], "count": 1})
    dispatch_download("spotifryer", "artist", "", artist_id="spotify:artist:abc", artist_name="X")
    url, body = list(calls.items())[0]
    assert body["artist_id"] == "spotify:artist:abc"


def test_dispatch_artist_name_fallback_for_spotifryer(monkeypatch):
    """No artist_id → fall back to artist_name (route resolves server-side)."""
    calls = _fake_post_factory(monkeypatch, {"job_ids": [1], "count": 1})
    dispatch_download("spotifryer", "artist", "", artist_name="X")
    url, body = list(calls.items())[0]
    assert body["artist_name"] == "X"
    assert "artist_id" not in body


def test_dispatch_artist_uses_artist_id_for_others(monkeypatch):
    calls = _fake_post_factory(monkeypatch, {"job_id": 10})
    dispatch_download("qoochie", "artist", "", artist_id="123", artist_name="Some Artist")
    url, body = list(calls.items())[0]
    assert url.endswith("/download/discography")
    assert body["artist_id"] == "123"


def test_dispatch_unknown_kind_errors_without_network(monkeypatch):
    out = dispatch_download("tidalwave", "bogus", "http://x")
    assert out["error"] == "unknown_kind"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))