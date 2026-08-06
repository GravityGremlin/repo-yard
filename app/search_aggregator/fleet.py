"""Fleet adapter — normalize the four tools' surfaces.

Table-driven per-service config handles URL divergence (flat vs nested
action paths, JSON vs HTML list endpoints, priority support, library
browse/recent formats).  Every public function
never raises — network / tool errors return graceful error envelopes.
"""

from __future__ import annotations

import concurrent.futures
import logging
import os
import re

import requests

from app.search_aggregator.aggregator import TOOLS

log = logging.getLogger(__name__)

FLEET_TIMEOUT = float(os.environ.get("REPO_YARD_FLEET_TIMEOUT", "5"))

# Shared session with connection pooling — all fleet HTTP calls reuse it.
_fleet_session = requests.Session()

# ── Status normalisation ─────────────────────────────────────────────────
# Same mapping used in routes.py; duplicated here to avoid circular imports.
_STATUS_MAP: dict[str, str] = {
    "queued": "queued",
    "running": "running",
    "paused": "running",       # active but suspended → closest proxy state
    "completed": "completed",
    "error": "failed",
    "cancelled": "cancelled",
}


def _normalize_status(raw: str) -> str:
    """Map a tool's native status string to the canonical vocabulary."""
    return _STATUS_MAP.get(str(raw).lower(), "queued")


# ── Per-service configuration ────────────────────────────────────────────
# action_base: URL path with {job_id} placeholder, relative to the tool base.
#   spotifryer FLAT:  /download/{job_id}
#   others NESTED:    /download/jobs/{job_id}
# priority: whether the tool exposes priority/up, priority/down endpoints.

_SERVICE_CONFIG: dict[str, dict] = {
    "spotifryer": {
        "list_path": "/download/jobs/data",        # JSON endpoint
        "list_format": "json",
        "action_base": "/download/{job_id}",       # flat
        "priority": True,
    },
    "qoochie": {
        "list_path": "/download/jobs/list",        # HTML partial
        "list_format": "html",
        "action_base": "/download/jobs/{job_id}",  # nested
        "priority": False,
    },
    "tidalwave": {
        "list_path": "/download/jobs/list",        # HTML partial
        "list_format": "html",
        "action_base": "/download/jobs/{job_id}",  # nested
        "priority": True,
    },
    "deeznutz": {
        "list_path": "/download/jobs/list",        # HTML partial
        "list_format": "html",
        "action_base": "/download/jobs/{job_id}",  # nested
        "priority": True,
    },
}

# Canonical action name → URL suffix (relative to action_base).
_ACTION_SUFFIX: dict[str, str] = {
    "cancel": "/cancel",
    "retry": "/retry",
    "pause": "/pause",
    "resume": "/resume",
    "delete": "/delete",
    "priority_up": "/priority/up",
    "priority_down": "/priority/down",
}

# Fleet-wide actions (same URL shape for all tools).
_FLEET_ENDPOINT: dict[str, str] = {
    "purge": "/download/jobs/purge",
    "retry_all": "/download/jobs/retry-all-errored",
}

# Available per-job actions, keyed by normalised status.
_ACTIONS_BY_STATUS: dict[str, list[str]] = {
    "queued": ["priority_up", "priority_down", "cancel", "delete"],
    "running": ["pause", "cancel", "delete"],
    "completed": ["delete"],
    "failed": ["retry", "delete"],
    "cancelled": ["retry", "delete"],
}

_VALID_ACTIONS = frozenset(_ACTION_SUFFIX)
_VALID_FLEET_ACTIONS = frozenset(_FLEET_ENDPOINT)


# ── HTML job-list parsing ────────────────────────────────────────────────

