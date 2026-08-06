"""Streamrip provider — downloads FLAC from Tidal/Qobuz via the streamrip CLI."""

import json
import logging
import subprocess
import tempfile
from pathlib import Path
from typing import Optional, Callable

from app.config import _cfg

from .provider import AudioProvider, DownloadCancelled, Track

log = logging.getLogger(__name__)


class StreamripProvider(AudioProvider):
    """Download lossless audio through the streamrip CLI."""

    name = "streamrip"
    quality_label = "FLAC"

    def __init__(self) -> None:
        self.binary: str = _cfg("sources.streamrip.binary", "rip")
        self.timeout: int = int(_cfg("sources.streamrip.timeout", 300))
        self.quality: str = _cfg("sources.streamrip.quality", "lossless")

    # -- search ---------------------------------------------------------------

    def search(self, track: Track) -> Optional[str]:
        """Return a provider resource ID, or *None* if not found."""

        # Prefer ISRC lookup when available — exact match.
        if track.isrc:
            rid = self._search_query(f"isrc:{track.isrc}")
            if rid:
                return rid

        # Fallback: artist + title text search.
        return self._search_query(f"{track.artist} - {track.title}")

    def _search_query(self, query: str) -> Optional[str]:
        # streamrip removed the --json flag; use -o to write results to a temp file
        # The temp file is created outside the try block; the finally block guarantees
        # cleanup on all Python-level exit paths (normal return, caught exceptions,
        # and uncaught exceptions). Only a process-level crash (SIGKILL) could leak.
        output_file: Optional[str] = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tf:
                output_file = tf.name
            cmd = [self.binary, "search", query, "-o", output_file, "-n", "1"]
            log.debug("streamrip search: %s", " ".join(cmd))
            subprocess.run(
                cmd,
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
            )
            with open(output_file) as f:
                data = json.load(f)
            results = data.get("results", [])
            if results:
                rid = results[0].get("id")
                if rid:
                    log.info("streamrip found: %s (query=%s)", rid, query)
                    return str(rid)
            log.debug("streamrip: no results for query=%s", query)
        except subprocess.CalledProcessError as exc:
            log.warning("streamrip search failed (rc=%s): %s", exc.returncode, exc.stderr.strip())
        except (json.JSONDecodeError, FileNotFoundError):
            log.warning("streamrip: could not parse search results for query=%s", query)
        except subprocess.TimeoutExpired:
            log.warning("streamrip search timed out for query=%s", query)
        finally:
            if output_file:
                try:
                    Path(output_file).unlink(missing_ok=True)
                except OSError:
                    pass
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
        if cancel_signal and cancel_signal():
            raise DownloadCancelled()

        output_dir.mkdir(parents=True, exist_ok=True)
        cmd = [
            self.binary,
            "url",
            resource_id,
            "-d",
            str(output_dir),
            "--quality",
            self.quality,
        ]
        log.info("streamrip download: %s", " ".join(cmd))

        try:
            # Signal mid-start
            if progress_cb:
                progress_cb(0, 100)

            subprocess.run(
                cmd,
                cwd=output_dir,
                timeout=self.timeout,
                capture_output=True,
                text=True,
                check=True,
            )

            if progress_cb:
                progress_cb(80, 100)

            if cancel_signal and cancel_signal():
                raise DownloadCancelled()

        except subprocess.CalledProcessError as exc:
            log.error("streamrip download failed (rc=%s): %s", exc.returncode, exc.stderr.strip())
            return None
        except subprocess.TimeoutExpired:
            log.error("streamrip download timed out after %ss for %s", self.timeout, resource_id)
            return None

        if progress_cb:
            progress_cb(100, 100)

        # Locate the most-recently modified .flac in the output directory.
        return self._find_downloaded_file(output_dir)

    @staticmethod
    def _find_downloaded_file(output_dir: Path) -> Optional[Path]:
        """Return the newest *.flac in *output_dir*, or *None*."""
        flacs = sorted(
            output_dir.glob("*.flac"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if flacs:
            log.info("streamrip: downloaded %s", flacs[0])
            return flacs[0]
        log.warning("streamrip: no .flac file found in %s after download", output_dir)
        return None
