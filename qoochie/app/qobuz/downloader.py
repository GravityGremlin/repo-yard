"""Qobuz download engine — stream download with metadata tagging + cover art."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Callable

from mutagen.flac import FLAC
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4
from mutagen.oggvorbis import OggVorbis
from mutagen.id3 import APIC, TIT2, TPE1, TALB, TRCK
from mutagen.flac import Picture

from app.config import DOWNLOAD_DIR, get_format_ids
from app.proxy import make_proxied_session

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Exceptions & type aliases
# ---------------------------------------------------------------------------

class DownloadCancelled(Exception):
    """Raised when a download is cancelled mid-transfer."""


ProgressCallback = Callable[[int, int], None]  # (bytes_done, bytes_total)
CancelCheck = Callable[[], bool]                # returns True → abort

# URL patterns
_TRACK_RE = re.compile(r"qobuz\.com/track/(\d+)")
_ALBUM_RE = re.compile(r"qobuz\.com/album/(\d+)")
_PLAYLIST_RE = re.compile(r"qobuz\.com/playlist/(\d+)")

# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------

def parse_qobuz_url(url: str) -> tuple[str, str] | None:
    """Return (kind, id) from a Qobuz URL, or None."""
    for kind, regex in [("track", _TRACK_RE), ("album", _ALBUM_RE),
                        ("playlist", _PLAYLIST_RE)]:
        m = regex.search(url)
        if m:
            return kind, m.group(1)
    return None


# ---------------------------------------------------------------------------
# Metadata tagging
# ---------------------------------------------------------------------------

def _tag_file(path: Path, track_info: dict, cover_data: bytes | None = None,
              cover_mime: str = "image/jpeg") -> None:
    """Write ID3/Vorbis/MP4 tags to the downloaded audio file."""
    ext = path.suffix.lower()
    title = track_info.get("title", "")
    artist = track_info.get("artist", "")
    album = track_info.get("album", "")
    track_num = track_info.get("track_number", 0)

    try:
        if ext == ".mp3":
            audio = MP3(str(path))
            if audio.tags is None:
                audio.add_tags()
            tags = audio.tags
            tags.add(TIT2(encoding=3, text=[title]))
            tags.add(TPE1(encoding=3, text=[artist]))
            tags.add(TALB(encoding=3, text=[album]))
            if cover_data:
                tags.add(APIC(encoding=3, mime=cover_mime, type=3,
                              desc="Cover", data=cover_data))
            if track_num:
                tags.add(TRCK(encoding=3, text=[str(track_num)]))
            audio.save()
        elif ext == ".flac":
            audio = FLAC(str(path))
            audio["title"] = title
            audio["artist"] = artist
            audio["album"] = album
            if track_num:
                audio["tracknumber"] = str(track_num)
            if cover_data:
                pic = Picture()
                pic.type = 3
                pic.mime = cover_mime
                pic.desc = "Cover"
                pic.data = cover_data
                audio.clear_pictures()
                audio.add_picture(pic)
            audio.save()
        elif ext in (".m4a", ".aac"):
            audio = MP4(str(path))
            audio["\xa9nam"] = [title]
            audio["\xa9ART"] = [artist]
            audio["\xa9alb"] = [album]
            if track_num:
                audio["trkn"] = [(track_num, 0)]
            if cover_data:
                audio["covr"] = [cover_data]
            audio.save()
        elif ext == ".ogg":
            audio = OggVorbis(str(path))
            audio["title"] = [title]
            audio["artist"] = [artist]
            audio["album"] = [album]
            if track_num:
                audio["tracknumber"] = [str(track_num)]
            audio.save()
    except Exception as exc:
        _log.warning("Tagging failed for %s: %s", path.name, exc)


def _fetch_cover(url: str | None, proxy_url: str = "") -> bytes | None:
    """Download album cover art. Returns bytes or None."""
    if not url:
        return None
    try:
        resp = make_proxied_session(proxy_url).get(url, timeout=10)
        if resp.status_code == 200 and len(resp.content) > 100:
            return resp.content
    except Exception:
        _log.debug("Cover art fetch failed: %s", url)
    return None


# ---------------------------------------------------------------------------
# QobuzDownloader
# ---------------------------------------------------------------------------

class QobuzDownloader:
    """Download a single track from Qobuz with progress, cancellation, and tagging."""

    def __init__(self, session: Any = None, progress_cb: ProgressCallback | None = None,
                 cancel_check: CancelCheck | None = None, proxy_url: str = ""):
        self.session = session
        self.progress_cb = progress_cb
        self.cancel_check = cancel_check
        self.proxy_url = proxy_url

    def download_url(self, url: str, dest_dir: str | None = None) -> str:
        """Download a track or album from a Qobuz URL.

        Returns path to the downloaded file (track) or album directory.
        Raises DownloadCancelled if the cancel_check fires.
        """
        parsed = parse_qobuz_url(url)
        if not parsed:
            raise ValueError(f"Not a valid Qobuz URL: {url}")
        kind, item_id = parsed

        if kind == "track":
            track_id = int(item_id)
            return self._download_track(track_id, dest_dir)

        if kind == "album":
            return self._download_album(item_id, dest_dir)

        if kind == "playlist":
            raise ValueError(
                "Playlist downloads are not yet supported — use individual "
                "track or album URLs instead."
            )

        raise ValueError(f"QobuzDownloader does not support {kind} URLs")

    def _download_album(self, album_id: str, dest_dir: str | None = None) -> str:
        """Download all tracks in an album into a single directory.

        Returns the album directory path.
        """
        session = self.session
        if not session:
            raise RuntimeError("No Qobuz session — not connected")

        # 1) Fetch track list for the album
        try:
            tracks = session.get_album_tracks(album_id)
        except Exception as exc:
            raise RuntimeError(
                f"Failed to fetch album track list for {album_id}: {exc}"
            ) from exc

        if not tracks:
            raise RuntimeError(f"Album {album_id} has no tracks")

        # 2) Determine album directory name from first track's metadata.
        #    The track dicts from get_album_tracks carry the same shape as
        #    get_track() — ``performer`` (dict) and ``album`` (dict with title).
        first = tracks[0]
        album_info = first.get("album", {})
        if not isinstance(album_info, dict):
            album_info = {}
        album_title = album_info.get("title", f"album_{album_id}")

        artist_name = first.get("performer", {})
        if isinstance(artist_name, dict):
            artist_name = artist_name.get("name", "Unknown Artist")
        if not artist_name:
            artist_name = "Unknown Artist"

        safe_artist = re.sub(r'[\\/:*?"<>|]', '_', artist_name)[:80]
        safe_album = re.sub(r'[\\/:*?"<>|]', '_', album_title)[:100]
        album_dir_name = f"{safe_artist} - {safe_album}"

        base = Path(dest_dir) if dest_dir else DOWNLOAD_DIR
        album_dir = base / album_dir_name
        album_dir.mkdir(parents=True, exist_ok=True)

        # 3) Download each track into the album directory
        total = len(tracks)
        _log.info("Album %s: %d tracks → %s", album_id, total, album_dir)
        for idx, track in enumerate(tracks, 1):
            if self._check_cancel():
                raise DownloadCancelled()
            track_id = track.get("id")
            if not track_id:
                _log.warning("Skipping track with no id in album %s (index %d)",
                             album_id, idx)
                continue
            _log.info("Album %s: downloading track %d/%d (id=%s)",
                      album_id, idx, total, track_id)
            if self.progress_cb:
                self.progress_cb(idx - 1, total)
            self._download_track(int(track_id), str(album_dir))

        if self.progress_cb:
            self.progress_cb(total, total)

        _log.info("Album download complete: %s", album_dir)
        return str(album_dir)

    def _download_track(self, track_id: int, dest_dir: str | None = None) -> str:
        """Download a single track by ID, trying formats highest→lowest."""
        session = self.session
        if not session:
            raise RuntimeError("No Qobuz session — not connected")

        # 1) Get track metadata
        try:
            track_info = session.get_track(track_id)
        except Exception as exc:
            raise RuntimeError(f"Failed to fetch track metadata: {exc}") from exc

        title = track_info.get("title", track_info.get("name", f"track_{track_id}"))
        artist_name = track_info.get("performer", {})
        if isinstance(artist_name, dict):
            artist_name = artist_name.get("name", "")
        album_info = track_info.get("album", {})
        album_name = album_info.get("title", "") if isinstance(album_info, dict) else ""
        track_num = track_info.get("track_number", 0)
        cover_url = ""
        if isinstance(album_info, dict):
            img = album_info.get("image", {})
            if isinstance(img, dict):
                cover_url = img.get("large", img.get("medium", ""))

        # 2) Try formats highest→lowest
        format_ids = get_format_ids()
        last_exc: Exception | None = None
        for fmt_id in format_ids:
            if self._check_cancel():
                raise DownloadCancelled()
            try:
                file_data = session.get_file_url(track_id, fmt_id, intent="stream")
            except Exception as exc:
                _log.warning("getFileUrl failed for fmt %d: %s", fmt_id, exc)
                last_exc = exc
                continue

            stream_url = file_data.get("url", "")
            if stream_url:
                return self._stream_to_file(
                    stream_url, title, artist_name, album_name,
                    track_num, cover_url, dest_dir, track_info
                )

            # Encrypted key path — skip for v1, log and fall back
            _log.info("Format %d returned encrypted key for track %d, falling back", fmt_id, track_id)
            last_exc = RuntimeError("Encrypted key — not yet supported in v1")

        raise RuntimeError(f"All format attempts failed for track {track_id}: {last_exc}")

    def _stream_to_file(self, url: str, title: str, artist: str, album: str,
                        track_num: int, cover_url: str,
                        dest_dir: str | None, track_info: dict) -> str:
        """Stream the audio URL to a file with progress + tagging."""
        out_dir = Path(dest_dir) if dest_dir else DOWNLOAD_DIR
        out_dir.mkdir(parents=True, exist_ok=True)

        # Determine extension from URL or default to .flac
        ext = ".flac"
        url_lower = url.lower()
        if ".mp3" in url_lower:
            ext = ".mp3"
        elif ".m4a" in url_lower or ".mp4" in url_lower:
            ext = ".m4a"
        elif ".ogg" in url_lower:
            ext = ".ogg"

        safe_title = re.sub(r'[\\/:*?"<>|]', '_', title)[:100]
        safe_artist = re.sub(r'[\\/:*?"<>|]', '_', artist)[:80]
        filename = f"{safe_artist} - {safe_title}{ext}"
        out_path = out_dir / filename

        # Stream with progress
        headers = {"User-Agent": self.session._http.headers.get("User-Agent", "")}
        req_session = make_proxied_session(self.proxy_url)
        resp = req_session.get(url, headers=headers, stream=True, timeout=120)
        resp.raise_for_status()
        total = int(resp.headers.get("content-length", 0)) or 0
        downloaded = 0

        tmp_path = out_path.with_suffix(out_path.suffix + ".part")
        try:
            with open(tmp_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=65536):
                    if self._check_cancel():
                        raise DownloadCancelled()
                    f.write(chunk)
                    downloaded += len(chunk)
                    if self.progress_cb and total:
                        self.progress_cb(downloaded, total)

            tmp_path.rename(out_path)
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise

        # Fetch cover art
        cover_data = _fetch_cover(cover_url, proxy_url=self.proxy_url)

        # Tag the file
        _tag_file(out_path, {
            "title": title, "artist": artist, "album": album,
            "track_number": track_num,
        }, cover_data=cover_data)

        _log.info("Downloaded: %s (%d bytes)", out_path.name, downloaded)
        return str(out_path)

    def _check_cancel(self) -> bool:
        if self.cancel_check and self.cancel_check():
            return True
        return False