def _parse_jobs_html(html: str) -> list[dict]:
    """Extract job data from an HTMX job-list partial (qoochie/tidalwave/deeznutz).

    Each tool renders rows like::

        <div id="job-XXX" class="job-row-wrapper">
          <div class="job-row job-running" data-job-id="XXX">
            <span class="job-title">…</span>
            <span class="status-badge status-running">running</span>
            <span class="progress-text">42%</span>
            <span class="job-error">…</span>
            <span class="job-files">3 files</span>
          </div>
        </div>

    Returns a list of dicts with keys: id, title, status, progress,
    error, files.  Unparseable rows are silently skipped.
    """
    jobs: list[dict] = []
    # Split on each job-row-wrapper to get per-job blocks.
    # The wrapper has id="job-{id}" — use that to grab the id.
    parts = re.split(
        r'<div\s+id="job-([^"]+)"\s+class="job-row-wrapper">\s*',
        html,
    )
    # parts = [preamble, id1, block1, id2, block2, …]
    for i in range(1, len(parts), 2):
        job_id = parts[i]
        block = parts[i + 1] if i + 1 < len(parts) else ""

        # Title
        m_title = re.search(r'class="job-title"[^>]*>([^<]+)', block)
        title = m_title.group(1).strip() if m_title else job_id

        # Status from status-badge
        m_status = re.search(r'status-badge\s+status-(\w+)', block)
        status = m_status.group(1) if m_status else "queued"

        # Progress
        progress = None
        m_pct = re.search(r'progress-text[^>]*>\s*(\d+(?:\.\d+)?)\s*%', block)
        if m_pct:
            progress = round(float(m_pct.group(1)) / 100.0, 2)

        # Error
        error = None
        m_err = re.search(r'job-error[^>]*>([^<]+)', block)
        if m_err:
            error = m_err.group(1).strip()

        # Files
        files = None
        m_files = re.search(r'job-files[^>]*>\s*(\d+)', block)
        if m_files:
            files = int(m_files.group(1))

        jobs.append({
            "id": job_id,
            "title": title,
            "status": status,
            "progress": progress,
            "error": error,
            "files": files,
        })
    return jobs


# ── Normalisation ────────────────────────────────────────────────────────

def _normalize_job(raw: dict, service: str) -> dict:
    """Convert a raw job dict (from JSON or HTML parse) to the canonical envelope."""
    job_id = str(raw.get("id") or "")
    status_raw = str(raw.get("status") or "")
    status = _normalize_status(status_raw)

    # Title: best-effort from whatever the tool provides.
    title = raw.get("title") or raw.get("name") or job_id
    if not title:
        title = job_id

    # Progress: float 0..1 (spotifryer JSON provides this directly; HTML-parsed
    # jobs already have it normalised to 0..1).
    progress = raw.get("progress")
    if progress is not None:
        try:
            progress = round(float(progress), 2)
        except (TypeError, ValueError):
            progress = None

    # Files: spotifryer JSON gives a list; HTML-parsed gives an int.
    files_raw = raw.get("files")
    if isinstance(files_raw, list):
        files = len(files_raw) if files_raw else None
    elif isinstance(files_raw, (int, float)):
        files = int(files_raw)
    else:
        files = None

    error = raw.get("error") or None
    if error == "":
        error = None

    kind = raw.get("kind") or None
    phase = raw.get("phase") or None
    created = raw.get("created_at") or raw.get("created") or None

    # Compute available actions for this job's state and this service.
    cfg = _SERVICE_CONFIG[service]
    possible = _ACTIONS_BY_STATUS.get(status, [])
    actions = [
        a for a in possible
        if a in _ACTION_SUFFIX
        and (cfg["priority"] or a not in ("priority_up", "priority_down"))
    ]

    return {
        "id": job_id,
        "title": title,
        "status": status,
        "progress": progress,
        "phase": phase,
        "kind": kind,
        "files": files,
        "error": error,
        "created": created,
        "actions": actions,
    }


# ── Public API ───────────────────────────────────────────────────────────

def fetch_jobs(service: str) -> dict:
    """Fetch and normalise all jobs from one service.

    Returns ``{"jobs": [...], "error": None | str}``.
    Never raises on network / tool errors.
    """
    if service not in TOOLS:
        return {"jobs": [], "error": f"unknown service: {service}"}

    cfg = _SERVICE_CONFIG[service]
    url = f"{TOOLS[service]}{cfg['list_path']}"

    try:
        resp = _fleet_session.get(url, timeout=FLEET_TIMEOUT)
        if resp.status_code == 401:
            return {"jobs": [], "error": "auth_expired"}
        resp.raise_for_status()
    except requests.RequestException as exc:
        log.debug("fleet: fetch_jobs failed for %s: %s", service, exc)
        return {"jobs": [], "error": str(exc)}

    try:
        if cfg["list_format"] == "json":
            raw_list = resp.json()
            if not isinstance(raw_list, list):
                raw_list = []
        else:
            raw_list = _parse_jobs_html(resp.text)
    except Exception:
        log.exception("fleet: parse error for %s", service)
        return {"jobs": [], "error": "parse_error"}

    jobs = [_normalize_job(j, service) for j in raw_list]
    return {"jobs": jobs, "error": None}


