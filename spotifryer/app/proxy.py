"""Proxy support — per-job proxy selection and injection into requests."""

from __future__ import annotations

import logging
from urllib.parse import urlsplit

import requests

from app.config import PROXY_LIST

logger = logging.getLogger(__name__)


def _redact(url: str) -> str:
    """Strip credentials from a proxy URL for safe logging."""
    if not url:
        return ""
    try:
        parts = urlsplit(url)
        return f"{parts.scheme}://{parts.hostname}:{parts.port}" if parts.port \
            else f"{parts.scheme}://{parts.hostname}"
    except Exception:
        logger.warning("Proxy connect failed, returning <invalid>")
        return "<invalid>"


def apply_proxy_to_requests_session(req_session: requests.Session, proxy_url: str) -> None:
    """Configure a requests.Session to route traffic through the given proxy."""
    if proxy_url:
        req_session.proxies.update({"http": proxy_url, "https": proxy_url})
        logger.debug("Applied proxy to requests session: %s", _redact(proxy_url))


def get_proxy_url(index: int | None = None) -> str:
    """Get proxy URL by index. Index 0 = no proxy."""
    if index is None or index < 0 or index >= len(PROXY_LIST):
        return ""
    return PROXY_LIST[index]


def make_proxied_session(proxy_url: str) -> requests.Session:
    """Create a requests.Session with proxy configured.

    If proxy_url is empty, returns a plain session (direct connection).
    """
    session = requests.Session()
    if proxy_url:
        proxies = {"http": proxy_url, "https": proxy_url}
        session.proxies.update(proxies)
        logger.debug("Proxy session created: %s", _redact(proxy_url))
    return session
