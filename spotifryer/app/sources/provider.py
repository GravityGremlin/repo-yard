"""Abstract base class and data structures for audio download providers."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Callable


@dataclass
class Track:
    """Spotify track metadata passed to download providers."""
    title: str
    artist: str
    album: str = ""
    isrc: Optional[str] = None
    cover_url: Optional[str] = None
    duration_ms: Optional[int] = None
    track_number: Optional[int] = None
    spotify_id: str = ""


class DownloadCancelled(Exception):
    """Raised when a download is cancelled via the cancel signal."""
    pass


class AudioProvider(ABC):
    """Interface every download backend must implement."""

    name: str
    quality_label: str  # e.g. "FLAC", "m4a"

    @abstractmethod
    def search(self, track: Track) -> Optional[str]:
        """Search for a track.

        Returns a provider-specific ID/URL, or None if not found.
        """
        ...

    @abstractmethod
    def download(
        self,
        track: Track,
        resource_id: str,
        output_dir: Path,
        progress_cb: Optional[Callable[[int, int], None]] = None,
        cancel_signal: Optional[Callable[[], bool]] = None,
    ) -> Optional[Path]:
        """Download a track.

        Returns path to downloaded file, or None on failure.
        """
        ...