def fetch_all_jobs() -> dict:
    """Fetch jobs from all four services in parallel.

    Returns ``{"services": {service: {"jobs": [...], "error": ...}, ...}}``.
    """
    result: dict[str, dict] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(TOOLS)) as ex:
        futures = {ex.submit(fetch_jobs, name): name for name in TOOLS}
        for fut in concurrent.futures.as_completed(futures):
            name = futures[fut]
            try:
                result[name] = fut.result()
            except Exception:
                log.exception("fleet: unexpected error fetching %s", name)
                result[name] = {"jobs": [], "error": "internal_error"}
    return {"services": result}


def job_action(service: str, job_id: str, action: str) -> dict:
    """Execute an action on a single job.

    Returns ``{"ok": bool, "status": str | None, "error": None | str}``.
    """
    if service not in TOOLS:
        return {"ok": False, "status": None, "error": f"unknown service: {service}"}
    if action not in _VALID_ACTIONS:
        return {"ok": False, "status": None, "error": f"invalid action: {action}"}

    cfg = _SERVICE_CONFIG[service]
    suffix = _ACTION_SUFFIX[action]
    url = f"{TOOLS[service]}{cfg['action_base'].format(job_id=job_id)}{suffix}"

    try:
        resp = _fleet_session.post(url, timeout=FLEET_TIMEOUT)
        if resp.status_code == 404:
            return {"ok": False, "status": None, "error": "job not found"}
        if resp.status_code >= 400:
            data = resp.json() if resp.content else {}
            return {
                "ok": False,
                "status": None,
                "error": data.get("error") or f"HTTP {resp.status_code}",
            }
        data = resp.json() if resp.content else {}
        return {
            "ok": True,
            "status": data.get("status"),
            "error": None,
        }
    except requests.RequestException as exc:
        log.debug("fleet: job_action %s failed for %s/%s: %s", action, service, job_id, exc)
        return {"ok": False, "status": None, "error": str(exc)}


def fleet_action(service: str, action: str) -> dict:
    """Execute a fleet-wide action (purge or retry_all).

    Returns ``{"ok": bool, "count": int | None, "error": None | str}``.
    """
    if service not in TOOLS:
        return {"ok": False, "count": None, "error": f"unknown service: {service}"}
    if action not in _VALID_FLEET_ACTIONS:
        return {"ok": False, "count": None, "error": f"invalid fleet action: {action}"}

    url = f"{TOOLS[service]}{_FLEET_ENDPOINT[action]}"

    try:
        resp = _fleet_session.post(url, timeout=FLEET_TIMEOUT)
        if resp.status_code >= 400:
            data = resp.json() if resp.content else {}
            return {
                "ok": False,
                "count": None,
                "error": data.get("error") or f"HTTP {resp.status_code}",
            }
        data = resp.json() if resp.content else {}
        count = data.get("deleted") or data.get("retried")
        return {"ok": True, "count": count, "error": None}
    except requests.RequestException as exc:
        log.debug("fleet: fleet_action %s failed for %s: %s", action, service, exc)
        return {"ok": False, "count": None, "error": str(exc)}


# ══════════════════════════════════════════════════════════════════════════
# Library adapter
# ══════════════════════════════════════════════════════════════════════════

# Per-service library configuration.
#   browse_format: "json" for spotifryer, "html" for qoochie/tidalwave/deeznutz
#   recent_format: "json" for spotifryer (returns job dicts), "html" for others
_LIBRARY_CONFIG: dict[str, dict] = {
    "spotifryer": {
        "browse_format": "json",    # GET /library/browse → JSON
        "recent_format": "json",    # GET /library/recent → JSON (job dicts)
    },
    "qoochie": {
        "browse_format": "html",
        "recent_format": "html",
    },
    "tidalwave": {
        "browse_format": "html",
        "recent_format": "html",
    },
    "deeznutz": {
        "browse_format": "html",
        "recent_format": "html",
    },
}


# ── HTML parsing helpers ────────────────────────────────────────────────

