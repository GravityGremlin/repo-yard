"""Deezer download engine — native track download via deezer-py + Blowfish/CBC decrypt."""

from __future__ import annotations

import hashlib
import logging
import os
import re
from pathlib import Path
from typing import Any, Callable

import requests

from app.config import DOWNLOAD_DIR, DEEZER_QUALITY

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Exceptions & type aliases
# ---------------------------------------------------------------------------

class DownloadCancelled(Exception):
    """Raised when a download is cancelled mid-transfer."""


ProgressCallback = Callable[[int, int], None]  # (bytes_done, bytes_total)
CancelCheck = Callable[[], bool]                # returns True → abort


# ---------------------------------------------------------------------------
# URL patterns
# ---------------------------------------------------------------------------

_TRACK_RE = re.compile(r"deezer\.com/(?:[a-z]{2}/)?track/(\d+)")
_ALBUM_RE = re.compile(r"deezer\.com/(?:[a-z]{2}/)?album/(\d+)")
_PLAYLIST_RE = re.compile(r"deezer\.com/(?:[a-z]{2}/)?playlist/(\d+)")

# ---------------------------------------------------------------------------
# Codec -> file extension mapping
# ---------------------------------------------------------------------------

_CODEC_EXTS: dict[str, str] = {
    "FLAC": ".flac",
    "MP4A": ".m4a",
    "MP3":  ".mp3",
    "AAC":  ".aac",
    "OGG":  ".ogg",
}

# Legacy UI aliases → canonical deezer-py format strings
_QUALITY_ALIASES: dict[str, str] = {
    "LOW": "MP3_128",
    "HIGH": "MP3_320",
    "LOSSLESS": "FLAC",
    "HIRES": "MP4_RA1",
}
_CANONICAL_QUALITIES: set[str] = {"FLAC", "MP3_320", "MP3_128", "MP4_RA1"}


def _resolve_quality(quality: str | None) -> str:
    """Map a quality string (canonical or legacy alias) to a deezer-py format.

    Canonical values (``FLAC``, ``MP3_320``, ``MP3_128``, ``MP4_RA1``) pass
    through unchanged.  Legacy UI aliases (``LOW``, ``HIGH``, ``LOSSLESS``,
    ``HIRES``) are translated.  Unknown or ``None`` values fall back to
    ``MP3_128``.
    """
    if quality is None:
        return "MP3_128"
    q = quality.strip().upper()
    if q in _CANONICAL_QUALITIES:
        return q
    return _QUALITY_ALIASES.get(q, "MP3_128")

# Blowfish key derivation secret — the well-known public Deezer key shared
# by all open-source Deezer downloaders.  Externalisable via env but the
# default preserves out-of-the-box functionality.
_DEEZER_SECRET = os.environ.get("DEEZER_SECRET", "g4el58wc0zvf9na1").encode()


# ---------------------------------------------------------------------------
# Blowfish / CBC decryption helpers
# ---------------------------------------------------------------------------

def _derive_blowfish_key(track_id: int | str) -> bytes:
    """Derive the 16-byte Blowfish decryption key for a Deezer track.

    Algorithm (from ``deezloader`` / ``streamrip``):
      1. ``MD5(str(track_id))`` → 32 hex chars
      2. Split into two 16-char halves: ``part1`` and ``part2``
      3. XOR the two halves byte-by-byte → 16-byte key

    Returns the raw 16-byte Blowfish key.
    """
    md5 = hashlib.md5(str(track_id).encode()).hexdigest()
    b1 = bytes.fromhex(md5[:16])
    b2 = bytes.fromhex(md5[16:])
    return bytes(a ^ b for a, b in zip(b1, b2))


def _decrypt_chunk(data: bytes, key: bytes, iv: bytes = b"\x00" * 8) -> bytes:
    """Decrypt a single Deezer chunk with Blowfish/CBC.

    Only the first 2048 bytes of **each 2048-byte block** are encrypted;
    the remainder of the chunk is plaintext.  ``pycryptodome`` provides the
    Blowfish cipher.

    Raises ``ValueError`` if *data* is shorter than 2048 bytes (no encrypted
    section to process).
    """
    if len(data) < 2048:
        raise ValueError("Deezer encrypted chunks must be at least 2048 bytes")
    from Crypto.Cipher import Blowfish
    cipher = Blowfish.new(key, Blowfish.MODE_CBC, iv=iv)
    return cipher.decrypt(data[:2048]) + data[2048:]


