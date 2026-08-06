"""HTTP surface for the unified search aggregator."""

from __future__ import annotations

import json
import logging
import re

import requests
from flask import Blueprint, Response, jsonify, render_template, request

from app.search_aggregator.aggregator import TOOLS, _search_session, resolve_playlist_url, search_all
from app.search_aggregator.download import dispatch_download
from app.search_aggregator.fleet import (
    FLEET_TIMEOUT,
    _VALID_ACTIONS,
    _VALID_FLEET_ACTIONS,
    fetch_all_audit,
    fetch_all_jobs,
    fetch_all_stats,
    fetch_audit,
    fetch_auth_status,
    fetch_browse_all,
    fetch_lyrics,
    fleet_action,
    job_action,
    library_serve_url,
    validate_credential,
)
from app.search_aggregator.models import AggregatedResponse
from app.search_aggregator.url_detect import detect_url

log = logging.getLogger(__name__)

yard_bp = Blueprint("yard", __name__)

# Search priority (product decision): discography → playlists → albums → tracks.
TYPE_TABS = [
    ("artist", "Discography"),
    ("playlist", "Playlists"),
    ("album", "Albums"),
    ("track", "Tracks"),
]
VALID_TYPES = {key for key, _label in TYPE_TABS}


def _active_type() -> str:
    t = (request.args.get("type") or "artist").strip().lower()
    return t if t in VALID_TYPES else "artist"


def _tool_statuses(default: str = "ok") -> dict[str, str]:
    return {name: default for name in TOOLS}


@yard_bp.get("/")
def index():
    return render_template("index.html", tools=TOOLS, active_type=_active_type(), type_tabs=TYPE_TABS)


def _playlist_context(q: str):
    """Resolve a playlist URL; returns (playlist_payload, statuses) or (None, None) if not a playlist URL."""
    det = detect_url(q)
    if det is None or det.kind != "playlist":
        return None, None
    try:
        payload = resolve_playlist_url(q)
    except Exception:
        payload = {"provider": det.service, "url": det.url, "error": "provider_error",
                   "playlist": None, "tracks": []}
    statuses = _tool_statuses()
    statuses[det.service] = "ok" if payload.get("error") in (None, "invalid_url") else payload["error"]
    return payload, statuses


@yard_bp.get("/search")
def search():
    """HTMX partial — the unified results panel."""
    q = (request.args.get("q") or "").strip()
    active = _active_type()

    if not q:
        return render_template(
            "_results.html",
            tools=TOOLS, active_type=active,
            agg=AggregatedResponse(query="", statuses=_tool_statuses()),
        )

    playlist_payload, statuses = _playlist_context(q)
    if playlist_payload is not None:
        return render_template(
            "_playlist.html",
            tools=TOOLS, active_type=active,
            payload=playlist_payload, statuses=statuses,
        )

    agg = search_all(q, active)
    return render_template("_results.html", tools=TOOLS, active_type=active, agg=agg)


@yard_bp.get("/search/json")
def search_json():
    """JSON API — same aggregation, machine-readable."""
    q = (request.args.get("q") or "").strip()
    active = _active_type()

    if not q:
        return jsonify(AggregatedResponse(query="", statuses=_tool_statuses()).to_dict())

    playlist_payload, _statuses = _playlist_context(q)
    if playlist_payload is not None:
        return jsonify(playlist_payload)

    return jsonify(search_all(q, active).to_dict())


@yard_bp.get("/status")
def status():
    """Configured tools + their reachability (quick health view)."""
    return jsonify({"tools": TOOLS})


def _bool_arg(v) -> bool | None:
    """'true'/'1'/'on' → True; 'false'/'0' → False; anything else → None."""
    if v is None:
        return None
    s = str(v).strip().lower()
    if s in ("1", "true", "yes", "on"):
        return True
    if s in ("0", "false", "no", "off"):
        return False
    return None