def _parse_browse_html(html: str) -> list[dict]:
    """Extract browse items from an HTMX library_items.html partial.

    Each row is a ``<tr class="dir|file">`` with a name link, size, and
    modified date in ``<td>`` cells.
    """
    items: list[dict] = []
    # Split on each table row.
    for m in re.finditer(
        r'<tr\s+class="(dir|file)">\s*(.*?)</tr>',
        html, re.DOTALL,
    ):
        kind = "dir" if m.group(1) == "dir" else "file"
        row = m.group(2)

        # Name from first <a> tag text (strip leading emoji).
        m_name = re.search(r'<a[^>]*>\s*(?:[^\w]*\s*)?(.+?)\s*</a>', row, re.DOTALL)
        name = m_name.group(1).strip() if m_name else ""

        # Size — first <td> that isn't the name cell.
        cells = re.findall(r'<td[^>]*>(.*?)</td>', row, re.DOTALL)
        size = None
        if len(cells) >= 2:
            size_text = re.sub(r'<[^>]+>', '', cells[1]).strip()
            if size_text and size_text != "—":
                size = _human_to_bytes(size_text)

        items.append({"name": name, "path": name, "kind": kind, "size": size})
    return items


def _human_to_bytes(text: str) -> int | None:
    """Best-effort convert a human-readable size string to bytes."""
    text = text.strip().lower()
    multipliers = {"kb": 1024, "mb": 1024 ** 2, "gb": 1024 ** 3, "tb": 1024 ** 4}
    for suffix, mult in multipliers.items():
        if text.endswith(suffix):
            try:
                return int(float(text[: -len(suffix)].strip()) * mult)
            except ValueError:
                return None
    # Plain number → bytes.
    try:
        return int(text)
    except ValueError:
        return None


def _parse_recent_html(html: str) -> list[dict]:
    """Extract recent-file entries from an HTMX library_items.html partial.

    Recent rows use the same ``<tr class="file">`` structure but include
    a ``data-path`` or a serve-link with the relative path.
    """
    items: list[dict] = []
    for m in re.finditer(
        r'<tr\s+class="file">\s*(.*?)</tr>',
        html, re.DOTALL,
    ):
        row = m.group(1)

        # Name from <a> text.
        m_name = re.search(r'<a[^>]*>\s*(?:[^\w]*\s*)?(.+?)\s*</a>', row, re.DOTALL)
        name = m_name.group(1).strip() if m_name else ""

        # Path — the href of the serve link.
        m_path = re.search(r'href="/library/serve/([^"]+)"', row)
        path = _url_unescape(m_path.group(1)) if m_path else name

        # Size from cells.
        cells = re.findall(r'<td[^>]*>(.*?)</td>', row, re.DOTALL)
        size = None
        if len(cells) >= 2:
            size_text = re.sub(r'<[^>]+>', '', cells[1]).strip()
            if size_text and size_text != "—":
                size = _human_to_bytes(size_text)

        items.append({"name": name, "path": path, "kind": "file", "size": size})
    return items


def _url_unescape(s: str) -> str:
    """Minimal URL-decode for path segments (handles %20 etc.)."""
    from urllib.parse import unquote
    return unquote(s)


def _normalize_browse_item(raw: dict, browse_path: str) -> dict:
    """Normalize a spotifryer JSON browse item to the canonical shape."""
    name = raw.get("name", "")
    is_dir = raw.get("is_dir", False)
    size = raw.get("size")
    # Construct relative path.
    if browse_path and browse_path != "/":
        rel = f"{browse_path.rstrip('/')}/{name}"
    else:
        rel = name
    return {
        "name": name,
        "path": rel,
        "kind": "dir" if is_dir else "file",
        "size": size if isinstance(size, (int, float)) else None,
    }


# ── Public library API ──────────────────────────────────────────────────

def fetch_browse(service: str, path: str = "/") -> dict:
    """Browse a tool's music library directory.

    Returns ``{"items": [{"name", "path", "kind", "size"}], "error": ...}``.
    """
    if service not in TOOLS:
        return {"items": [], "error": f"unknown service: {service}"}

    cfg = _LIBRARY_CONFIG[service]
    # Strip leading slash for the path param (tools treat "" as root).
    subpath = path.strip("/")
    url = f"{TOOLS[service]}/library/browse/{subpath}"

    try:
        resp = _fleet_session.get(url, timeout=FLEET_TIMEOUT)
        if resp.status_code == 401:
            return {"items": [], "error": "auth_expired"}
        if resp.status_code == 404:
            return {"items": [], "error": "not found"}
        resp.raise_for_status()
    except requests.RequestException as exc:
        log.debug("fleet: fetch_browse failed for %s: %s", service, exc)
        return {"items": [], "error": str(exc)}

    try:
        if cfg["browse_format"] == "json":
            data = resp.json()
            items = [
                _normalize_browse_item(it, subpath)
                for it in data.get("items", [])
            ]
        else:
            items = _parse_browse_html(resp.text)
    except Exception:
        log.exception("fleet: browse parse error for %s", service)
        return {"items": [], "error": "parse_error"}

    return {"items": items, "error": None}


