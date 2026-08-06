"""Tests for QobuzDownloader in app.qobuz.downloader.

Mocks the QobuzClient layer to test download flow without network.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.qobuz.downloader import (
    QobuzDownloader, DownloadCancelled, parse_qobuz_url,
)


# ---------------------------------------------------------------------------
# URL parsing
# ---------------------------------------------------------------------------

def test_parse_track_url():
    kind, tid = parse_qobuz_url("https://www.qobuz.com/track/123456")
    assert kind == "track"
    assert tid == "123456"


def test_parse_album_url():
    kind, aid = parse_qobuz_url("https://www.qobuz.com/album/789")
    assert kind == "album"
    assert aid == "789"


def test_parse_playlist_url():
    kind, pid = parse_qobuz_url("https://www.qobuz.com/playlist/42")
    assert kind == "playlist"
    assert pid == "42"


def test_parse_invalid_url():
    assert parse_qobuz_url("https://example.com/track/1") is None


# ---------------------------------------------------------------------------
# Download flow (mocked network)
# ---------------------------------------------------------------------------

class MockQobuzClient:
    """Minimal mock of QobuzClient for download tests."""

    def __init__(self, stream_data: bytes = b"RIFF" + b"\x00" * 100):
        self.stream_data = stream_data
        self._http = MagicMock()
        self._http.headers = {"User-Agent": "test"}

    def get_track(self, track_id):
        return {
            "title": "Test Track",
            "performer": {"name": "Test Artist"},
            "album": {
                "title": "Test Album",
                "image": {"large": ""},
            },
            "track_number": 1,
        }

    def get_file_url(self, track_id, format_id, intent="stream"):
        return {"url": "https://streaming.qobuz.com/test.flac"}


def _mock_get_factory(data: bytes):
    """Return a requests.get mock that yields *data* in chunks."""
    resp = MagicMock()
    resp.status_code = 200
    resp.headers = {"content-length": str(len(data))}
    resp.iter_content = lambda chunk_size=1: [data[i:i+chunk_size] for i in range(0, len(data), chunk_size)]
    resp.raise_for_status = MagicMock()
    return resp


def test_download_url_track(tmp_path):
    """Full download flow for a track URL with mocked network."""
    stream_data = b"\x00" * 2048
    mock_session = MockQobuzClient()
    progress_calls = []

    def on_progress(done, total):
        progress_calls.append((done, total))

    cancel_called = [False]

    def on_cancel():
        return cancel_called[0]

    dl = QobuzDownloader(session=mock_session, progress_cb=on_progress, cancel_check=on_cancel)

    mock_sess = MagicMock()
    mock_sess.get.return_value = _mock_get_factory(stream_data)
    with patch("app.qobuz.downloader.make_proxied_session", return_value=mock_sess):
        result = dl.download_url(
            "https://www.qobuz.com/track/12345",
            dest_dir=str(tmp_path),
        )

    result_path = Path(result)
    assert result_path.exists()
    assert result_path.parent == tmp_path
    assert "Test Artist" in result_path.name
    assert "Test Track" in result_path.name
    assert result_path.stat().st_size == len(stream_data)


# ---------------------------------------------------------------------------
# Album download
# ---------------------------------------------------------------------------

class MockAlbumClient(MockQobuzClient):
    """Mock client that returns album track lists from get_album_tracks."""

    def __init__(self, num_tracks: int = 2, stream_data: bytes = b"\x00" * 512):
        super().__init__(stream_data=stream_data)
        self.num_tracks = num_tracks

    def get_album_tracks(self, album_id):
        return [
            {
                "id": 1000 + i,
                "title": f"Track {i + 1}",
                "performer": {"name": "Album Artist"},
                "album": {
                    "title": "Test Album",
                    "image": {"large": ""},
                },
                "track_number": i + 1,
            }
            for i in range(self.num_tracks)
        ]

    def get_track(self, track_id):
        # Return track metadata keyed by id so we can verify per-track info
        for i in range(self.num_tracks):
            tid = 1000 + i
            if track_id == tid:
                return {
                    "title": f"Track {i + 1}",
                    "performer": {"name": "Album Artist"},
                    "album": {
                        "title": "Test Album",
                        "image": {"large": ""},
                    },
                    "track_number": i + 1,
                }
        return super().get_track(track_id)


def test_download_url_album(tmp_path):
    """Album URL downloads all tracks into a single directory."""
    stream_data = b"\x00" * 512
    mock_session = MockAlbumClient(num_tracks=2, stream_data=stream_data)
    progress_calls = []

    def on_progress(done, total):
        progress_calls.append((done, total))

    dl = QobuzDownloader(session=mock_session, progress_cb=on_progress,
                         cancel_check=lambda: False)

    mock_sess = MagicMock()
    mock_sess.get.return_value = _mock_get_factory(stream_data)
    with patch("app.qobuz.downloader.make_proxied_session",
               return_value=mock_sess):
        result = dl.download_url(
            "https://www.qobuz.com/album/99999",
            dest_dir=str(tmp_path),
        )

    result_path = Path(result)
    assert result_path.is_dir()
    assert result_path.parent == tmp_path
    # Directory name follows "Artist - Album" convention
    assert "Album Artist" in result_path.name
    assert "Test Album" in result_path.name

    # Both tracks downloaded into the album dir
    files = sorted(result_path.iterdir())
    # Each track produces one audio file + possibly cover art or other files,
    # but at minimum we expect the two track files.
    audio_files = [f for f in files if f.suffix in (".flac", ".mp3", ".ogg", ".m4a")]
    assert len(audio_files) == 2
    track_names = sorted(f.name for f in audio_files)
    assert "Track 1" in track_names[0]
    assert "Track 2" in track_names[1]

    # Progress callback was invoked (2 tracks × 2 calls each = 4+ calls)
    assert len(progress_calls) >= 4


def test_download_url_album_cancel(tmp_path):
    """Cancel during album download raises DownloadCancelled."""
    mock_session = MockAlbumClient(num_tracks=4)
    cancel_called = [False]

    def on_cancel():
        return cancel_called[0]

    dl = QobuzDownloader(session=mock_session, cancel_check=on_cancel)

    # Patch _download_track to fake a successful download for the first track
    # and raise DownloadCancelled on the second — avoids any real network calls.
    call_count = [0]

    def patched_download_track(track_id, dest_dir=None):
        call_count[0] += 1
        if call_count[0] >= 2:
            cancel_called[0] = True
            raise DownloadCancelled()
        # Fake a successful download — create a dummy file
        d = Path(dest_dir) if dest_dir else tmp_path
        (d / f"track_{track_id}.flac").write_bytes(b"\x00" * 10)
        return str(d / f"track_{track_id}.flac")

    dl._download_track = patched_download_track

    with pytest.raises(DownloadCancelled):
        dl.download_url(
            "https://www.qobuz.com/album/99999",
            dest_dir=str(tmp_path),
        )


def test_download_url_album_empty(tmp_path):
    """Album with no tracks raises RuntimeError."""
    mock_session = MockAlbumClient(num_tracks=0)
    dl = QobuzDownloader(session=mock_session, cancel_check=lambda: False)

    with pytest.raises(RuntimeError, match="no tracks"):
        dl.download_url(
            "https://www.qobuz.com/album/99999",
            dest_dir=str(tmp_path),
        )


def test_download_url_playlist_not_supported():
    """Playlist URLs raise a clear ValueError."""
    dl = QobuzDownloader(session=MockQobuzClient())
    with pytest.raises(ValueError, match="Playlist downloads are not yet supported"):
        dl.download_url("https://www.qobuz.com/playlist/42")


# ---------------------------------------------------------------------------
# Download flow (mocked network)
# ---------------------------------------------------------------------------

def test_download_cancel(tmp_path):
    """Cancel check mid-download raises DownloadCancelled."""
    mock_session = MockQobuzClient()

    cancel_called = [False]

    def on_cancel():
        return cancel_called[0]

    dl = QobuzDownloader(session=mock_session, cancel_check=on_cancel)

    def fake_get(url, **kwargs):
        resp = MagicMock()
        resp.status_code = 200
        resp.headers = {"content-length": "4096"}

        def chunks(**kwargs):
            # First chunk is fine, then signal cancel
            cancel_called[0] = True
            yield b"\x00" * 100
            yield b"\x00" * 100

        resp.iter_content = chunks
        resp.raise_for_status = MagicMock()
        return resp

    mock_sess = MagicMock()
    mock_sess.get.side_effect = fake_get
    with patch("app.qobuz.downloader.make_proxied_session", return_value=mock_sess):
        with pytest.raises(DownloadCancelled):
            dl.download_url("https://www.qobuz.com/track/99999", dest_dir=str(tmp_path))


def test_download_no_session():
    """Download with no session raises RuntimeError."""
    dl = QobuzDownloader(session=None)
    with pytest.raises(RuntimeError, match="No Qobuz session"):
        dl.download_url("https://www.qobuz.com/track/1")


def test_download_non_track_url():
    """Download with an unsupported URL kind (playlist) raises ValueError."""
    mock_session = MockQobuzClient()
    dl = QobuzDownloader(session=mock_session)
    with pytest.raises(ValueError, match="not yet supported"):
        dl.download_url("https://www.qobuz.com/playlist/123")


def test_download_invalid_url():
    """Download with invalid URL raises ValueError."""
    mock_session = MockQobuzClient()
    dl = QobuzDownloader(session=mock_session)
    with pytest.raises(ValueError, match="Not a valid Qobuz URL"):
        dl.download_url("https://example.com/garbage")