# ---------------------------------------------------------------------------
# Filesystem helpers
# ---------------------------------------------------------------------------

def _safe_name(name: str | None) -> str:
    """Sanitize a string for use as a filesystem path component."""
    if name is None:
        return "Unknown"
    for char in r'\/:*?"<>|':
        name = name.replace(char, "-")
    name = name.strip()
    return name[:200].rstrip(".") if name else "Unknown"


def _album_dir(base: Path, artist: str, album: str) -> Path:
    """Return *base / artist / album*, creating the tree if needed."""
    d = base / _safe_name(artist) / _safe_name(album)
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------------------
# Streaming download helper
# ---------------------------------------------------------------------------

def _download_file(
    session: requests.Session,
    url: str,
    dest: Path,
    callback: ProgressCallback | None = None,
    cancel_check: CancelCheck | None = None,
) -> None:
    """Stream-download *url* to *dest* with progress reporting.

    Raises ``DownloadCancelled`` if the cancel-check fires mid-transfer.
    """
    resp = session.get(url, stream=True, timeout=120)
    resp.raise_for_status()

    total = int(resp.headers.get("content-length", 0))
    downloaded = 0
    chunk_size = 65536  # 64 KiB
    cancelled = False

    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        with open(dest, "wb") as fh:
            for chunk in resp.iter_content(chunk_size=chunk_size):
                if cancel_check and cancel_check():
                    cancelled = True
                    break
                if chunk:
                    fh.write(chunk)
                    downloaded += len(chunk)
                    _log.debug("Downloaded %d / %d bytes for %s", downloaded, total, dest.name)
                    if callback:
                        callback(downloaded, total)
    finally:
        resp.close()

    if cancelled:
        # Clean up partial file
        if dest.exists():
            dest.unlink()
        raise DownloadCancelled(f"download cancelled after {downloaded} bytes")
    _log.debug("Finished streaming %s (%d bytes)", dest.name, downloaded)


# ---------------------------------------------------------------------------
# Cover art download
# ---------------------------------------------------------------------------

def _download_cover(
    session: object,  # deezer.Deezer instance (uses .gw and .http)
    album_id: str,
    output_dir: Path,
    http_session: requests.Session | None = None,
) -> Path | None:
    """Download album cover art to *output_dir / cover.jpg*.

    Uses the deezer session's gw API to look up the cover hash, then
    fetches from the Deezer CDN.  Returns the path on success, or ``None``
    if unavailable.
    """
    cover_path = output_dir / "cover.jpg"
    if cover_path.exists():
        return cover_path

    try:
        album_meta = session.gw.get_album(int(album_id))  # type: ignore[union-attr]
    except Exception as exc:
        _log.debug("Could not fetch album metadata for cover: %s", exc)
        return None

    picture = album_meta.get("ALB_PICTURE") or album_meta.get("COVER")
    if not picture:
        _log.info("No cover art available for album %s", album_id)
        return None

    cover_url = f"https://e-cdns-images.dzcdn.net/images/cover/{picture}/500x500-000000-80-0-0.jpg"
    http = http_session or session.http  # type: ignore[union-attr]
    try:
        resp = http.get(cover_url, timeout=30)
        if resp.ok and resp.content:
            output_dir.mkdir(parents=True, exist_ok=True)
            cover_path.write_bytes(resp.content)
            _log.info("Cover art saved to %s", cover_path)
            return cover_path
    except Exception as exc:
        _log.debug("Cover download failed for album %s: %s", album_id, exc)

    return None


# ---------------------------------------------------------------------------
# Mutagen tag embedding
# ---------------------------------------------------------------------------