def fetch_browse_all(path: str = "/") -> dict:
    """Browse all four tools' libraries in parallel."""
    result: dict[str, dict] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(TOOLS)) as ex:
        futures = {ex.submit(fetch_browse, name, path): name for name in TOOLS}
        for fut in concurrent.futures.as_completed(futures):
            name = futures[fut]
            try:
                result[name] = fut.result()
            except Exception:
                log.exception("fleet: unexpected error browsing %s", name)
                result[name] = {"items": [], "error": "internal_error"}
    return {"services": result}


def fetch_recent(service: str) -> dict:
    """Fetch recently added/completed items from a tool's library.

    Returns ``{"items": [...], "error": ...}``.
    """
    if service not in TOOLS:
        return {"items": [], "error": f"unknown service: {service}"}

    cfg = _LIBRARY_CONFIG[service]
    url = f"{TOOLS[service]}/library/recent"

    try:
        resp = _fleet_session.get(url, timeout=FLEET_TIMEOUT)
        if resp.status_code == 401:
            return {"items": [], "error": "auth_expired"}
        resp.raise_for_status()
    except requests.RequestException as exc:
        log.debug("fleet: fetch_recent failed for %s: %s", service, exc)
        return {"items": [], "error": str(exc)}

    try:
        if cfg["recent_format"] == "json":
            # spotifryer returns completed job dicts.
            raw = resp.json()
            items = []
            for job in (raw if isinstance(raw, list) else []):
                files = job.get("files") or []
                items.append({
                    "name": job.get("title") or job.get("id", ""),
                    "path": files[0] if files else "",
                    "kind": "file",
                    "size": None,
                })
        else:
            items = _parse_recent_html(resp.text)
    except Exception:
        log.exception("fleet: recent parse error for %s", service)
        return {"items": [], "error": "parse_error"}

    return {"items": items, "error": None}


def library_serve_url(service: str, path: str) -> str:
    """Return the full URL for streaming an audio file from a tool's library."""
    if service not in TOOLS:
        return ""
    return f"{TOOLS[service]}/library/serve/{path.strip('/')}"


# ══════════════════════════════════════════════════════════════════════════
# Phase 3 — Auth status / credential validation
# ══════════════════════════════════════════════════════════════════════════

# Per-service auth configuration.
#   status_path: GET path returning JSON with auth status fields.
#   validate_method: HTTP method for credential validation (None = not supported).
#   validate_path: POST path for validation endpoint (None = not supported).
#   validate_body: "token" | "arl" — which credential field to send.
_AUTH_CONFIG: dict[str, dict] = {
    "spotifryer": {
        "status_path": "/spotify/status",
        "validate_path": None,          # logout only; no validation
        "validate_body": None,
    },
    "qoochie": {
        "status_path": "/qobuz/status",
        "validate_path": "/qobuz/auth/validate",
        "validate_body": "token",
    },
    "tidalwave": {
        "status_path": "/tidal/status",
        "validate_path": "/tidal/auth/validate",
        "validate_body": "token",
    },
    "deeznutz": {
        "status_path": "/deezer/status",
        "validate_path": "/deezer/auth/validate",
        "validate_body": "arl",
    },
}


def fetch_auth_status(service: str) -> dict:
    """Fetch credential/auth status from a tool.

    Returns::

        {
            "service": str,
            "status": "ok" | "expired" | "missing" | "unknown",
            "label": str,
            "last_updated": str | None,
            "error": None | str,
        }

    Never raises on network errors.
    """
    empty = {
        "service": service,
        "status": "unknown",
        "label": "",
        "last_updated": None,
        "error": None,
    }
    if service not in TOOLS:
        return {**empty, "error": f"unknown service: {service}"}

    cfg = _AUTH_CONFIG[service]
    url = f"{TOOLS[service]}{cfg['status_path']}"

    try:
        resp = _fleet_session.get(url, timeout=FLEET_TIMEOUT)
        if resp.status_code == 401:
            return {**empty, "status": "expired", "error": "auth expired"}
        resp.raise_for_status()
    except requests.RequestException as exc:
        log.debug("fleet: fetch_auth_status failed for %s: %s", service, exc)
        return {**empty, "error": str(exc)}

    try:
        data = resp.json()
    except Exception:
        log.exception("fleet: auth status parse error for %s", service)
        return {**empty, "error": "parse_error"}

    # Normalise the tool's response to our canonical schema.
    status_raw = str(data.get("status") or "").lower()
    if status_raw in ("ok", "valid", "active"):
        status = "ok"
    elif status_raw in ("expired", "invalid"):
        status = "expired"
    elif status_raw in ("missing", "none", ""):
        status = "missing"
    else:
        status = "unknown"

    return {
        "service": service,
        "status": status,
        "label": str(data.get("label") or ""),
        "last_updated": data.get("last_updated") or data.get("updated_at") or None,
        "error": data.get("error") or None,
    }


