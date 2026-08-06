"""Download dispatch — route a repo action to the owning tool.

A result card (playlist / album / track / artist-discography) carries its
provider tool, a canonical URL, and — for the artist case — the artist name.
The aggregator re-POSTs to the owning tool's download endpoint so a single
auth/queue is used and the browser never needs cross-origin CSRF tokens.

Per-tool contract (from live recon of the four tools):
  * enqueue         POST /download/enqueue — one path for track/album/playlist
                    spotifryer: {"url","kind":track|album|playlist}
                    qoochie, tidalwave, deeznutz: {"url","type":track|album|playlist}
                    (all four workers resolve those URL kinds via download_url)
  * discography     POST /download/discography
                    spotifryer: {"artist_name"} resolved to a URI server-side
                    qoochie, tidalwave, deeznutz: {"artist_id"} (provider id)
                    plus {"include_singles","prefer_explicit","override_existing"}

The tools CSRF-exempt these download paths server-to-server.
"""

from __future__ import annotations

import logging
import os

import requests

from app.search_aggregator.aggregator import TOOLS, _search_session

log = logging.getLogger(__name__)

# Downloads (esp. discography) enqueue many jobs server-side and can exceed
# the fast search timeout — give them their own, longer window.
DOWNLOAD_TIMEOUT = float(os.environ.get("REPO_YARD_DOWNLOAD_TIMEOUT", "30"))

# kind values the two URL-param tool families expect on /download/enqueue.
_ALBUM_TRACK = ("album", "track")


def _enqueue_payload(service: str, url: str, kind: str) -> dict:
    """Build the /download/enqueue body for a track/album/playlist by URL."""
    key = "kind" if service == "spotifryer" else "type"
    return {"url": url, key: kind}


def dispatch_download(
    service: str,
    kind: str,
    url: str = "",
    *,
    artist_id: str = "",
    artist_name: str = "",
    include_singles: bool | None = None,
    prefer_explicit: bool | None = None,
    override_existing: bool | None = None,
    timeout: float | None = None,
) -> dict:
    """Route a download to the owning tool. Returns the tool's JSON envelope
    ({job_id...} or {error...}) augmented with provider/kind/url for the UI.

    Never raises on transport failure — returns an {error: "unavailable"}
    envelope so a single failing tool never breaks the request.
    """
    base = TOOLS.get(service)
    if not base:
        return {"provider": service, "kind": kind, "url": url, "error": "unknown_service"}

    endpoint: str
    body: dict
    try:
        if kind in ("album", "track", "playlist"):
            # All four tools' workers resolve album/track/playlist URLs directly
            # via download_url, so one enqueue path covers every URL kind.
            endpoint = "/download/enqueue"
            body = _enqueue_payload(service, url, kind)
        elif kind == "artist":
            endpoint = "/download/discography"
            if service == "spotifryer":
                if artist_id and ":" not in artist_id:
                    # /search/json returns a bare spotify id; the discography
                    # route expects a URI. Prefer the exact id from the search
                    # result over a name re-search (ambiguous names can hit the
                    # wrong artist).
                    body = {"artist_id": f"spotify:artist:{artist_id}"}
                else:
                    body = {"artist_id": artist_id} if artist_id else {"artist_name": artist_name}
            else:
                # tidal/deezer/qoochie resolve by provider artist_id.
                body = {"artist_id": artist_id or url}
            if include_singles is not None:
                body["include_singles"] = "true" if include_singles else "false"
            if prefer_explicit is not None:
                body["prefer_explicit"] = "true" if prefer_explicit else "false"
            if override_existing is not None:
                body["override_existing"] = "true" if override_existing else "false"
        else:
            return {"provider": service, "kind": kind, "url": url, "error": "unknown_kind"}
    except Exception as exc:  # pragma: no cover - defensive
        log.exception("repo-yard: dispatch_download routing failed")
        return {"provider": service, "kind": kind, "url": url, "error": "routing_error", "detail": str(exc)}

    try:
        resp = _search_session.post(
            f"{base}{endpoint}",
            json=body,
            params={"format": "json"},
            timeout=timeout or DOWNLOAD_TIMEOUT,
        )
    except requests.Timeout:
        return {"provider": service, "kind": kind, "url": url, "error": "unavailable"}
    except requests.RequestException:
        return {"provider": service, "kind": kind, "url": url, "error": "unavailable"}
    except Exception:  # pragma: no cover - defensive
        log.exception("repo-yard: unexpected error dispatching download")
        return {"provider": service, "kind": kind, "url": url, "error": "unavailable"}

    # Read the tool's body first, THEN check status — the tools return
    # {error: auth_expired|...} JSON on 4xx/5xx and the UI maps it to a
    # specific chip. raise_for_status() before json() would swallow that.
    if not resp.ok:
        try:
            tool_error = resp.json()
        except ValueError:
            tool_error = {"error": f"HTTP {resp.status_code}"}
        tool_error.setdefault("provider", service)
        tool_error.setdefault("kind", kind)
        tool_error.setdefault("url", url)
        return tool_error

    out = resp.json() if resp.content else {}
    out.setdefault("provider", service)
    out.setdefault("kind", kind)
    out.setdefault("url", url)
    return out