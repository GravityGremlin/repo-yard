"""YT-DLP fallback provider — downloads from YouTube Music when streamrip can't find a track."""

import logging
import subprocess
from pathlib import Path
from typing import Optional, Callable

from app.config import _cfg

from .provider import AudioProvider, DownloadCancelled, Track

log = logging.getLogger(__name__)

# Optional heavy imports — gracefully degrade when packages are missing.
try:
    import yt_dlp  # type: ignore[import-untyped]
except ImportError:
    yt_dlp = None  # type: ignore[assignment]

try:
    from ytmusicapi import YTMusic  # type: ignore[import-untyped]
except ImportError:
    YTMusic = None  # type: ignore[assignment,misc]


class YTDlpProvider(AudioProvider):
    """Download audio from YouTube Music via yt-dlp."""

    name = "ytdlp"
    quality_label = "m4a"

    def __init__(self) -> None:
        self.fmt: str = _cfg("sources.ytdlp.format", "m4a/bestaudio/best")
        self.ratelimit: int = int(_cfg("sources.ytdlp.ratelimit", 1_000_000))
        self.sleep_interval: int = int(_cfg("sources.ytdlp.sleep_interval_requests", 3))
        self.max_concurrent: int = int(_cfg("sources.ytdlp.max_concurrent", 2))

    # -- search ---------------------------------------------------------------

    def search(self, track: Track) -> Optional[str]:
        """Return a YouTube URL, or *None* if nothing found."""

        # 1. Try ytmusicapi if available (better metadata matching).
        if YTMusic is not None:
            url = self._search_ytmusic(track)
            if url:
                return url

        # 2. Fallback: yt-dlp ytsearch (no OAuth needed).
        return self._search_ytdlp_cli(track)

    def _search_ytmusic(self, track: Track) -> Optional[str]:
        try:
            ytm = YTMusic()
            query = f"{track.artist} {track.title}"
            results = ytm.search(query, filter="songs", limit=5)
            for r in results:
                vid = r.get("videoId")
                if vid:
                    url = f"https://music.youtube.com/watch?v={vid}"
                    log.info("ytmusic found: %s", url)
                    return url
            log.debug("ytmusic: no results for %s", query)
        except Exception:
            log.exception("ytmusicapi search failed")
        return None

    @staticmethod
    def _search_ytdlp_cli(track: Track) -> Optional[str]:
        query = f"{track.artist} - {track.title}"
        cmd = [
            "yt-dlp",
            f"ytsearch1:{query}",
            "--get-id",
            "--no-warnings",
            "--quiet",
        ]
        log.debug("yt-dlp search: %s", " ".join(cmd))
        try:
            result = subprocess.run(
                cmd,
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
            )
            video_id = result.stdout.strip().splitlines()
            if video_id:
                url = f"https://www.youtube.com/watch?v={video_id[0]}"
                log.info("yt-dlp found: %s", url)
                return url
            log.debug("yt-dlp: no results for query=%s", query)
        except subprocess.CalledProcessError as exc:
            log.warning("yt-dlp search failed (rc=%s): %s", exc.returncode, exc.stderr.strip())
        except subprocess.TimeoutExpired:
            log.warning("yt-dlp search timed out for query=%s", query)
        return None

    # -- download -------------------------------------------------------------

    def download(
        self,
        track: Track,
        resource_id: str,
        output_dir: Path,
        progress_cb: Optional[Callable[[int, int], None]] = None,
        cancel_signal: Optional[Callable[[], bool]] = None,
    ) -> Optional[Path]:
        if yt_dlp is None:
            log.error("yt-dlp Python package is not installed — cannot download")
            return None

        if cancel_signal and cancel_signal():
            raise DownloadCancelled()

        output_dir.mkdir(parents=True, exist_ok=True)

        def _progress_hook(d: dict) -> None:
            """Bridge yt-dlp's progress dict to our callback."""
            if cancel_signal and cancel_signal():
                raise DownloadCancelled()
            if progress_cb:
                downloaded = d.get("downloaded_bytes", 0) or 0
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                progress_cb(downloaded, total)

        ydl_opts: dict = {
            "format": self.fmt,
            "outtmpl": "%(id)s.%(ext)s",
            "paths": {"home": str(output_dir)},
            "quiet": True,
            "no_warnings": True,
            "postprocessors": [
                {"key": "FFmpegExtractAudio", "preferredcodec": "m4a"},
            ],
            "ratelimit": self.ratelimit,
            "sleep_interval_requests": self.sleep_interval,
            "retries": 5,
            "noprogress": False,
            "progress_hooks": [_progress_hook],
        }

        log.info("yt-dlp download: %s", resource_id)
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(resource_id, download=True)
        except DownloadCancelled:
            raise
        except Exception:
            log.exception("yt-dlp download failed for %s", resource_id)
            return None

        if info is None:
            log.warning("yt-dlp returned no info for %s", resource_id)
            return None

        # Find the audio file that yt-dlp produced.
        return self._find_downloaded_file(output_dir, info.get("id", ""))

    @staticmethod
    def _find_downloaded_file(output_dir: Path, video_id: str) -> Optional[Path]:
        """Return the first file matching ``<video_id>.*`` in *output_dir*."""
        if not video_id:
            # Fallback: grab newest .m4a in dir.
            candidates = sorted(
                output_dir.glob("*.m4a"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            return candidates[0] if candidates else None

        candidates = list(output_dir.glob(f"{video_id}.*"))
        # Prefer the audio extension.
        for ext in ("m4a", "opus", "mp3", "wav", "ogg", "webm"):
            match = [c for c in candidates if c.suffix == f".{ext}"]
            if match:
                log.info("yt-dlp: downloaded %s", match[0])
                return match[0]
        # Return whatever we found.
        if candidates:
            log.info("yt-dlp: downloaded %s", candidates[0])
            return candidates[0]

        log.warning("yt-dlp: no output file found for id=%s in %s", video_id, output_dir)
        return None