@yard_bp.post("/download")
def download():
    """Forward a repo/download action to the owning tool.

    Form fields (HTMX buttons on result cards):
      service  - tool name (spotifryer|qoochie|tidalwave|deeznutz)
      kind     - track | album | playlist | artist
      url      - canonical content URL (track/album/playlist)
      artist_id / artist_name - for kind=artist
      include_singles / prefer_explicit / override_existing - discography options
    Returns the tool's JSON envelope; if the request is HTMX, renders a
    small inline status chip instead.
    """
    service = (request.form.get("service") or "").strip().lower()
    kind = (request.form.get("kind") or "").strip().lower()
    if service not in TOOLS or kind not in ("track", "album", "playlist", "artist"):
        return jsonify({"error": "invalid_request"}), 400

    if kind == "artist" and not (request.form.get("artist_id") or request.form.get("artist_name")):
        return jsonify({"error": "artist_id or artist_name required"}), 400

    # "no singles" checkbox: checked → exclude singles (include_singles=False);
    # unchecked → include singles by default. prefer_explicit is pre-checked in
    # the UI (default ON); an unchecked box is absent from the form, so absent
    # must mean explicitly False — never fall through to a tool's own default.
    no_singles = request.form.get("no_singles")
    include_singles = "false" if _bool_arg(no_singles) else "true"
    prefer_explicit = request.form.get("prefer_explicit")
    if prefer_explicit is None:
        prefer_explicit = "false"  # unchecked → explicitly off

    payload = dispatch_download(
        service,
        kind,
        url=(request.form.get("url") or "").strip(),
        artist_id=(request.form.get("artist_id") or "").strip(),
        artist_name=(request.form.get("artist_name") or "").strip(),
        include_singles=_bool_arg(include_singles),
        prefer_explicit=_bool_arg(prefer_explicit),
        override_existing=_bool_arg(request.form.get("override_existing")),
    )

    if request.headers.get("HX-Request"):
        error = payload.get("error")
        job_id = payload.get("job_id")
        job_ids = payload.get("job_ids")
        queued = payload.get("queued") or payload.get("count")
        if job_id:
            msg, ok = f"queued job {job_id}", True
            return (
                f'<span class="dl-chip ok"'
                f' data-service="{service}" data-job="{job_id}">'
                f'{msg}</span>'
            )
        elif job_ids:
            msg, ok = f"queued {len(job_ids)} jobs", True
        elif queued:
            msg, ok = f"queued {queued} jobs", True
        elif error == "auth_expired":
            msg, ok = "auth expired — re-auth in the tool", False
        elif error == "unavailable":
            msg, ok = f"{service} unreachable", False
        else:
            msg, ok = f"{service}: {error or 'failed'}", False
        return f'<span class="dl-chip {"ok" if ok else "err"}">{msg}</span>'

    return jsonify(payload)


# ── Job status proxy ────────────────────────────────────────────────────────

_VALID_SERVICES = frozenset(TOOLS)
_STATUS_TIMEOUT = float(__import__("os").environ.get("REPO_YARD_STATUS_TIMEOUT", "3"))

# Normalisation: tool vocab → proxy vocab.
# All four tools use: queued, running, paused, completed, error, cancelled.
# The proxy exposes: queued, running, completed, failed, cancelled.
_STATUS_MAP = {
    "queued": "queued",
    "running": "running",
    "paused": "running",       # active but suspended → closest proxy state
    "completed": "completed",
    "error": "failed",
    "cancelled": "cancelled",
}


def _normalize_status(raw: str) -> str:
    """Map a tool's native status string to the proxy's canonical vocab."""
    return _STATUS_MAP.get(str(raw).lower(), "queued")