def validate_credential(
    service: str,
    token: str | None = None,
    arl: str | None = None,
) -> dict:
    """Validate a credential against a tool's auth endpoint.

    Returns ``{"ok": bool, "error": None | str}``.
    Never raises on network errors.
    """
    if service not in TOOLS:
        return {"ok": False, "error": f"unknown service: {service}"}

    cfg = _AUTH_CONFIG[service]
    if cfg["validate_path"] is None:
        return {"ok": False, "error": f"{service} does not support credential validation"}

    url = f"{TOOLS[service]}{cfg['validate_path']}"

    # Build the body based on the tool's expected field.
    body: dict = {}
    if cfg["validate_body"] == "token" and token is not None:
        body["token"] = token
    elif cfg["validate_body"] == "arl" and arl is not None:
        body["arl"] = arl
    else:
        return {"ok": False, "error": f"missing credential for {cfg['validate_body']} field"}

    try:
        resp = _fleet_session.post(url, json=body, timeout=FLEET_TIMEOUT)
        if resp.status_code == 401:
            return {"ok": False, "error": "invalid credential"}
        if resp.status_code >= 400:
            data = resp.json() if resp.content else {}
            return {"ok": False, "error": data.get("error") or f"HTTP {resp.status_code}"}
        data = resp.json() if resp.content else {}
        ok = data.get("ok", True)
        return {"ok": bool(ok), "error": data.get("error")}
    except requests.RequestException as exc:
        log.debug("fleet: validate_credential failed for %s: %s", service, exc)
        return {"ok": False, "error": str(exc)}


# ══════════════════════════════════════════════════════════════════════════
# Phase 3 — Stats proxy
# ══════════════════════════════════════════════════════════════════════════

# Per-service stats configuration.
#   stats_format: "json" for spotifryer, "html" for qoochie/tidalwave/deeznutz.
_STATS_CONFIG: dict[str, dict] = {
    "spotifryer": {"stats_format": "json"},
    "qoochie":    {"stats_format": "html"},
    "tidalwave":  {"stats_format": "html"},
    "deeznutz":   {"stats_format": "html"},
}

_STATS_FIELDS = (
    "downloads_today",
    "files_managed",
    "jobs_active",
    "jobs_queued",
    "library_size_bytes",
)


def _parse_stats_html(html: str) -> dict:
    """Best-effort extraction of stats from an HTMX stats partial.

    Tools render stat cards like::

        <div class="stat-card">
          <span class="stat-value">5</span>
          <span class="stat-label">Downloads Today</span>
        </div>

    Or with data attributes::

        <div class="stat-card" data-field="downloads_today">
          <span class="stat-value">5</span>
        </div>
    """
    result: dict[str, int] = {}

    # Strategy 1: data-field attributes (more reliable).
    for m in re.finditer(
        r'data-field="(\w+)"[^>]*>.*?stat-value[^>]*>\s*([^<]+)',
        html, re.DOTALL,
    ):
        field = m.group(1)
        if field in _STATS_FIELDS:
            val_clean = m.group(2).strip().replace(",", "")
            if field == "library_size_bytes":
                result[field] = _human_to_bytes(val_clean) or 0
            else:
                try:
                    result[field] = int(val_clean)
                except ValueError:
                    pass

    # Strategy 2: label-based matching (fallback).
    if len(result) < 2:
        label_map = {
            "downloads today": "downloads_today",
            "files managed": "files_managed",
            "files in library": "files_managed",
            "active jobs": "jobs_active",
            "running jobs": "jobs_active",
            "queued jobs": "jobs_queued",
            "pending jobs": "jobs_queued",
            "library size": "library_size_bytes",
        }
        cards = re.findall(
            r'stat-card[^>]*>.*?stat-value[^>]*>\s*([^<]+).*?stat-label[^>]*>\s*([^<]+)',
            html, re.DOTALL,
        )
        for value_text, label_text in cards:
            label_lower = label_text.strip().lower()
            for needle, field in label_map.items():
                if needle in label_lower and field not in result:
                    val_clean = value_text.strip().replace(",", "")
                    # Handle human-readable sizes for library_size_bytes.
                    if field == "library_size_bytes":
                        result[field] = _human_to_bytes(val_clean) or 0
                    else:
                        try:
                            result[field] = int(val_clean)
                        except ValueError:
                            pass
                    break

    return result


