"""Minimal stubs of tidalapi types.
Only attributes that TidalDownloader and the session modules actually read.
Do NOT import the real ``tidalapi`` in test modules — use these stubs.
"""
from __future__ import annotations
import time
from dataclasses import dataclass

# ── Data types ──────────────────────────────────────────────────

@dataclass
class Artist:
    id: str
    name: str
    
    def get_albums(self, limit=None, offset=0):
        """Return a list of Album stubs. Defaults to empty; set in test."""
        return []


@dataclass
class Album:
    id: str
    name: str = ""
    artist: Artist | None = None
    year: int | None = None
    num_tracks: int = 0
    image: str = ""

    def tracks(self) -> list[Track]:
        return []


@dataclass
class Track:
    id: str
    name: str = ""
    artist: Artist | None = None
    album: Album | None = None
    track_num: int | None = 0
    duration: int = 0

    def get_stream(self) -> Stream:
        return Stream()


@dataclass
class Playlist:
    id: str
    name: str = ""

    def tracks(self) -> list[Track]:
        return []


@dataclass
class StreamManifest:
    codecs: str = "FLAC"

    def get_urls(self) -> list[str]:
        return ["http://cdn.example.com/track.flac"]


@dataclass
class Stream:
    def get_stream_manifest(self) -> StreamManifest:
        return StreamManifest()


# ── Session ─────────────────────────────────────────────────────

class Config:
    quality: str = "HIGH"


class LoginLink:
    user_code: str = "TEST-CODE"
    device_code: str = "TEST-CODE"
    verification_uri_complete: str = "https://link.tidal.com/TEST-CODE"
    expires_in: int = 300


class Future:
    """Simulates tidalapi's OAuth future — call it to get the session."""
    _session: Session | None

    def __init__(self, session: Session | None = None) -> None:
        self._session = session

    def __call__(self) -> Session:
        return self._session or Session()


class Session:
    """Minimal tidalapi.Session stub used by TidalDownloader and session mgmt."""

    config: Config
    access_token: str | None
    refresh_token: str | None
    expiry_time: object | None
    token_type: str
    request_session: object | None

    def __init__(self) -> None:
        self.config = Config()
        self.access_token = None
        self.refresh_token = None
        self.expiry_time = None
        self.token_type = "Bearer"
        self.request_session = None

    # ── Methods read by app/tidal/session.py ──

    def check_login(self) -> bool:
        return True

    def load_oauth_session(
        self,
        token_type: str = "Bearer",
        access_token: str = "",
        refresh_token: str | None = None,
        expiry_time: object | None = None,
    ) -> None:
        self.token_type = token_type
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.expiry_time = expiry_time

    def token_refresh(self, refresh_token: str) -> bool:
        self.refresh_token = refresh_token
        self.access_token = f"refreshed-{refresh_token[:8]}-{int(time.time()) % 10000}"
        return True

    def login_oauth(self) -> tuple[LoginLink, Future]:
        return LoginLink(), Future(self)

    # ── Lookup methods read by TidalDownloader ──

    def track(self, id: str) -> Track:
        return Track(id=id, name=f"Track {id}")

    def album(self, id: str) -> Album:
        return Album(id=id, name=f"Album {id}")

    def playlist(self, id: str) -> Playlist:
        return Playlist(id=id, name=f"Playlist {id}")

    def artist(self, artist_id):
        """Return an Artist stub with a matching ID."""
        return Artist(id=str(artist_id), name="Test Artist")