def _embed_tags(filepath: Path, metadata: dict[str, Any]) -> None:
    """Embed Deezer metadata into an audio file via mutagen."""
    from mutagen import File as MutagenFile

    audio = MutagenFile(str(filepath), easy=True)

    if audio is None:
        # Fallback: try specific format handlers
        ext = filepath.suffix.lower()
        try:
            if ext == ".mp3":
                from mutagen.mp3 import MP3
                audio = MP3(str(filepath))
            elif ext == ".flac":
                from mutagen.flac import FLAC
                audio = FLAC(str(filepath))
            elif ext in (".m4a", ".mp4"):
                from mutagen.mp4 import MP4
                audio = MP4(str(filepath))
            else:
                _log.warning("mutagen could not open %s for tagging", filepath)
                return
        except Exception:
            _log.warning("mutagen could not open %s for tagging", filepath)
            return

    # Set text tags
    tag_map = {
        "title": metadata.get("title"),
        "artist": metadata.get("artist"),
        "albumartist": metadata.get("albumartist"),
        "album": metadata.get("album"),
        "tracknumber": metadata.get("tracknumber"),
        "date": metadata.get("date"),
        "genre": metadata.get("genre"),
    }
    for key, value in tag_map.items():
        if value is not None:
            try:
                audio[key] = str(value)
            except (KeyError, ValueError):
                pass

    # Embed cover art
    cover_data = metadata.get("cover_data")
    if cover_data:
        try:
            from mutagen.mp3 import MP3
            from mutagen.flac import FLAC
            from mutagen.mp4 import MP4

            ext = filepath.suffix.lower()
            if ext == ".mp3":
                from mutagen.id3 import APIC
                id3 = audio if isinstance(audio, MP3) else MP3(str(filepath))
                if not isinstance(id3.tags, type(None)):
                    audio = id3
                audio.delall("APIC")
                audio.add(APIC(
                    encoding=0,  # latin-1
                    mime="image/jpeg",
                    type=3,  # front cover
                    desc="Cover",
                    data=cover_data,
                ))
            elif ext == ".flac":
                flac = audio if isinstance(audio, FLAC) else FLAC(str(filepath))
                audio = flac
                from mutagen.flac import Picture
                pic = Picture()
                pic.type = 3
                pic.mime = "image/jpeg"
                pic.desc = "Cover"
                pic.data = cover_data
                audio.clear_pictures()
                audio.add_picture(pic)
            elif ext in (".m4a", ".mp4"):
                mp4 = audio if isinstance(audio, MP4) else MP4(str(filepath))
                audio = mp4
                audio["covr"] = [cover_data]
        except Exception as exc:
            _log.warning("Failed to embed cover art in %s: %s", filepath, exc)

    try:
        audio.save()
        _log.debug("Tags saved to %s", filepath)
    except Exception as exc:
        _log.warning("Failed to write tags to %s: %s", filepath, exc)


# ---------------------------------------------------------------------------
# Downloader
# ---------------------------------------------------------------------------