def _sse_read_last_event(url: str, timeout: float = _STATUS_TIMEOUT) -> dict | None:
    """Open an SSE stream, read data events until timeout, return the last one.

    For terminal jobs on tidalwave/deeznutz the server sends a single status
    event and closes, so this returns almost instantly.  For running jobs we
    grab the latest event within *timeout* seconds then disconnect.  On any
    transport error we return None (caller maps to a graceful error).
    """
    try:
        resp = _search_session.get(url, stream=True, timeout=timeout)
        resp.raise_for_status()
    except requests.RequestException:
        return None

    last_event: dict | None = None
    try:
        for raw_line in resp.iter_lines(decode_unicode=True):
            if raw_line is None:
                break
            if not raw_line.startswith("data: "):
                continue
            payload = raw_line[len("data: "):]
            try:
                evt = json.loads(payload)
            except (json.JSONDecodeError, ValueError):
                continue
            last_event = evt
            # Terminal event → no need to read further.
            status = str(evt.get("status", "")).lower()
            if status in ("completed", "error", "cancelled", "failed"):
                break
    except Exception:
        # Non-transport read errors (malformed stream, connection drop mid-read):
        # return whatever we parsed so far — the caller maps a missing/partial
        # event to a graceful error envelope, so don't crash the request.
        log.debug("job-status proxy: stream read error for %s", url, exc_info=True)
    finally:
        resp.close()
    return last_event


def _parse_html_status(html: str) -> dict | None:
    """Best-effort extraction of job status from an HTML partial.

    Tools render a row like:
      <span class="status-badge status-queued">queued</span>
      <span class="progress-text">42%</span>
      <span class="job-error">…</span>
      <span class="job-files">3 files</span>
    """
    # status badge text
    m = re.search(r'status-badge\s+status-(\w+)', html)
    if not m:
        return None
    status = m.group(1)

    progress = None
    m_pct = re.search(r'progress-text[^>]*>\s*(\d+(?:\.\d+)?)\s*%', html)
    if m_pct:
        progress = round(float(m_pct.group(1)) / 100.0, 2)

    error = None
    m_err = re.search(r'job-error[^>]*>([^<]+)<', html)
    if m_err:
        error = m_err.group(1).strip()

    files = None
    m_files = re.search(r'job-files[^>]*>\s*(\d+)\s*files?', html)
    if m_files:
        files = int(m_files.group(1))

    return {"status": status, "progress": progress, "error": error, "files": files}


def _fetch_spotifryer_status(job_id: str) -> dict:
    """spotifryer has a clean JSON status endpoint: GET /download/<id>/status."""
    base = TOOLS["spotifryer"]
    resp = _search_session.get(
        f"{base}/download/{job_id}/status",
        timeout=_STATUS_TIMEOUT,
    )
    if resp.status_code == 404:
        return {"status": "failed", "error": "job not found"}
    resp.raise_for_status()
    data = resp.json()
    files = data.get("files")
    return {
        "status": _normalize_status(data.get("status", "")),
        "progress": data.get("progress"),
        "phase": data.get("phase"),
        "files": len(files) if isinstance(files, list) else files,
        "error": data.get("error") or None,
    }


def _fetch_sse_tool_status(service: str, job_id: str) -> dict:
    """Fetch status for qoochie/tidalwave/deeznutz.

    Primary path: read the SSE stream for up to *timeout* seconds and return
    the last status event.  If the stream yields nothing (terminal job on
    qoochie, which lacks a terminal fast-path), fall back to parsing the HTML
    partial from GET /download/jobs/<id>.
    """
    base = TOOLS[service]

    # ── primary: SSE stream ────────────────────────────────────────
    event = _sse_read_last_event(f"{base}/download/events/{job_id}")
    if event is not None:
        raw_status = event.get("status") or event.get("type", "")
        # SSE error events carry {"type":"error","error":"…"} without a status key.
        if event.get("type") == "error" and not event.get("status"):
            return {
                "status": "failed",
                "progress": event.get("progress"),
                "phase": event.get("phase"),
                "files": event.get("files"),
                "error": event.get("error") or "download error",
            }
        return {
            "status": _normalize_status(raw_status),
            "progress": event.get("progress"),
            "phase": event.get("phase"),
            "files": event.get("files"),
            "error": event.get("error") or None,
        }

    # ── fallback: parse HTML job-row partial ───────────────────────
    try:
        resp = _search_session.get(
                f"{base}/download/jobs/{job_id}",
                timeout=_STATUS_TIMEOUT,
            )
        if resp.status_code == 404:
            return {"status": "failed", "error": "job not found"}
        resp.raise_for_status()
        parsed = _parse_html_status(resp.text)
        if parsed:
            return {
                "status": _normalize_status(parsed["status"]),
                "progress": parsed["progress"],
                "phase": None,
                "files": parsed["files"],
                "error": parsed["error"],
            }
    except requests.RequestException:
        log.debug("job-status proxy: HTML fallback fetch failed for %s/%s", service, job_id)

    return {"status": "failed", "error": "unreachable"}


