"""Result normalization and ISRC-based dedup for the search aggregator."""

from __future__ import annotations

from app.search_aggregator.models import SearchResult


def normalize_tool_results(provider: str, raw_results: list[dict]) -> list[SearchResult]:
    """Convert a tool's raw JSON result list into canonical SearchResult objects.

    Tools may include extra fields or omit optional ones — we extract only the
    canonical fields and coerce types defensively.
    """
    out: list[SearchResult] = []
    for item in raw_results:
        if not isinstance(item, dict):
            continue
        try:
            sr = SearchResult(
                provider=provider,
                type=str(item.get("type") or "track"),
                id=str(item.get("id") or ""),
                title=str(item.get("title") or ""),
                artist=str(item.get("artist") or ""),
                album=str(item.get("album") or ""),
                cover_url=_none_on_falsy(item.get("cover_url")),
                duration_ms=_int_or_none(item.get("duration_ms")),
                isrc=_none_on_falsy(item.get("isrc")),
                url=str(item.get("url") or ""),
                year=_int_or_none(item.get("year")),
            )
            if sr.id and sr.title:
                out.append(sr)
        except (TypeError, ValueError):
            continue
    return out


def deduplicate(results: list[SearchResult]) -> list[SearchResult]:
    """Deduplicate across providers, preferring ISRC when present.

    - Items with a non-null ISRC: keep the first occurrence per ISRC.
    - Items without ISRC: fall back to exact (lowercased title, artist) match.
    Order of the input list is preserved.
    """
    seen_isrc: set[str] = set()
    seen_nokey: set[tuple[str, str]] = set()
    out: list[SearchResult] = []
    for r in results:
        if r.isrc:
            if r.isrc in seen_isrc:
                continue
            seen_isrc.add(r.isrc)
        else:
            key = (r.title.lower().strip(), r.artist.lower().strip())
            if key in seen_nokey:
                continue
            seen_nokey.add(key)
        out.append(r)
    return out


def _none_on_falsy(v):
    return None if not v else str(v)


def _int_or_none(v):
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None
