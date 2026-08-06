"""Unit tests for the search aggregator's normalization and dedup logic.

These use stubbed tool responses — no network, no live fleet. Run with:
    python -m pytest tests/ -q
    # or, without pytest installed:
    python tests/test_aggregator.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.search_aggregator.normalizer import deduplicate, normalize_tool_results
from app.search_aggregator.models import SearchResult
from app.search_aggregator import aggregator


# ── search_all (parallel aggregation) ───────────────────────────────────────

def _fake_fetch_factory(monkeypatch, payloads: dict, raises: dict | None = None):
    """Stub aggregator.fetch_tool per tool. `raises` maps tool → Exception class."""
    raises = raises or {}

    def _fake_fetch(tool, query, rtype, timeout):
        exc = raises.get(tool)
        if exc is not None:
            raise exc
        return payloads[tool]

    monkeypatch.setattr(aggregator, "fetch_tool", _fake_fetch)


def test_search_all_ok_dedups_across_tools(monkeypatch):
    _fake_fetch_factory(monkeypatch, {
        "tidalwave": {"results": [
            {"type": "track", "id": "1", "title": "Bangarang", "artist": "Skrillex", "isrc": "USX1"},
        ]},
        "qoochie": {"results": [
            # same ISRC → dropped by dedup
            {"type": "track", "id": "2", "title": "Bangarang", "artist": "Skrillex", "isrc": "USX1"},
            {"type": "track", "id": "3", "title": "Other", "artist": "X"},
        ]},
        "spotifryer": {"results": []},
        "deeznutz": {"results": []},
    })
    agg = aggregator.search_all("bangarang")
    assert [r.id for r in agg.results] == ["1", "3"]
    assert agg.statuses == {"tidalwave": "ok", "qoochie": "ok",
                            "spotifryer": "ok", "deeznutz": "ok"}


def test_search_all_timeout_marks_unavailable(monkeypatch):
    import requests
    _fake_fetch_factory(monkeypatch, {
        "tidalwave": {"results": [{"type": "track", "id": "1", "title": "T", "artist": "A"}]},
        "qoochie": {"results": [{"type": "track", "id": "2", "title": "T", "artist": "A"}]},
        "spotifryer": {"results": []},
        "deeznutz": {"results": []},
    }, raises={"spotifryer": requests.Timeout})
    agg = aggregator.search_all("t")
    assert agg.statuses["spotifryer"] == "unavailable"
    assert agg.statuses["tidalwave"] == "ok"


def test_search_all_auth_expired_and_provider_error(monkeypatch):
    import requests
    _fake_fetch_factory(monkeypatch, {
        "tidalwave": {"results": []},
        "qoochie": {"error": "auth_expired"},
        "spotifryer": {"error": "rate_limited"},   # non-auth error
        "deeznutz": {"results": []},
    }, raises={"deeznutz": requests.ConnectionError})
    agg = aggregator.search_all("t")
    assert agg.statuses["qoochie"] == "auth_expired"
    assert agg.statuses["spotifryer"] == "provider_error"
    assert agg.statuses["deeznutz"] == "unavailable"


def test_search_all_unexpected_exception_is_provider_error(monkeypatch):
    _fake_fetch_factory(monkeypatch, {
        "tidalwave": {"results": []},
        "qoochie": {"results": []},
        "spotifryer": {"results": []},
        "deeznutz": {"results": []},
    }, raises={"deeznutz": ValueError})
    agg = aggregator.search_all("t")
    assert agg.statuses["deeznutz"] == "provider_error"
    assert agg.statuses["tidalwave"] == "ok"


def test_search_all_respects_max_results_per_tool(monkeypatch):
    many = {"results": [{"type": "track", "id": str(i), "title": f"T{i}", "artist": "A"}
                        for i in range(25)]}
    _fake_fetch_factory(monkeypatch, {
        "tidalwave": many,
        "qoochie": {"results": []},
        "spotifryer": {"results": []},
        "deeznutz": {"results": []},
    })
    monkeypatch.setattr(aggregator, "MAX_RESULTS_PER_TOOL", 10)
    agg = aggregator.search_all("t")
    assert len(agg.results) == 10


def test_search_all_zero_cap_disables_limiting(monkeypatch):
    many = {"results": [{"type": "track", "id": str(i), "title": f"T{i}", "artist": "A"}
                        for i in range(25)]}
    _fake_fetch_factory(monkeypatch, {
        "tidalwave": many,
        "qoochie": {"results": []},
        "spotifryer": {"results": []},
        "deeznutz": {"results": []},
    })
    monkeypatch.setattr(aggregator, "MAX_RESULTS_PER_TOOL", 0)
    agg = aggregator.search_all("t")
    assert len(agg.results) == 25


def test_search_all_empty_query_no_fetch(monkeypatch):
    """search_all never fetches for an empty query — returns empty envelope."""
    agg = aggregator.search_all("")
    assert agg.results == []
    assert set(agg.statuses) == set(aggregator.TOOLS)



def test_normalize_maps_canonical_fields():
    raw = [
        {"type": "track", "id": "42", "title": "T", "artist": "A", "album": "Al",
         "cover_url": "http://img", "duration_ms": "214000", "isrc": "XX1", "year": "2010"},
        {"type": "track", "id": "43", "title": "NoISRC", "artist": "B"},  # sparse
        {"not": "a dict"},  # junk row
    ]
    out = normalize_tool_results("qoochie", raw)
    assert len(out) == 2
    r = out[0]
    assert r.provider == "qoochie" and r.isrc == "XX1"
    assert r.duration_ms == 214000 and r.year == 2010


def test_dedup_by_isrc():
    a = SearchResult(provider="tidalwave", type="track", id="1", title="Bangarang", artist="Skrillex", isrc="USX1")
    b = SearchResult(provider="qoochie", type="track", id="2", title="Bangarang", artist="Skrillex", isrc="USX1")
    c = SearchResult(provider="deeznutz", type="track", id="3", title="Other", artist="X", isrc=None)
    out = deduplicate([a, b, c])
    assert [r.id for r in out] == ["1", "3"]  # b dropped (same ISRC as a), c kept


def test_dedup_fallback_without_isrc():
    a = SearchResult(provider="tidalwave", type="track", id="1", title="Song", artist="Art")
    b = SearchResult(provider="spotifryer", type="track", id="2", title="Song", artist="Art")
    c = SearchResult(provider="qoochie", type="track", id="3", title="Song", artist="Different")
    out = deduplicate([a, b, c])
    assert [r.id for r in out] == ["1", "3"]


def test_dedup_keeps_distinct_isrcs():
    a = SearchResult(provider="tidalwave", type="track", id="1", title="A", artist="X", isrc="I1")
    b = SearchResult(provider="qoochie", type="track", id="2", title="B", artist="Y", isrc="I2")
    assert len(deduplicate([a, b])) == 2


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
    print("all aggregator tests passed")