class DeezerDownloader:
    """Native Deezer download engine.

    Usage::

        dz = deezer.Deezer()
        dz.login_via_arl(arl)

        dl = DeezerDownloader(dz, download_dir=Path("/music"))
        paths = dl.download_url("https://deezer.com/track/12345")
    """

    def __init__(
        self,
        session: object,
        download_dir: Path | None = None,
        http_session: requests.Session | None = None,
    ):
        self.session = session
        self.download_dir = download_dir or DOWNLOAD_DIR
        self.http = http_session or requests.Session()

    # -- Public API ----------------------------------------------------------

    def download_url(
        self,
        url: str,
        output_dir: Path | None = None,
        callback: ProgressCallback | None = None,
        cancel_check: CancelCheck | None = None,
        quality: str | None = None,
    ) -> list[Path]:
        """Parse a Deezer URL and dispatch to the matching download method.

        Supports:
          - ``https://deezer.com/track/<id>``
          - ``https://deezer.com/album/<id>``
          - ``https://deezer.com/playlist/<id>``
        """
        resolved = _resolve_quality(quality or DEEZER_QUALITY)
        if m := _TRACK_RE.search(url):
            p = self.download_track(m.group(1), output_dir, quality=resolved, callback=callback, cancel_check=cancel_check)
            return [p] if p else []
        if m := _ALBUM_RE.search(url):
            return self.download_album(m.group(1), output_dir, quality=resolved, callback=callback, cancel_check=cancel_check)
        if m := _PLAYLIST_RE.search(url):
            return self.download_playlist(m.group(1), output_dir, quality=resolved, callback=callback, cancel_check=cancel_check)
        raise ValueError(f"Unrecognised Deezer URL: {url}")

    def download_track(
        self,
        track_id: str,
        output_dir: Path | None = None,
        quality: str | None = None,
        callback: ProgressCallback | None = None,
        cancel_check: CancelCheck | None = None,
    ) -> Path | None:
        """Download a single track by Deezer track ID.

        *quality*: ``"FLAC"``, ``"MP3_320"``, ``"MP3_128"`` (if available).
        Returns the path to the downloaded file, or ``None`` on failure.
        """
        quality = _resolve_quality(quality or DEEZER_QUALITY)
        # 1. Validate session
        try:
            import deezer as _deezer
            if not isinstance(self.session, _deezer.Deezer):
                _log.error("Session is not a deezer.Deezer instance")
                return None
        except ImportError:
            pass
        if not getattr(self.session, "logged_in", True):
            _log.error("Session is not logged in")
            return None

        tid = int(track_id)
        if cancel_check and cancel_check():
            raise DownloadCancelled("cancelled before track %s" % track_id)

        # 2. Get track metadata
        try:
            track = self.session.gw.get_track(tid)
        except Exception as exc:
            _log.error("Failed to get track metadata for %s: %s", track_id, exc)
            return None

        # 3. Extract track info
        title = track.get("SNG_TITLE", str(tid))
        artist = track.get("ART_NAME", "Unknown Artist")
        album_title = track.get("ALB_TITLE", "Unknown Album")
        track_num = int(track.get("TRACK_NUMBER", 0))
        duration = track.get("DURATION", 0)
        album_id = track.get("ALB_ID", "")

        _log.info("Preparing track: %s — %s / %s (%ss)", title, artist, album_title, duration)

        if cancel_check and cancel_check():
            raise DownloadCancelled("cancelled before download of track %s" % track_id)

        # 4. Get encrypted download URL (needs TRACK_TOKEN + TrackFormats)
        track_token = track.get("TRACK_TOKEN", "")
        if not track_token:
            _log.error("No TRACK_TOKEN in metadata for track %s", track_id)
            return None

        # Map quality string to a string format name (deezer-py expects str)
        _qf_map = {"FLAC": "FLAC", "MP3_320": "MP3_320",
                    "MP3_128": "MP3_128", "MP4_RA1": "MP4_RA1"}
        track_fmt = _qf_map.get(quality, "MP3_128")

        try:
            encrypted_url = self.session.get_track_url(track_token, track_fmt)
        except Exception as exc:
            _log.error("Failed to get download URL for track %s: %s", track_id, exc)
            return None

        if not encrypted_url:
            _log.error("No download URL available for track %s", track_id)
            return None

        _log.debug("Encrypted URL obtained for track %s", track_id)

        # 5. Resolve output directory
        out_base = Path(output_dir) if output_dir else self.download_dir
        album_dir = _album_dir(out_base, artist, album_title)
        album_dir.mkdir(parents=True, exist_ok=True)

        # 6. Determine extension
        codec = track.get("FORMAT", quality)
        ext = _CODEC_EXTS.get(codec.upper(), ".mp3")

        # 7. Download encrypted file
        safe_title = _safe_name(title)
        temp_path = album_dir / f"{safe_title}.encrypted"
        final_path = album_dir / f"{track_num:02d} - {safe_title}{ext}" if track_num else album_dir / f"{safe_title}{ext}"

        # Disambiguate if file already exists (multi-disc / bad metadata)
        if final_path.exists():
            stem = final_path.stem
            counter = 2
            while final_path.exists():
                final_path = album_dir / f"{stem} - {counter}{ext}"
                counter += 1

        try:
            _download_file(self.http, encrypted_url, temp_path, callback, cancel_check)
        except DownloadCancelled:
            raise
        except Exception as exc:
            _log.error("Download failed for track %s: %s", track_id, exc)
            if temp_path.exists():
                temp_path.unlink()
            return None

        # 8. Decrypt
        try:
            bf_key = _derive_blowfish_key(tid)
            with open(temp_path, "rb") as encrypted_fh, open(final_path, "wb") as decrypted_fh:
                while True:
                    chunk = encrypted_fh.read(2048)
                    if not chunk:
                        break
                    if len(chunk) >= 2048:
                        decrypted_fh.write(_decrypt_chunk(chunk, bf_key))
                    else:
                        # Last chunk shorter than 2048 — write as-is (unencrypted tail)
                        decrypted_fh.write(chunk)
            _log.info("Decrypted track %s -> %s", track_id, final_path.name)
        except Exception as exc:
            _log.error("Decryption failed for track %s: %s", track_id, exc)
            if final_path.exists():
                final_path.unlink()
            if temp_path.exists():
                temp_path.unlink()
            return None
        finally:
            if temp_path.exists():
                temp_path.unlink()

        # 9. Fetch cover art data for embedding
        cover_data = None
        if album_id:
            cover_data = self._fetch_cover_data(album_id)

        # 10. Embed tags
        meta: dict[str, Any] = {
            "title": title,
            "artist": artist,
            "albumartist": artist,
            "album": album_title,
            "tracknumber": str(track_num) if track_num else None,
            "date": track.get("PHYSICAL_RELEASE_DATE", ""),
            "genre": track.get("GENRE_ID", ""),
            "cover_data": cover_data,
        }
        try:
            _embed_tags(final_path, meta)
        except Exception as exc:
            _log.warning("Tag embedding failed for %s: %s", final_path, exc)

        _log.info("Track downloaded: %s", final_path)
        return final_path

    def download_album(
        self,
        album_id: str,
        output_dir: Path | None = None,
        quality: str | None = None,
        callback: ProgressCallback | None = None,
        cancel_check: CancelCheck | None = None,
    ) -> list[Path]:
        """Download all tracks in an album. Returns list of downloaded file paths."""
        try:
            import deezer as _deezer
            if not isinstance(self.session, _deezer.Deezer):
                _log.error("Session is not a deezer.Deezer instance")
                return []
        except ImportError:
            pass
        if not getattr(self.session, "logged_in", True):
            _log.error("Session is not logged in")
            return []

        aid = int(album_id)
        if cancel_check and cancel_check():
            raise DownloadCancelled("cancelled before album %s" % album_id)

        # Get album metadata
        try:
            album_meta = self.session.gw.get_album(aid)
        except Exception as exc:
            _log.error("Failed to get album metadata for %s: %s", album_id, exc)
            return []

        album_title = album_meta.get("TITLE", "Unknown Album")
        artist = album_meta.get("ART_NAME", "Unknown Artist")
        _log.info("Album: %s — %s", artist, album_title)

        # Get track listing
        try:
            tracks = self.session.gw.get_album_tracks(aid)
        except Exception as exc:
            _log.error("Failed to get tracks for album %s: %s", album_id, exc)
            return []

        if not tracks:
            _log.warning("Album %s has no tracks", album_id)
            return []

        # Create album directory
        out_base = Path(output_dir) if output_dir else self.download_dir
        album_dir = _album_dir(out_base, artist, album_title)

        # Download cover art
        _download_cover(self.session, album_id, album_dir, self.http)

        total = len(tracks)
        paths: list[Path] = []
        for i, track_info in enumerate(tracks):
            if cancel_check and cancel_check():
                raise DownloadCancelled("album %s cancelled before track %d" % (album_id, i))

            track_id_str = str(track_info.get("SNG_ID", ""))
            if not track_id_str:
                _log.warning("Skipping track with no ID in album %s", album_id)
                continue

            # Create fractional progress callback
            track_callback: ProgressCallback | None = None
            if callback:
                def _make_track_cb(idx: int, tot: int, cb: ProgressCallback) -> ProgressCallback:
                    def _wrapped(bytes_done: int, bytes_total: int) -> None:
                        frac = bytes_done / bytes_total if bytes_total > 0 else 0.0
                        cb(idx + frac, tot)
                    return _wrapped
                track_callback = _make_track_cb(i, total, callback)

            try:
                path = self.download_track(
                    track_id_str,
                    output_dir=album_dir,
                    quality=quality,
                    callback=track_callback,
                    cancel_check=cancel_check,
                )
                if path:
                    paths.append(path)
            except DownloadCancelled:
                raise
            except Exception as exc:
                _log.error("Failed to download track %s from album %s: %s",
                           track_id_str, album_id, exc)
                continue

        _log.info("Album %s: downloaded %d / %d tracks", album_id, len(paths), total)
        return paths

    def download_playlist(
        self,
        playlist_id: str,
        output_dir: Path | None = None,
        quality: str | None = None,
        callback: ProgressCallback | None = None,
        cancel_check: CancelCheck | None = None,
    ) -> list[Path]:
        """Download all tracks in a playlist. Returns list of downloaded file paths."""
        try:
            import deezer as _deezer
            if not isinstance(self.session, _deezer.Deezer):
                _log.error("Session is not a deezer.Deezer instance")
                return []
        except ImportError:
            pass
        if not getattr(self.session, "logged_in", True):
            _log.error("Session is not logged in")
            return []

        pid = int(playlist_id)
        if cancel_check and cancel_check():
            raise DownloadCancelled("cancelled before playlist %s" % playlist_id)

        # Get playlist metadata
        try:
            playlist_meta = self.session.gw.get_playlist(pid)
        except Exception as exc:
            _log.error("Failed to get playlist metadata for %s: %s", playlist_id, exc)
            return []

        playlist_title = playlist_meta.get("TITLE", "Unknown Playlist")
        _log.info("Playlist: %s", playlist_title)

        # Get track listing
        try:
            tracks = self.session.gw.get_playlist_tracks(pid)
        except Exception as exc:
            _log.error("Failed to get tracks for playlist %s: %s", playlist_id, exc)
            return []

        if not tracks:
            _log.warning("Playlist %s has no tracks", playlist_id)
            return []

        # Create playlist subdirectory
        out_base = Path(output_dir) if output_dir else self.download_dir
        playlist_dir = out_base / _safe_name(playlist_title)
        playlist_dir.mkdir(parents=True, exist_ok=True)

        total = len(tracks)
        paths: list[Path] = []
        for i, track_info in enumerate(tracks):
            if cancel_check and cancel_check():
                raise DownloadCancelled("playlist %s cancelled before track %d" % (playlist_id, i))

            track_id_str = str(track_info.get("SNG_ID", ""))
            if not track_id_str:
                _log.warning("Skipping track with no ID in playlist %s", playlist_id)
                continue

            # Create fractional progress callback
            track_callback: ProgressCallback | None = None
            if callback:
                def _make_track_cb(idx: int, tot: int, cb: ProgressCallback) -> ProgressCallback:
                    def _wrapped(bytes_done: int, bytes_total: int) -> None:
                        frac = bytes_done / bytes_total if bytes_total > 0 else 0.0
                        cb(idx + frac, tot)
                    return _wrapped
                track_callback = _make_track_cb(i, total, callback)

            try:
                path = self.download_track(
                    track_id_str,
                    output_dir=playlist_dir,
                    quality=quality,
                    callback=track_callback,
                    cancel_check=cancel_check,
                )
                if path:
                    paths.append(path)
            except DownloadCancelled:
                raise
            except Exception as exc:
                _log.error("Failed to download track %s from playlist %s: %s",
                           track_id_str, playlist_id, exc)
                continue

        _log.info("Playlist %s: downloaded %d / %d tracks", playlist_id, len(paths), total)
        return paths

    # -- Internal helpers ----------------------------------------------------

    def _fetch_cover_data(self, album_id: str) -> bytes | None:
        """Fetch album cover image bytes for embedding in track tags.

        Tries the gw API album metadata for the picture hash, then
        downloads from Deezer's CDN.
        """
        try:
            album_meta = self.session.gw.get_album(int(album_id))
        except Exception as exc:
            _log.debug("Could not fetch album metadata for cover: %s", exc)
            return None

        picture = album_meta.get("ALB_PICTURE") or album_meta.get("COVER")
        if not picture:
            _log.debug("No cover hash for album %s", album_id)
            return None

        cover_url = f"https://e-cdns-images.dzcdn.net/images/cover/{picture}/500x500-000000-80-0-0.jpg"
        try:
            resp = self.http.get(cover_url, timeout=30)
            if resp.ok and resp.content:
                _log.debug("Cover art fetched for album %s (%d bytes)", album_id, len(resp.content))
                return resp.content
        except Exception as exc:
            _log.debug("Cover download failed for album %s: %s", album_id, exc)

        return None