@yard_bp.get("/download/<service>/<job_id>")
def job_status(service: str, job_id: str):
    """Proxy a single job's status from the owning tool.

    Returns normalized JSON:
      {"service", "job_id", "status", "progress", "phase", "files", "error"}

    Status is one of: queued | running | completed | failed | cancelled.
    Never raises on downstream errors — returns HTTP 200 with a graceful
    error envelope so the polling UI stops cleanly.
    """
    if service not in _VALID_SERVICES:
        return jsonify({"error": f"unknown service: {service}"}), 400

    try:
        if service == "spotifryer":
            normalized = _fetch_spotifryer_status(job_id)
        else:
            normalized = _fetch_sse_tool_status(service, job_id)
    except requests.Timeout:
        log.warning("job-status proxy: timeout fetching %s/%s", service, job_id)
        normalized = {"status": "failed", "error": "timeout"}
    except requests.ConnectionError:
        log.warning("job-status proxy: connection error for %s/%s", service, job_id)
        normalized = {"status": "failed", "error": "unreachable"}
    except Exception:
        log.exception("job-status proxy: unexpected error for %s/%s", service, job_id)
        normalized = {"status": "failed", "error": "unreachable"}

    return jsonify({
        "service": service,
        "job_id": job_id,
        "status": normalized.get("status", "failed"),
        "progress": normalized.get("progress"),
        "phase": normalized.get("phase"),
        "files": normalized.get("files"),
        "error": normalized.get("error"),
    })


# ══════════════════════════════════════════════════════════════════════════
# Phase 3 — Auth, Stats, Audit pages (server-rendered for the HTMX UI)
# ══════════════════════════════════════════════════════════════════════════


@yard_bp.get("/auth")
def auth_page():
    """Auth page — status for all tools + validate forms (designer template)."""
    results = {}
    for svc in TOOLS:
        results[svc] = fetch_auth_status(svc)
    return render_template("auth.html", services=results)


@yard_bp.get("/auth/partial")
def auth_partial():
    """HTMX partial — re-fetch auth status and render status cards."""
    results = {}
    for svc in TOOLS:
        results[svc] = fetch_auth_status(svc)
    return render_template("_auth_status.html", services=results)


@yard_bp.get("/stats")
def stats_page():
    """Stats page — download/library stats for all tools (designer template)."""
    return render_template("stats.html", services=fetch_all_stats().get("services", {}))


@yard_bp.get("/stats/partial")
def stats_partial():
    """HTMX partial — re-fetch stats and render stat cards."""
    return render_template("_stats_cards.html", services=fetch_all_stats().get("services", {}))


@yard_bp.get("/audit")
def audit_page():
    """Audit page — merged audit log across all tools (designer template)."""
    payload = fetch_all_audit(0)
    return render_template(
        "audit.html",
        entries=payload.get("entries", []),
        failures=payload.get("failures", {}),
    )


@yard_bp.get("/audit/partial")
def audit_partial():
    """HTMX partial — re-fetch audit entries and render the list."""
    payload = fetch_all_audit(0)
    return render_template(
        "_audit_list.html",
        entries=payload.get("entries", []),
        failures=payload.get("failures", {}),
    )


# ── Job control plane ────────────────────────────────────────────────────

_VALID_FLEET_SERVICES = frozenset(TOOLS)


