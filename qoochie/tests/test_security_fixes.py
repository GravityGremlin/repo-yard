"""Regression tests for the triage audit security fixes.

Covers:
- CSRF origin guard (foreign Origin → 403, no Origin → allowed, own-host → allowed)
- Tar extraction rejects crafted archives with ``../`` path members
- QOBUZ_TOKEN never appears in captured log output
"""

from __future__ import annotations

import io
import logging
import tarfile
import time

import pytest


# ── CSRF Origin Guard ─────────────────────────────────────────────────────


class TestCSRFOriginGuard:
    """Verify the origin guard on CSRF-exempted POST endpoints."""

    def test_foreign_origin_rejected(self, app_client):
        """POST /download/enqueue with a foreign Origin → 403."""
        resp = app_client.post(
            "/download/enqueue",
            json={"url": "https://qobuz.com/track/12345"},
            headers={"Origin": "https://evil.example.com"},
        )
        assert resp.status_code == 403

    def test_foreign_origin_discography_rejected(self, app_client):
        """POST /download/discography with a foreign Origin → 403."""
        resp = app_client.post(
            "/download/discography",
            json={"artist_id": "99999"},
            headers={"Origin": "https://evil.example.com"},
        )
        assert resp.status_code == 403

    def test_foreign_origin_playlist_resolve_rejected(self, app_client):
        """POST /playlist/resolve with a foreign Origin → 403."""
        resp = app_client.post(
            "/playlist/resolve",
            json={"url": "https://qobuz.com/playlist/12345"},
            headers={"Origin": "https://evil.example.com"},
        )
        assert resp.status_code == 403

    def test_no_origin_allowed(self, app_client):
        """POST /download/enqueue with no Origin/Referer → allowed (server-to-server)."""
        resp = app_client.post(
            "/download/enqueue",
            json={"url": "https://qobuz.com/track/12345"},
        )
        # Should NOT be 403 (may be 400/503 depending on Qobuz session, but not 403)
        assert resp.status_code != 403

    def test_own_host_origin_allowed(self, app_client):
        """POST /download/enqueue with own host Origin → allowed."""
        resp = app_client.post(
            "/download/enqueue",
            json={"url": "https://qobuz.com/track/12345"},
            headers={"Origin": "http://10.8.0.10:19290"},
        )
        assert resp.status_code != 403

    def test_localhost_origin_allowed(self, app_client):
        """POST /download/enqueue with localhost Origin → allowed."""
        resp = app_client.post(
            "/download/enqueue",
            json={"url": "https://qobuz.com/track/12345"},
            headers={"Origin": "http://localhost:19290"},
        )
        assert resp.status_code != 403

    def test_repo_yard_origin_allowed(self, app_client):
        """POST /download/enqueue with ry.n0g.xyz Origin → allowed."""
        resp = app_client.post(
            "/download/enqueue",
            json={"url": "https://qobuz.com/track/12345"},
            headers={"Origin": "http://ry.n0g.xyz:19297"},
        )
        assert resp.status_code != 403

    def test_foreign_referer_rejected(self, app_client):
        """POST /download/enqueue with a foreign Referer (no Origin) → 403."""
        resp = app_client.post(
            "/download/enqueue",
            json={"url": "https://qobuz.com/track/12345"},
            headers={"Referer": "https://evil.example.com/page"},
        )
        assert resp.status_code == 403

    def test_own_host_referer_allowed(self, app_client):
        """POST /download/enqueue with own host Referer → allowed."""
        resp = app_client.post(
            "/download/enqueue",
            json={"url": "https://qobuz.com/track/12345"},
            headers={"Referer": "http://10.8.0.10:19290/enqueue"},
        )
        assert resp.status_code != 403

    def test_non_exempted_endpoint_unaffected(self, app_client):
        """POST to a non-exempted endpoint with foreign Origin → not blocked by origin guard."""
        # /import/upload is a regular CSRF-protected endpoint; the origin guard
        # only fires on the exempted paths. This request will be handled by
        # SeaSurf or the route logic, not the origin guard.
        resp = app_client.post(
            "/import/upload",
            data={},
            headers={"Origin": "https://evil.example.com"},
            content_type="multipart/form-data",
        )
        # Should not be a 403 from the origin guard (SeaSurf is disabled in tests)
        assert resp.status_code != 403


