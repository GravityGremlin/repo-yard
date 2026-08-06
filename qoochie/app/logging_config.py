"""Structured logging setup."""

import logging
import sys

_REDACTED = "[REDACTED]"


class _TokenScrubFilter(logging.Filter):
    """Scrub QOBUZ_TOKEN (and QOBUZ_APP_SECRET) from log output.

    The token/secret are long env-var strings that could leak via exception
    messages (e.g. requests.HTTPError repr including request headers) or
    debug logging.  This filter blanks them in the rendered message.
    """

    _secrets: list[str] = []

    def __init__(self) -> None:
        super().__init__()
        self._refresh()

    def _refresh(self) -> None:
        """Collect non-empty secrets from env at init time."""
        import os
        secrets = []
        for var in ("QOBUZ_TOKEN", "QOBUZ_APP_SECRET"):
            val = os.environ.get(var, "").strip()
            if val and len(val) > 4:
                secrets.append(val)
        self._secrets = secrets

    def filter(self, record: logging.LogRecord) -> bool:
        if self._secrets and isinstance(record.msg, str):
            for secret in self._secrets:
                if secret in record.msg:
                    record.msg = record.msg.replace(secret, _REDACTED)
        # Also scrub any positional args that might contain secrets
        if self._secrets and record.args:
            if isinstance(record.args, tuple):
                scrubbed = []
                changed = False
                for arg in record.args:
                    s = str(arg)
                    if any(secret in s for secret in self._secrets):
                        for secret in self._secrets:
                            s = s.replace(secret, _REDACTED)
                        scrubbed.append(s)
                        changed = True
                    else:
                        scrubbed.append(arg)
                if changed:
                    record.args = tuple(scrubbed)
            elif isinstance(record.args, dict):
                scrubbed = {}
                changed = False
                for k, v in record.args.items():
                    s = str(v)
                    if any(secret in s for secret in self._secrets):
                        for secret in self._secrets:
                            s = s.replace(secret, _REDACTED)
                        scrubbed[k] = s
                        changed = True
                    else:
                        scrubbed[k] = v
                if changed:
                    record.args = scrubbed
        return True


def setup_logging() -> None:
    """Configure structured logging for the app."""
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    root = logging.getLogger()
    if not root.handlers:
        root.addHandler(handler)
        root.addFilter(_TokenScrubFilter())
    root.setLevel(logging.INFO)
    # Quiet noisy libs
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
