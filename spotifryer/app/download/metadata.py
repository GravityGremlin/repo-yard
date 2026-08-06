"""Embed Spotify metadata into downloaded audio files using mutagen."""
from __future__ import annotations

import io
import logging
from pathlib import Path
from typing import Optional

try:
    import mutagen
    from mutagen import File as MutagenFile
    HAS_MUTAGEN = True
except ImportError:
    HAS_MUTAGEN = False

try:
    from PIL import Image
    HAS_PILLOW = True
except ImportError:
    HAS_PILLOW = False

import requests

from app.sources.provider import Track

logger = logging.getLogger(__name__)


def _download_cover(url: str) -> Optional[bytes]:
    """Download cover art, return JPEG bytes."""
    if not url:
        return None
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.content
        # Convert to JPEG via Pillow if available
        if HAS_PILLOW:
            try:
                img = Image.open(io.BytesIO(data))
                if img.mode != "RGB":
                    img = img.convert("RGB")
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=95)
                return buf.getvalue()
            except Exception:
                logger.warning("Pillow thumbnail conversion failed, using original")
                # Fall back to original bytes if Pillow conversion fails
                return data
        return data
    except Exception:
        logger.warning("Failed to download cover art from %s", url)
        return None


def _embed_flac(audio, track: Track, cover_bytes: Optional[bytes]) -> None:
    """Embed metadata into a FLAC file."""
    audio["title"] = track.title
    audio["artist"] = track.artist
    audio["album"] = track.album or ""
    audio["albumartist"] = track.artist  # Spotify doesn't provide albumartist separately
    if track.isrc:
        audio["isrc"] = track.isrc
    if track.track_number is not None:
        audio["tracknumber"] = str(track.track_number)
    if cover_bytes:
        from mutagen.flac import Picture
        pic = Picture()
        pic.type = 3  # Cover (front)
        pic.mime = "image/jpeg"
        pic.data = cover_bytes
        audio.add_picture(pic)
    audio.save()


def _embed_mp4(audio, track: Track, cover_bytes: Optional[bytes]) -> None:
    """Embed metadata into an M4A/MP4 file."""
    audio["\xa9nam"] = [track.title]
    audio["\xa9ART"] = [track.artist]
    audio["\xa9alb"] = [track.album or ""]
    audio["aART"] = [track.artist]
    if track.isrc:
        audio["\xa9cmt"] = [track.isrc]
    if track.track_number is not None:
        audio["trkn"] = [(track.track_number, 0)]
    if cover_bytes:
        from mutagen.mp4 import MP4Cover
        audio["covr"] = [MP4Cover(cover_bytes, imageformat=MP4Cover.FORMAT_JPEG)]
    audio.save()


def _embed_id3(audio, track: Track, cover_bytes: Optional[bytes]) -> None:
    """Embed metadata into an MP3 file via ID3 tags."""
    from mutagen.id3 import TIT2, TPE1, TALB, TPE2, TRCK, TSRC, APIC

    # Ensure ID3 header exists
    if audio.tags is None:
        audio.add_tags()
    tags = audio.tags

    tags["TIT2"] = TIT2(encoding=3, text=[track.title])
    tags["TPE1"] = TPE1(encoding=3, text=[track.artist])
    tags["TALB"] = TALB(encoding=3, text=[track.album or ""])
    tags["TPE2"] = TPE2(encoding=3, text=[track.artist])
    if track.isrc:
        tags["TSRC"] = TSRC(encoding=3, text=[track.isrc])
    if track.track_number is not None:
        tags["TRCK"] = TRCK(encoding=3, text=[str(track.track_number)])
    if cover_bytes:
        tags["APIC"] = APIC(
            encoding=3,
            mime="image/jpeg",
            type=3,  # Cover (front)
            desc="Cover",
            data=cover_bytes,
        )
    audio.save()


def _embed_ogg(audio, track: Track, cover_bytes: Optional[bytes]) -> None:
    """Embed metadata into an OGG/Opus file via Vorbis Comments."""
    from mutagen.flac import Picture

    audio["TITLE"] = track.title
    audio["ARTIST"] = track.artist
    audio["ALBUM"] = track.album or ""
    audio["ALBUMARTIST"] = track.artist
    if track.isrc:
        audio["ISRC"] = track.isrc
    if track.track_number is not None:
        audio["TRACKNUMBER"] = str(track.track_number)
    if cover_bytes:
        pic = Picture()
        pic.type = 3
        pic.mime = "image/jpeg"
        pic.data = cover_bytes
        import base64
        audio["METADATA_BLOCK_PICTURE"] = base64.b64encode(pic.write()).decode("ascii")
    audio.save()


def embed_metadata(file_path: Path, track: Track) -> bool:
    """Embed Spotify metadata into an audio file.

    Supports FLAC, M4A/MP4, MP3, and OGG/Opus formats.
    Returns True on success, False on failure.
    """
    if not HAS_MUTAGEN:
        logger.warning("mutagen not installed — skipping metadata embedding for %s", file_path)
        return False

    if not file_path.exists():
        logger.error("File not found: %s", file_path)
        return False

    # Download cover art if URL provided
    cover_bytes = _download_cover(track.cover_url) if track.cover_url else None

    suffix = file_path.suffix.lower()

    try:
        if suffix == ".flac":
            from mutagen.flac import FLAC
            audio = FLAC(str(file_path))
            _embed_flac(audio, track, cover_bytes)
        elif suffix in (".m4a", ".mp4"):
            from mutagen.mp4 import MP4
            audio = MP4(str(file_path))
            _embed_mp4(audio, track, cover_bytes)
        elif suffix == ".mp3":
            audio = MutagenFile(str(file_path), easy=False)
            if audio is None:
                logger.error("Could not open MP3: %s", file_path)
                return False
            _embed_id3(audio, track, cover_bytes)
        elif suffix in (".ogg", ".opus"):
            # Try Opus first, fall back to Vorbis
            try:
                from mutagen.oggopus import OggOpus
                audio = OggOpus(str(file_path))
            except mutagen.MutagenError:
                from mutagen.oggvorbis import OggVorbis
                audio = OggVorbis(str(file_path))
            _embed_ogg(audio, track, cover_bytes)
        else:
            logger.warning("Unsupported audio format: %s — skipping metadata", suffix)
            return False

        logger.info("Embedded metadata for %s — %s", track.artist, track.title)
        return True

    except Exception:
        logger.exception("Failed to embed metadata in %s", file_path)
        return False
