"""Native Tidal download engine — zero subprocess, pure tidalapi + requests.

Uses tidalapi's track.get_stream() -> StreamManifest -> direct HTTPS URLs,
downloads with requests streaming, reports progress via callback, and can be
cancelled mid-transfer via a cancel-check callback.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Callable

import requests
import tidalapi

from app.config import DOWNLOAD_DIR

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[int, int], None]
CancelCheck = Callable[[], bool]

_TRACK_RE = re.compile(r"tidal\.com/track/(\d+)", re.IGNORECASE)
_ALBUM_RE = re.compile(r"tidal\.com/album/(\d+)", re.IGNORECASE)
_PLAYLIST_RE = re.compile(r"tidal\.com/playlist/([a-f0-9-]+)", re.IGNORECASE)

_CODEC_EXTS = {
    "FLAC": ".flac",
    "MP4A": ".m4a",
    "MP3": ".mp3",
    "AAC": ".m4a",
    "EC3": ".ec3",
}


class DownloadCancelled(Exception):
    """Raised when a cancel-check reports the job was cancelled."""


def _cancelled(check: CancelCheck | None) -> bool:
    return check is not None and check()


class TidalDownloader:
    """Native tidalapi download engine — no subprocess, no CLI tools."""

    def __init__(self, session: tidalapi.Session, download_dir: Path | None = None,
                 http_session: requests.Session | None = None):
        self.session = session
        self.download_dir = download_dir or DOWNLOAD_DIR
        self._http = http_session or requests.Session()

    # -- Public API -----------------------------------------------

    def download_url(self, url: str, output_dir: Path | None = None,
                     callback: ProgressCallback | None = None,
                     cancel_check: CancelCheck | None = None) -> list[Path]:
        """Parse a Tidal URL and download the appropriate content.

        Supports album, track, and playlist URLs. Returns list of downloaded
        file paths. Raises DownloadCancelled if the job is cancelled mid-flight.
        """
        output_dir = output_dir or self.download_dir

        if m := _TRACK_RE.search(url):
            track_id = m.group(1)
            path = self.download_track(track_id, output_dir, callback=callback,
                                       cancel_check=cancel_check)
            return [path] if path else []

        if m := _ALBUM_RE.search(url):
            album_id = m.group(1)
            return self.download_album(album_id, output_dir, callback=callback,
                                        cancel_check=cancel_check)

        if m := _PLAYLIST_RE.search(url):
            playlist_id = m.group(1)
            return self.download_playlist(playlist_id, output_dir, callback=callback,
                                          cancel_check=cancel_check)

        raise ValueError(f"Unrecognised Tidal URL: {url}")

    def download_track(self, track_id: str, output_dir: Path,
                       quality: int | None = None,
                       callback: ProgressCallback | None = None,
                       cancel_check: CancelCheck | None = None) -> Path | None:
        """Download a single track. Returns path to downloaded file."""
        track = self.session.track(str(track_id))
        return self._download_track_obj(track, output_dir, quality, callback, cancel_check)

    def download_album(self, album_id: str, output_dir: Path,
                       quality: int | None = None,
                       callback: ProgressCallback | None = None,
                       cancel_check: CancelCheck | None = None) -> list[Path]:
        """Download all tracks in an album. Returns list of file paths."""
        album = self.session.album(str(album_id))
        tracks = album.tracks()
        output_dir = self._album_dir(album, output_dir)

        paths: list[Path] = []
        total = len(tracks)
        for i, track in enumerate(tracks):
            if _cancelled(cancel_check):
                raise DownloadCancelled("album cancelled before track %d" % i)
            track_callback = self._album_callback(callback, i, total) if callback else None
            path = self._download_track_obj(track, output_dir, quality, track_callback,
                                            cancel_check)
            if path:
                paths.append(path)
        self._download_cover(album, output_dir)
        return paths

    def download_playlist(self, playlist_id: str, output_dir: Path,
                          quality: int | None = None,
                          callback: ProgressCallback | None = None,
                          cancel_check: CancelCheck | None = None) -> list[Path]:
        """Download all tracks in a playlist. Returns list of file paths."""
        playlist = self.session.playlist(playlist_id)
        tracks = playlist.tracks()
        output_dir = output_dir / self._safe_name(playlist.name)

        paths: list[Path] = []
        total = len(tracks)
        for i, track in enumerate(tracks):
            if _cancelled(cancel_check):
                raise DownloadCancelled("playlist cancelled before track %d" % i)
            track_callback = self._album_callback(callback, i, total) if callback else None
            path = self._download_track_obj(track, output_dir, quality, track_callback,
                                            cancel_check)
            if path:
                paths.append(path)
        return paths

    # -- Internal ------------------------------------------------

    def _download_track_obj(self, track: tidalapi.Track, output_dir: Path,
                            quality: int | None = None,
                            callback: ProgressCallback | None = None,
                            cancel_check: CancelCheck | None = None) -> Path | None:
        """Download a single Track object to disk. Returns None on failure,
        raises DownloadCancelled if cancelled mid-transfer."""
        if _cancelled(cancel_check):
            raise DownloadCancelled("track %s cancelled before stream" % getattr(track, "id", "?"))

        try:
            stream = track.get_stream()
        except Exception as exc:
            logger.error("Failed to get stream for track %s: %s", track.id, exc)
            return None

        manifest = stream.get_stream_manifest()
        urls = manifest.get_urls()
        if not urls:
            logger.error("No download URLs for track %s", track.id)
            return None

        codec = str(manifest.codecs).upper()
        ext = _CODEC_EXTS.get(codec, ".m4a")

        artist_name = self._safe_name(track.artist.name) if track.artist else "Unknown"
        album_name = self._safe_name(track.album.name) if track.album else "Unknown"
        track_num = track.track_num or 0
        title = self._safe_name(track.name or str(track.id))

        filename = f"{track_num:02d} - {title}{ext}" if track_num else f"{title}{ext}"
        # For album downloads, _album_dir already creates a descriptive parent directory
        # (e.g., "Artist - Album (Year)"), so we place tracks directly inside it.
        # For single-track downloads, artist/album subdirectories are still used.
        if str(output_dir) != str(self.download_dir):
            file_path = output_dir / filename
        else:
            file_path = output_dir / artist_name / album_name / filename
        file_path.parent.mkdir(parents=True, exist_ok=True)

        logger.info("Downloading track %s -> %s (%d URLs)", track.id, file_path, len(urls))

        # Try every URL; BTS streams may have prefetch/rotation mirrors.
        for idx, url in enumerate(urls):
            if _cancelled(cancel_check):
                if file_path.exists():
                    file_path.unlink()
                raise DownloadCancelled("track %s cancelled" % track.id)
            try:
                total = self._download_file(url, file_path, callback, cancel_check)
            except DownloadCancelled:
                if file_path.exists():
                    file_path.unlink()
                raise
            except Exception as exc:
                logger.warning("URL %d/%d failed for track %s: %s", idx + 1, len(urls),
                               track.id, exc)
                continue
            if total > 0:
                logger.info("Downloaded track %s (%d bytes)", track.id, total)
                self._embed_tags(file_path, track)
                return file_path

        # All URLs failed — clean up any partial artefact.
        if file_path.exists():
            file_path.unlink()
        logger.error("All download URLs failed for track %s", track.id)
        return None

    def _download_file(self, url: str, output_path: Path,
                       callback: ProgressCallback | None = None,
                       cancel_check: CancelCheck | None = None) -> int:
        """Stream-download a URL to a file with progress reporting.

        Returns total bytes downloaded. Raises DownloadCancelled if the
        cancel-check fires mid-transfer, leaving no complete file behind
        (the caller cleans up partials).
        """
        headers = {}
        if self.session.access_token:
            headers["Authorization"] = f"Bearer {self.session.access_token}"

        resp = self._http.get(url, headers=headers, stream=True, timeout=60)
        resp.raise_for_status()

        total = int(resp.headers.get("content-length", 0))
        downloaded = 0
        chunk_size = 262144

        cancelled = False
        try:
            with open(output_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=chunk_size):
                    if _cancelled(cancel_check):
                        cancelled = True
                        break
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)
                        if callback:
                            callback(downloaded, total)
        finally:
            resp.close()
        if cancelled:
            raise DownloadCancelled("transfer cancelled after %d bytes" % downloaded)
        return downloaded

    def _album_dir(self, album: tidalapi.Album, base: Path) -> Path:
        artist = self._safe_name(album.artist.name) if album.artist else "Unknown"
        name = self._safe_name(album.name or str(album.id))
        year = f" ({album.year})" if album.year else ""
        return base / f"{artist} - {name}{year}"

    def _download_cover(self, album: tidalapi.Album, output_dir: Path) -> None:
        """Download album cover art to output_dir/cover.jpg."""
        cover_path = output_dir / "cover.jpg"
        if cover_path.exists():
            return
        for size in (1280, 750, 480, 320):
            try:
                url = album.image(size)
            except ValueError:
                continue
            if url:
                try:
                    r = self._http.get(url, timeout=15)
                    if r.ok:
                        cover_path.write_bytes(r.content)
                        logger.info("Cover art saved to %s", cover_path)
                        return
                except Exception as exc:
                    logger.debug("Cover download failed for size %d: %s", size, exc)
                    continue
        logger.info("No cover art available for %s", album.name)

    @staticmethod
    def _embed_tags(file_path: Path, track: tidalapi.Track) -> None:
        from mutagen import File as MutagenFile

        audio = MutagenFile(str(file_path), easy=True)
        if audio is None:
            logger.warning("mutagen could not open %s for tagging", file_path)
            return
        album = track.album
        values = {
            "title": track.name,
            "artist": track.artist.name if track.artist else None,
            "albumartist": album.artist.name if album and album.artist else None,
            "album": album.name if album else None,
            "tracknumber": str(track.track_num) if track.track_num else None,
            "date": str(album.year) if album and album.year else None,
        }
        for key, value in values.items():
            if value:
                try:
                    audio[key] = value
                except (KeyError, ValueError):
                    pass
        try:
            audio.save()
        except Exception:
            logger.warning("failed to write tags to %s", file_path, exc_info=True)

    @staticmethod
    def _album_callback(callback: ProgressCallback, index: int, total: int) -> ProgressCallback:
        """Wrap a per-track callback to report album-level fractional progress.

        For track i of N, if the track is at fraction f (= b/B bytes), the
        album reports done = (i + f) and total = N, so the caller sees smooth
        progress both across and within tracks.
        """
        def _wrapped(bytes_done: int, bytes_total: int) -> None:
            track_fraction = bytes_done / bytes_total if bytes_total > 0 else 0.0
            callback(index + track_fraction, total)
        return _wrapped

    @staticmethod
    def _safe_name(name: str | None) -> str:
        if name is None:
            return "Unknown"
        for char in '<>:"/\\|?*':
            name = name.replace(char, "_")
        name = " ".join(name.split())
        return name[:200].rstrip(".") if name else "Unknown"