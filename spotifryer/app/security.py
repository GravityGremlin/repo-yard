"""Security helpers — path traversal prevention."""

from __future__ import annotations

from pathlib import Path


def safe_resolve(base: Path, subpath: str) -> Path | None:
    """Resolve subpath against base, preventing path traversal.

    Returns the resolved path if it's within base, or None if it escapes.
    """
    if not subpath:
        return base.resolve()
    # Normalize and resolve
    resolved = (base / subpath).resolve()
    base_resolved = base.resolve()
    # Check the resolved path is within base
    try:
        resolved.relative_to(base_resolved)
    except ValueError:
        return None
    return resolved