def fetch_stats(service: str) -> dict:
    """Fetch download/library stats from a tool.

    Returns::

        {
            "service": str,
            "downloads_today": int,
            "files_managed": int,
            "jobs_active": int,
            "jobs_queued": int,
            "library_size_bytes": int,
            "error": None | str,
        }

    Never raises on network errors.
    """
    base = {
        "service": service,
        "downloads_today": 0,
        "files_managed": 0,
        "jobs_active": 0,
        "jobs_queued": 0,
        "library_size_bytes": 0,
        "error": None,
    }
    if service not in TOOLS:
        return {**base, "error": f"unknown service: {service}"}

    url = f"{TOOLS[service]}/stats/partial"

    try:
        resp = _fleet_session.get(url, timeout=FLEET_TIMEOUT)
        if resp.status_code == 401:
            return {**base, "error": "auth_expired"}
        resp.raise_for_status()
    except requests.RequestException as exc:
        log.debug("fleet: fetch_stats failed for %s: %s", service, exc)
        return {**base, "error": str(exc)}

    try:
        if _STATS_CONFIG[service]["stats_format"] == "json":
            data = resp.json()
            for field in _STATS_FIELDS:
                if field in data:
                    try:
                        base[field] = int(data[field])
                    except (TypeError, ValueError):
                        pass
        else:
            parsed = _parse_stats_html(resp.text)
            for field in _STATS_FIELDS:
                if field in parsed:
                    base[field] = parsed[field]
    except Exception:
        log.exception("fleet: stats parse error for %s", service)
        return {**base, "error": "parse_error"}

    return base


def fetch_all_stats() -> dict:
    """Fetch stats from all four services in parallel.

    Returns ``{"services": {name: stats_dict, ...}}``.
    """
    result: dict[str, dict] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(TOOLS)) as ex:
        futures = {ex.submit(fetch_stats, name): name for name in TOOLS}
        for fut in concurrent.futures.as_completed(futures):
            name = futures[fut]
            try:
                result[name] = fut.result()
            except Exception:
                log.exception("fleet: unexpected error fetching stats for %s", name)
                result[name] = {
                    "service": name,
                    "downloads_today": 0,
                    "files_managed": 0,
                    "jobs_active": 0,
                    "jobs_queued": 0,
                    "library_size_bytes": 0,
                    "error": "internal_error",
                }
    return {"services": result}


# ══════════════════════════════════════════════════════════════════════════
# Phase 3 — Audit proxy
# ══════════════════════════════════════════════════════════════════════════

_AUDIT_PAGE_SIZE = 50  # default fetch size for paginated audit lists


def _parse_audit_html(html: str) -> list[dict]:
    """Extract audit entries from an HTMX audit-list partial.

    Each tool renders rows like::

        <div class="audit-entry" data-job-id="abc">
          <span class="audit-time">2026-01-01 12:00</span>
          <span class="audit-action">download</span>
          <span class="audit-detail">Cool Track by Artist</span>
        </div>

    Or with table rows::

        <tr class="audit-row" data-job-id="abc">
          <td class="audit-time">2026-01-01 12:00</td>
          <td class="audit-action">download</td>
          <td class="audit-detail">Cool Track by Artist</td>
        </tr>
    """
    entries: list[dict] = []

    # Try div-based layout first.
    blocks = re.finditer(
        r'<div\s+class="audit-entry"[^>]*>(.*?)</div>',
        html, re.DOTALL,
    )
    for m in blocks:
        block = m.group(0)
        # data-job-id attribute on the div.
        m_job = re.search(r'data-job-id="([^"]*)"', block)
        job_id = m_job.group(1) if m_job else None

        m_time = re.search(r'audit-time[^>]*>([^<]+)', block)
        m_action = re.search(r'audit-action[^>]*>([^<]+)', block)
        m_detail = re.search(r'audit-detail[^>]*>([^<]+)', block)

        entries.append({
            "time": m_time.group(1).strip() if m_time else "",
            "action": m_action.group(1).strip() if m_action else "",
            "detail": m_detail.group(1).strip() if m_detail else "",
            "job_id": job_id,
        })

    # Fallback: table-row layout.
    if not entries:
        rows = re.finditer(
            r'<tr\s+class="audit-row"[^>]*>(.*?)</tr>',
            html, re.DOTALL,
        )
        for m in rows:
            block = m.group(0)
            m_job = re.search(r'data-job-id="([^"]*)"', block)
            job_id = m_job.group(1) if m_job else None

            m_time = re.search(r'audit-time[^>]*>([^<]+)', block)
            m_action = re.search(r'audit-action[^>]*>([^<]+)', block)
            m_detail = re.search(r'audit-detail[^>]*>([^<]+)', block)

            entries.append({
                "time": m_time.group(1).strip() if m_time else "",
                "action": m_action.group(1).strip() if m_action else "",
                "detail": m_detail.group(1).strip() if m_detail else "",
                "job_id": job_id,
            })

    return entries


