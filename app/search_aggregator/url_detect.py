"""Service URL detection for the unified search aggregator.

When a user pastes a playlist (or other content) URL into the search box,
detect which of the four services it belongs to so we can route the request
to the right tool instead of keyword-searching for a UUID.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# service name -> (host patterns, content-type path patterns)
# Hosts are matched case-insensitively against the URL's netloc.
_SERVICES = {
    "tidalwave": {
        "hosts": ("tidal.com", "listen.tidal.com"),
        "kinds": ("playlist", "album", "track", "artist"),
    },
    "spotifryer": {
        "hosts": ("open.spotify.com", "spotify.com"),
        "kinds": ("playlist", "album", "track", "artist"),
    },
    "qoochie": {
        "hosts": ("qobuz.com", "play.qobuz.com", "www.qobuz.com"),
        "kinds": ("playlist", "album", "track", "artist"),
    },
    "deeznutz": {
        "hosts": ("deezer.com", "www.deezer.com"),
        "kinds": ("playlist", "album", "track", "artist"),
    },
}


@dataclass(frozen=True)
class DetectedUrl:
    service: str          # tool name
    kind: str             # playlist | album | track | artist
    id: str               # provider-native id
    url: str              # normalized url (https, no query/fragment)


# Hosts can be bare ("tidal.com") or subdomained ("www.qobuz.com", "open.spotify.com").
def _netloc_matches(host: str, patterns: tuple[str, ...]) -> bool:
    h = host.lower().rstrip(".")
    for p in patterns:
        if h == p or h.endswith("." + p):
            return True
    return False


_URL_RE = re.compile(
    # Optional scheme, optional www, host, optional locale segment (en-us, fr…),
    # then kind/id. Trailing query/fragment/trailing-slash tolerated.
    r"^(?:https?://)?(?:www\.)?(?P<host>[a-z0-9.-]+)"
    r"(?:/(?:[a-z]{2}(?:-[a-z]{2})?))?"
    r"/(?P<kind>playlist|album|track|artist)/(?P<id>[A-Za-z0-9_-]+)(?:[/?#]|$)",
    re.IGNORECASE,
)


def detect_url(raw: str) -> DetectedUrl | None:
    """Detect a service URL in the input. Returns None for plain text.

    Accepts with/without scheme, optional www., ignores query strings and
    trailing slashes. Only returns a result when the host belongs to one of
    the four services.
    """
    text = (raw or "").strip()
    if not text or " " in text:
        return None  # a bare URL has no spaces; anything else is a query

    m = _URL_RE.match(text)
    if not m:
        return None

    host, kind, cid = m.group("host"), m.group("kind").lower(), m.group("id")
    for service, cfg in _SERVICES.items():
        if _netloc_matches(host, cfg["hosts"]):
            if kind not in cfg["kinds"]:
                return None
            # normalize: https://<host>/<kind>/<id>
            return DetectedUrl(
                service=service,
                kind=kind,
                id=cid,
                url=f"https://{host}/{kind}/{cid}",
            )
    return None