@yard_bp.get("/jobs")
def jobs_page():
    """Full jobs page — all services, auto-refresh via HTMX."""
    payload = fetch_all_jobs()
    return render_template("jobs.html", services=payload["services"])


@yard_bp.get("/jobs/list")
def jobs_list():
    """HTMX partial — re-fetch all jobs and render the list fragment."""
    payload = fetch_all_jobs()
    return render_template("_jobs_list.html", services=payload["services"])


@yard_bp.post("/jobs/<service>/<job_id>/<action>")
def job_action_route(service: str, job_id: str, action: str):
    """Execute a per-job action (cancel, retry, pause, resume, delete,
    priority_up, priority_down).

    Returns JSON ``{"ok": ...}`` or, for HTMX requests, an inline chip.
    """
    if service not in _VALID_FLEET_SERVICES:
        return jsonify({"error": f"unknown service: {service}"}), 400
    if action not in _VALID_ACTIONS:
        return jsonify({"error": f"invalid action: {action}"}), 400

    result = job_action(service, job_id, action)

    if request.headers.get("HX-Request"):
        if result["ok"]:
            return (
                f'<span class="dl-chip ok"'
                f' data-service="{service}" data-job="{job_id}">'
                f'{action} sent</span>'
            )
        return (
            f'<span class="dl-chip err"'
            f' data-service="{service}" data-job="{job_id}">'
            f'{result["error"] or action + " failed"}</span>'
        )

    return jsonify(result)


@yard_bp.post("/jobs/<service>/<action>")
def fleet_action_route(service: str, action: str):
    """Fleet-wide action (purge, retry_all) on a single service."""
    if service not in _VALID_FLEET_SERVICES:
        return jsonify({"error": f"unknown service: {service}"}), 400
    if action not in _VALID_FLEET_ACTIONS:
        return jsonify({"error": f"invalid fleet action: {action}"}), 400

    result = fleet_action(service, action)

    if request.headers.get("HX-Request"):
        if result["ok"]:
            count = result.get("count") or 0
            return (
                f'<span class="dl-chip ok"'
                f' data-service="{service}">'
                f'{action}: {count} affected</span>'
            )
        return (
            f'<span class="dl-chip err"'
            f' data-service="{service}">'
            f'{result["error"] or action + " failed"}</span>'
        )

    return jsonify(result)


# ── Library browse ───────────────────────────────────────────────────────

@yard_bp.get("/library")
def library_page():
    """Full library page — tabs load content via HTMX."""
    return render_template("library.html", services=TOOLS)


@yard_bp.get("/library/browse")
def library_browse():
    """HTMX partial — unified directory listing across all services."""
    path = request.args.get("path", "/")
    payload = fetch_browse_all(path)
    return render_template("_library_browse.html", services=payload["services"], path=path)


@yard_bp.get("/library/serve/<service>/<path:filepath>")
def library_serve(service: str, filepath: str):
    """Stream-passthrough: proxy an audio file from the owning tool.

    Fetches the tool's ``/library/serve/<path>`` endpoint and streams the
    bytes back to the client with the original Content-Type.
    """
    if service not in _VALID_FLEET_SERVICES:
        return jsonify({"error": f"unknown service: {service}"}), 404

    tool_url = library_serve_url(service, filepath)
    try:
        upstream = _search_session.get(tool_url, timeout=FLEET_TIMEOUT, stream=True)
        if upstream.status_code == 404:
            return jsonify({"error": "file not found"}), 404
        if upstream.status_code == 403:
            return jsonify({"error": "forbidden"}), 403
        upstream.raise_for_status()
    except requests.ConnectionError:
        return jsonify({"error": f"{service} unreachable"}), 502
    except requests.Timeout:
        return jsonify({"error": f"{service} timeout"}), 504
    except requests.RequestException as exc:
        log.warning("library_serve: upstream error for %s/%s: %s", service, filepath, exc)
        return jsonify({"error": str(exc)}), 502

    def _stream():
        try:
            for chunk in upstream.iter_content(chunk_size=65536):
                if chunk:
                    yield chunk
        finally:
            upstream.close()

    content_type = upstream.headers.get("Content-Type", "application/octet-stream")
    resp = Response(
        _stream(),
        content_type=content_type,
        status=upstream.status_code,
    )
    # Forward useful headers from the tool.
    for hdr in ("Content-Length", "Accept-Ranges", "Content-Disposition"):
        val = upstream.headers.get(hdr)
        if val:
            resp.headers[hdr] = val
    return resp


