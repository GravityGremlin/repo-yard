"""Shared Navidrome helpers — e.g. triggering a library scan."""

from __future__ import annotations

import logging

import requests

from app.config import NAVIDROME_URL, NAVIDROME_AUTO_SCAN

logger = logging.getLogger(__name__)


def trigger_navidrome_scan() -> bool:
    """Trigger a quick Navidrome library scan (non-critical, fire-and-forget).

    Returns True if the scan was triggered successfully, False otherwise.
    Respects the ``NAVIDROME_AUTO_SCAN`` config flag: returns False immediately
    when scanning is disabled.
    """
    if not NAVIDROME_AUTO_SCAN:
        return False
    try:
        resp = requests.post(
            f"{NAVIDROME_URL}/api/scan?full=false", timeout=5
        )
        if resp.ok:
            logger.info("Navidrome scan triggered")
            return True
        logger.warning("Navidrome scan returned %d", resp.status_code)
        return False
    except Exception as exc:
        logger.warning("Navidrome scan failed (non-critical): %s", exc)
        return False
