"""Tests for library access — browse, search, serve file, ZIP download.

Covers:
* ``GET /library/search?q=...``        — full-text search within the library
* ``GET /library/browse/<path>``       — directory listing (HTMX partial)
* ``GET /library/serve/<path>``        — stream a single audio file
* ``GET /library/download/<path>``     — stream a directory as ZIP (Bundle 1)
* ``GET /library/recent``              — recently modified files
* Path-traversal prevention via safe_resolve
"""

from __future__ import annotations

from pathlib import Path


def _populate_library(root: Path) -> None:
    """Create a small music library tree for testing."""
    artist = root / "Test Artist"
    album = artist / "Test Album (2024)"
    album.mkdir(parents=True, exist_ok=True)
    (album / "01 Intro.opus").write_text("f0" * 500)
    (album / "02 Main.opus").write_text("f1" * 500)

    # Second album
    album2 = artist / "Another Album"
    album2.mkdir(parents=True, exist_ok=True)
    (album2 / "track.opus").write_text("f2" * 500)

    # Hidden file (should be invisible to browse)
    (album / ".hidden.flac").write_text("secret")


class TestLibrarySearch:
    """GET /library/search — full-text library search."""

    def test_finds_by_artist(self, app_client, tmp_path):
        """Search for an artist name returns matching files."""
        _populate_library(tmp_path / "music")
        resp = app_client.get("/library/search?q=Test+Artist")
        assert resp.status_code == 200
        assert b"Test Artist" in resp.data
        assert b"01 Intro" in resp.data or b"02 Main" in resp.data

    def test_finds_by_album(self, app_client, tmp_path):
        """Search for an album name returns files from that album."""
        _populate_library(tmp_path / "music")
        resp = app_client.get("/library/search?q=Another+Album")
        assert resp.status_code == 200
        # Track name has extension stripped in search display
        assert b"track" in resp.data

    def test_short_query_returns_empty(self, app_client, tmp_path):
        """Queries shorter than 2 characters return empty results."""
        _populate_library(tmp_path / "music")
        resp = app_client.get("/library/search?q=T")
        assert resp.status_code == 200
        assert b"0 matches" in resp.data

    def test_empty_query_returns_empty(self, app_client):
        """Empty query returns no results (not a hard error)."""
        resp = app_client.get("/library/search?q=")
        assert resp.status_code == 200

    def test_no_match_returns_empty(self, app_client, tmp_path):
        """A query that matches nothing returns zero results."""
        _populate_library(tmp_path / "music")
        resp = app_client.get("/library/search?q=ZZZTOP")
        assert resp.status_code == 200
        assert b"0 matches" in resp.data


class TestLibraryBrowse:
    """GET /library/browse/<path> — directory listing."""

    def test_browse_root(self, app_client, tmp_path):
        """Root browse shows top-level artist directories."""
        _populate_library(tmp_path / "music")
        resp = app_client.get("/library/browse/")
        assert resp.status_code == 200
        assert b"Test Artist" in resp.data

    def test_browse_artist_dir(self, app_client, tmp_path):
        """Browsing into an artist folder shows album directories."""
        _populate_library(tmp_path / "music")
        resp = app_client.get("/library/browse/Test%20Artist")
        assert resp.status_code == 200
        assert b"Test Album (2024)" in resp.data
        assert b"Another Album" in resp.data

    def test_browse_nonexistent_returns_404(self, app_client):
        """Browsing a non-existent path returns 404."""
        resp = app_client.get("/library/browse/no/such/path")
        assert resp.status_code == 404

    def test_browse_hides_dotfiles(self, app_client, tmp_path):
        """Files starting with '.' are excluded from browse listing."""
        _populate_library(tmp_path / "music")
        resp = app_client.get("/library/browse/Test%20Artist/Test%20Album%20(2024)")
        assert resp.status_code == 200
        assert b".hidden" not in resp.data


class TestServeFile:
    """GET /library/serve/<path> — stream a single audio file."""

    def test_serve_existing_file(self, app_client, tmp_path):
        """Serving an existing file returns its content."""
        _populate_library(tmp_path / "music")
        resp = app_client.get(
            "/library/serve/Test%20Artist/Test%20Album%20(2024)/01%20Intro.opus"
        )
        assert resp.status_code == 200
        assert b"f0" in resp.data

    def test_serve_nonexistent_returns_404(self, app_client):
        """Serving a non-existent file returns 404."""
        resp = app_client.get("/library/serve/missing.opus")
        assert resp.status_code == 404


class TestZipDownload:
    """GET /library/download/<path> — ZIP bundle download (Bundle 1)."""

    def test_zip_download_returns_zip(self, app_client, tmp_path):
        """ZIP download returns 200 with correct Content-Type and disposition."""
        _populate_library(tmp_path / "music")
        resp = app_client.get(
            "/library/download/Test%20Artist/Test%20Album%20(2024)"
        )
        assert resp.status_code == 200
        assert resp.content_type == "application/zip"
        disp = resp.headers.get("Content-Disposition", "")
        assert "attachment;" in disp
        assert "Test Album (2024).zip" in disp

    def test_zip_contains_files(self, app_client, tmp_path):
        """ZIP download actually bundles the directory contents."""
        _populate_library(tmp_path / "music")
        resp = app_client.get(
            "/library/download/Test%20Artist/Test%20Album%20(2024)"
        )
        assert resp.status_code == 200
        # Quick check: ZIP should be non-trivial in size
        assert len(resp.data) > 100

    def test_zip_nonexistent_returns_404(self, app_client):
        """ZIP download for a missing path returns 404."""
        resp = app_client.get("/library/download/no/such/dir")
        assert resp.status_code == 404

    def test_zip_path_traversal_blocked(self, app_client):
        """Path traversal via '../' is blocked and returns 404."""
        resp = app_client.get("/library/download/../")
        assert resp.status_code == 404

    def test_zip_on_file_returns_404(self, app_client, tmp_path):
        """Requesting a ZIP of a file (not a dir) returns 404."""
        _populate_library(tmp_path / "music")
        resp = app_client.get(
            "/library/download/Test%20Artist/Test%20Album%20(2024)/01%20Intro.opus"
        )
        assert resp.status_code == 404


class TestLibraryRecent:
    """GET /library/recent — recently modified files."""

    def test_recent_returns_list(self, app_client, tmp_path):
        """Recent endpoint returns 200 even when scan cache is empty."""
        resp = app_client.get("/library/recent")
        assert resp.status_code == 200