# ── Tar Extraction Traversal ──────────────────────────────────────────────


class TestTarTraversal:
    """Verify that tar extraction rejects path-traversal members."""

    def test_extract_tar_rejects_traversal(self, tmp_path):
        """A tar archive with a ``../`` member should raise ValueError."""
        archive = tmp_path / "evil.tar"
        dest = tmp_path / "out"
        dest.mkdir()

        # Create a tar with a traversal entry
        with tarfile.open(archive, "w") as tf:
            info = tarfile.TarInfo(name="../../etc/evil.txt")
            data = b"pwned"
            info.size = len(data)
            from io import BytesIO
            tf.addfile(info, BytesIO(data))

        with pytest.raises(ValueError, match="Path traversal"):
            from app.import_upload.extract import extract_archive
            extract_archive(archive, dest)

    def test_extract_tar_rejects_absolute_path(self, tmp_path):
        """A tar archive with an absolute-path member should raise ValueError."""
        archive = tmp_path / "abs.tar"
        dest = tmp_path / "out"
        dest.mkdir()

        with tarfile.open(archive, "w") as tf:
            info = tarfile.TarInfo(name="/tmp/evil.txt")
            data = b"pwned"
            info.size = len(data)
            from io import BytesIO
            tf.addfile(info, BytesIO(data))

        with pytest.raises(ValueError, match="Path traversal|Absolute path"):
            from app.import_upload.extract import extract_archive
            extract_archive(archive, dest)

    def test_extract_tar_safe_member_succeeds(self, tmp_path):
        """A tar with only safe audio members should extract normally."""
        archive = tmp_path / "safe.tar"
        dest = tmp_path / "out"

        with tarfile.open(archive, "w") as tf:
            info = tarfile.TarInfo(name="subdir/track.flac")
            data = b"fake audio"
            info.size = len(data)
            from io import BytesIO
            tf.addfile(info, BytesIO(data))

        from app.import_upload.extract import extract_archive
        result = extract_archive(archive, dest)
        assert len(result) == 1
        assert result[0].exists()


# ── Token Log Scrubbing ──────────────────────────────────────────────────


class TestTokenLogScrubbing:
    """Verify QOBUZ_TOKEN never appears in log output."""

    def test_token_not_in_logs(self, tmp_path, monkeypatch, caplog):
        """Simulate an error containing the token string — it should be redacted."""
        test_token = "SUPERSECRET_TOKEN_VALUE_12345678"
        monkeypatch.setenv("QOBUZ_TOKEN", test_token)

        # Refresh the scrub filter so it picks up the new env value
        from app.logging_config import _TokenScrubFilter
        filt = _TokenScrubFilter()
        filt._refresh()

        logger = logging.getLogger("test_token_scrub")
        logger.addFilter(filt)

        with caplog.at_level(logging.DEBUG, logger="test_token_scrub"):
            # Log a message that would contain the token (simulating an exception)
            logger.error("Authentication failed: token=%s was rejected", test_token)
            logger.warning("Request header contained: X-User-Auth-Token=%s", test_token)

        # The token must never appear in the captured log output
        for record in caplog.records:
            assert test_token not in record.getMessage(), (
                f"Token leaked into log: {record.getMessage()}"
            )
        # Verify redaction happened
        assert any("[REDACTED]" in r.getMessage() for r in caplog.records)

    def test_secret_not_in_logs(self, tmp_path, monkeypatch, caplog):
        """QOBUZ_APP_SECRET should also be scrubbed."""
        test_secret = "MY_APP_SECRET_VALUE_ABCDEF"
        monkeypatch.setenv("QOBUZ_APP_SECRET", test_secret)

        from app.logging_config import _TokenScrubFilter
        filt = _TokenScrubFilter()
        filt._refresh()

        logger = logging.getLogger("test_secret_scrub")
        logger.addFilter(filt)

        with caplog.at_level(logging.DEBUG, logger="test_secret_scrub"):
            logger.error("Signing failed with secret %s", test_secret)

        for record in caplog.records:
            assert test_secret not in record.getMessage(), (
                f"Secret leaked into log: {record.getMessage()}"
            )