def fetch_audit(service: str, offset: int = 0) -> dict:
    """Fetch audit log entries from a tool.

    All four tools expose ``GET /audit/list`` returning an HTMX partial
    with paginated entries (``?offset=N``).

    Returns::

        {
            "entries": [{"time", "action", "detail", "job_id"}],
            "error": None | str,
        }

    Never raises on network errors.
    """
    if service not in TOOLS:
        return {"entries": [], "error": f"unknown service: {service}"}

    url = f"{TOOLS[service]}/audit/list?offset={offset}"

    try:
        resp = _fleet_session.get(url, timeout=FLEET_TIMEOUT)
        if resp.status_code == 401:
            return {"entries": [], "error": "auth_expired"}
        resp.raise_for_status()
    except requests.RequestException as exc:
        log.debug("fleet: fetch_audit failed for %s: %s", service, exc)
        return {"entries": [], "error": str(exc)}

    try:
        entries = _parse_audit_html(resp.text)
    except Exception:
        log.exception("fleet: audit parse error for %s", service)
        return {"entries": [], "error": "parse_error"}

    # Annotate each entry with the source service.
    for e in entries:
        e["service"] = service

    return {"entries": entries, "error": None}


def fetch_all_audit(offset: int = 0) -> dict:
    """Fetch audit entries from all four services in parallel.

    Returns ``{"entries": [...], "error": None}``.
    All entries are tagged with their source service.
    """
    all_entries: list[dict] = []
    first_error: str | None = None

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(TOOLS)) as ex:
        futures = {ex.submit(fetch_audit, name, offset): name for name in TOOLS}
        for fut in concurrent.futures.as_completed(futures):
            try:
                data = fut.result()
                all_entries.extend(data["entries"])
                if data["error"] and first_error is None:
                    first_error = data["error"]
            except Exception:
                name = futures[fut]
                log.exception("fleet: unexpected error fetching audit for %s", name)
                if first_error is None:
                    first_error = "internal_error"

    # Sort by time descending (newest first).
    all_entries.sort(key=lambda e: e.get("time", ""), reverse=True)
    return {"entries": all_entries, "error": first_error}


# ══════════════════════════════════════════════════════════════════════════
# Phase 3 — Lyrics proxy (spotifryer only)
# ══════════════════════════════════════════════════════════════════════════

def fetch_lyrics(service: str, track_id: str) -> dict:
    """Fetch lyrics for a track from a tool.

    Only spotifryer supports this via ``GET /track/<id>/lyrics``.

    Returns ``{"lyrics": str, "error": None | str}``.
    Never raises on network errors.
    """
    if service not in TOOLS:
        return {"lyrics": "", "error": f"unknown service: {service}"}
    if service != "spotifryer":
        return {"lyrics": "", "error": f"{service} does not support lyrics"}

    url = f"{TOOLS[service]}/track/{track_id}/lyrics"

    try:
        resp = _fleet_session.get(url, timeout=FLEET_TIMEOUT)
        if resp.status_code == 404:
            return {"lyrics": "", "error": "lyrics not found"}
        resp.raise_for_status()
    except requests.RequestException as exc:
        log.debug("fleet: fetch_lyrics failed for %s/%s: %s", service, track_id, exc)
        return {"lyrics": "", "error": str(exc)}

    try:
        data = resp.json()
        lyrics = data.get("lyrics") or data.get("text") or ""
        return {"lyrics": str(lyrics), "error": None}
    except Exception:
        # If the response is plain text, use it directly.
        return {"lyrics": resp.text, "error": None}
