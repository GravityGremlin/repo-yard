"""Parallel cross-tool search — the heart of repo-yard's search aggregation.

Fetches each tool's /search/json endpoint concurrently, normalizes the
results, deduplicates by ISRC, and reports per-tool status. A failing or
unavailable tool never blocks the others (riptide lesson: health awareness).
"""

from __future__ import annotations

import concurrent.futures
import logging
import os

import requests

from app.search_aggregator.models import AggregatedResponse
from app.search_aggregator.normalizer import deduplicate, normalize_tool_results

log = logging.getLogger(__name__)

# Shared session — four tool endpoints hit from the UI every search request.
# Connection pooling prunes per-call TCP/handshake latency to each tool.
_search_session = requests.Session()

# Tool registry: name -> base URL of the tool's /search/json endpoint.
# Defaults target the fleet on cthulhu (10.8.0.10); override per tool with
# REPO_YARD_<TOOL>_URL=http://host:port  (e.g. for local dev on :5000-:5003).
TOOLS: dict[str, str] = {
    "spotifryer": os.environ.get("REPO_YARD_SPOTIFRYER_URL", "http://10.8.0.10:19293"),
    "qoochie": os.environ.get("REPO_YARD_QOOCHIE_URL", "http://10.8.0.10:19295"),
    "tidalwave": os.environ.get("REPO_YARD_TIDALWAVE_URL", "http://10.8.0.10:19290"),
    "deeznutz": os.environ.get("REPO_YARD_DEEZNUTZ_URL", "http://10.8.0.10:19294"),
}

SEARCH_TIMEOUT = float(os.environ.get("REPO_YARD_SEARCH_TIMEOUT", "5"))
# Cap normalized results per tool so a chatty provider can't flood the UI or
# the dedup set. 0 disables the cap.
MAX_RESULTS_PER_TOOL = int(os.environ.get("REPO_YARD_MAX_RESULTS_PER_TOOL", "10"))

# Dedup tie-break order: when two tools return the same ISRC (or same
# title+artist), the earliest provider in this list keeps its copy. Timber
# of preference: FLAC-capable services first. Responses arrive in
# as_completed order (threaded), so without this the dedup winner — and
# therefore the UI order — is nondeterministic run-to-run.
_PROVIDER_PRIORITY = ("tidalwave", "qoochie", "spotifryer", "deeznutz")


def fetch_tool(tool: str, query: str, rtype: str, timeout: float) -> dict:
    """Query one tool's /search/json endpoint. Raises on network/HTTP errors."""
    resp = _search_session.get(
        f"{TOOLS[tool]}/search/json",
        params={"q": query, "type": rtype},
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json()


def search_all(query: str, rtype: str = "track", timeout: float | None = None) -> AggregatedResponse:
    """Search every tool in parallel; never raise on individual tool failure.

    Returns an AggregatedResponse with normalized+deduplicated results and a
    status per tool: "ok" | "auth_expired" | "provider_error" | "unavailable".
    """
    timeout = timeout or SEARCH_TIMEOUT
    statuses: dict[str, str] = {}
    raw: list[tuple[str, list[dict]]] = []

    if not query.strip():
        # Guard: an empty query would still fan out to every tool otherwise.
        # The routes layer already short-circuits, but keep the core defensive.
        return AggregatedResponse(query=query, statuses={name: "ok" for name in TOOLS})

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(TOOLS)) as ex:
        futures = {ex.submit(fetch_tool, name, query, rtype, timeout): name for name in TOOLS}
        for fut in concurrent.futures.as_completed(futures):
            name = futures[fut]
            try:
                payload = fut.result()
            except requests.Timeout:
                statuses[name] = "unavailable"
                continue
            except requests.RequestException:
                statuses[name] = "unavailable"
                continue
            except Exception:
                log.exception("repo-yard: unexpected error searching %s", name)
                statuses[name] = "provider_error"
                continue

            error = payload.get("error")
            if error:
                statuses[name] = "auth_expired" if error == "auth_expired" else "provider_error"
                continue
            statuses[name] = "ok"
            items = payload.get("results", []) or []
            if MAX_RESULTS_PER_TOOL > 0:
                items = items[:MAX_RESULTS_PER_TOOL]
            raw.append((name, items))

    # Stabilize provider order before dedup so ISRC ties always resolve to
    # the same winner regardless of which thread finished first.
    order = {name: i for i, name in enumerate(_PROVIDER_PRIORITY)}
    raw.sort(key=lambda pair: order.get(pair[0], len(_PROVIDER_PRIORITY)))

    results: list = []
    for name, items in raw:
        results.extend(normalize_tool_results(name, items))
    return AggregatedResponse(query=query, results=deduplicate(results), statuses=statuses)


def resolve_playlist_url(raw_url: str, timeout: float | None = None) -> dict:
    """Resolve a playlist URL against the owning tool's /playlist/resolve.

    Returns the tool's canonical envelope (provider, url, error, playlist,
    tracks). Raises on network/HTTP errors; the caller decides how to render
    an unavailable tool.
    """
    from app.search_aggregator.url_detect import detect_url

    det = detect_url(raw_url)
    if det is None or det.kind != "playlist":
        return {"provider": None, "url": raw_url, "error": "invalid_url",
                "playlist": None, "tracks": []}
    base = TOOLS.get(det.service)
    if not base:
        return {"provider": det.service, "url": det.url, "error": "provider_error",
                "playlist": None, "tracks": []}
    resp = _search_session.post(
        f"{base}/playlist/resolve",
        json={"url": det.url},
        params={"format": "json"},
        timeout=timeout or SEARCH_TIMEOUT,
    )
    resp.raise_for_status()
    payload = resp.json()
    payload.setdefault("provider", det.service)
    payload.setdefault("url", det.url)
    payload.setdefault("playlist", None)
    payload.setdefault("tracks", [])
    return payload
