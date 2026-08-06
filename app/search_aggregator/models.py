"""Canonical result models for the unified search aggregator."""

from __future__ import annotations

from dataclasses import dataclass, field, asdict


@dataclass
class SearchResult:
    """Single result normalized to the canonical schema.

    All tools return this shape from their /search/json endpoint.
    """
    provider: str                     # "spotifryer" | "qoochie" | "tidalwave" | "deeznutz"
    type: str                        # "track" | "album" | "artist" | "playlist"
    id: str                          # provider-native id
    title: str
    artist: str
    album: str = ""
    cover_url: str | None = None
    duration_ms: int | None = None
    isrc: str | None = None
    url: str = ""
    year: int | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class AggregatedResponse:
    """Final response sent to the UI."""
    query: str
    results: list[SearchResult] = field(default_factory=list)
    statuses: dict[str, str] = field(default_factory=dict)
    # status per tool: "ok" | "auth_expired" | "provider_error" | "unavailable"

    def to_dict(self) -> dict:
        return {
            "query": self.query,
            "results": [r.to_dict() for r in self.results],
            "statuses": self.statuses,
        }