# ══════════════════════════════════════════════════════════════════════════
# Phase 3 — Auth status + credential validation
# ══════════════════════════════════════════════════════════════════════════


@yard_bp.get("/auth/status")
def auth_status_all():
    """Fetch auth status for all four tools in parallel."""
    results = {}
    for svc in TOOLS:
        results[svc] = fetch_auth_status(svc)
    return jsonify(results)


@yard_bp.get("/auth/status/<service>")
def auth_status_one(service: str):
    """Fetch auth status for a single tool."""
    if service not in _VALID_FLEET_SERVICES:
        return jsonify({"error": f"unknown service: {service}"}), 400
    return jsonify(fetch_auth_status(service))


@yard_bp.post("/auth/validate")
def auth_validate():
    """Validate a credential against a tool's auth endpoint.

    Form fields:
      service  - tool name
      token    - auth token (qoochie/tidalwave)
      arl      - ARL token (deeznutz)
    """
    service = (request.form.get("service") or request.args.get("service") or "").strip().lower()
    if service not in _VALID_FLEET_SERVICES:
        return jsonify({"error": f"unknown service: {service}"}), 400

    token = request.form.get("token") or request.args.get("token")
    arl = request.form.get("arl") or request.args.get("arl")
    result = validate_credential(service, token=token, arl=arl)
    return jsonify(result)


# ══════════════════════════════════════════════════════════════════════════
# Phase 3 — Stats proxy
# ══════════════════════════════════════════════════════════════════════════


@yard_bp.get("/stats/json")
def stats_all():
    """Fetch download/library stats for all four tools in parallel."""
    payload = fetch_all_stats()
    return jsonify(payload)


@yard_bp.get("/stats/<service>")
def stats_one(service: str):
    """Fetch stats for a single tool."""
    if service not in _VALID_FLEET_SERVICES:
        return jsonify({"error": f"unknown service: {service}"}), 400
    return jsonify(fetch_all_stats()["services"].get(service, {
        "service": service,
        "downloads_today": 0,
        "files_managed": 0,
        "jobs_active": 0,
        "jobs_queued": 0,
        "library_size_bytes": 0,
        "error": "not fetched",
    }))


# ══════════════════════════════════════════════════════════════════════════
# Phase 3 — Audit proxy
# ══════════════════════════════════════════════════════════════════════════


@yard_bp.get("/audit/json")
def audit_all():
    """Fetch audit log entries from all four tools in parallel."""
    offset = request.args.get("offset", 0, type=int)
    return jsonify(fetch_all_audit(offset))


@yard_bp.get("/audit/<service>")
def audit_one(service: str):
    """Fetch audit log entries from a single tool."""
    if service not in _VALID_FLEET_SERVICES:
        return jsonify({"error": f"unknown service: {service}"}), 400
    offset = request.args.get("offset", 0, type=int)
    return jsonify(fetch_audit(service, offset=offset))


# ══════════════════════════════════════════════════════════════════════════
# Phase 3 — Lyrics proxy (spotifryer only)
# ══════════════════════════════════════════════════════════════════════════


@yard_bp.get("/lyrics/<service>/<track_id>")
def lyrics_one(service: str, track_id: str):
    """Fetch lyrics for a track from a tool (spotifryer only)."""
    if service not in _VALID_FLEET_SERVICES:
        return jsonify({"error": f"unknown service: {service}"}), 400
    return jsonify(fetch_lyrics(service, track_id))
