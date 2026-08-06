"""Regression tests for triage-audit security fixes.

(a) Concurrent token refresh → token.json always valid JSON with latest refresh.
(b) CSRF Origin/Referer guard on exempt endpoints → 403 for untrusted origins.
(c) Tar extraction traversal rejection (belt-and-suspenders with filter='data').
"""

from __future__ import annotations

import io
import json
import sys
import tarfile
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.import_upload.extract import extract_archive


# ═══════════════════════════════════════════════════════════════════
# (a) Concurrent token refresh — token.json integrity
# ═══════════════════════════════════════════════════════════════════


class TestConcurrentTokenRefresh:
    """Multiple threads calling _create_session should never corrupt token.json."""

    def _write_token(self, token_file: Path, refresh_token: str, expired: bool = True) -> None:
        """Write a minimal token.json to *token_file*."""
        from datetime import datetime, timezone
        if expired:
            expiry = datetime(2020, 1, 1, tzinfo=timezone.utc).timestamp()
        else:
            expiry = datetime(2099, 1, 1, tzinfo=timezone.utc).timestamp()
        data = {
            "access_token": "test-access",
            "refresh_token": refresh_token,
            "token_type": "Bearer",
            "expiry_time": expiry,
        }
        token_file.write_text(json.dumps(data, indent=2))

    def test_concurrent_refresh_produces_valid_json(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Spawn N threads racing _create_session; token.json stays valid JSON."""
        from app.tidal import session as tidal_session

        tidal_cfg = tmp_path / "tidal_config"
        tidal_cfg.mkdir(parents=True, exist_ok=True)
        token_file = tidal_cfg / "token.json"

        # Write an expired token so the refresh branch fires.
        self._write_token(token_file, refresh_token="old-refresh-token-aaa", expired=True)

        monkeypatch.setattr(tidal_session, "_token_file", token_file)

        refresh_counter = {"count": 0}
        refresh_lock = threading.Lock()

        def make_fake_session(session_obj):
            """Configure fake_token_refresh on the mock session so that the
            session's own attributes stay in sync with what _write_token_file
            will persist — mimicking how tidalapi.Session.token_refresh works
            (updates access_token / refresh_token in-place then returns True).
            """
            def fake_token_refresh(refresh_token: str) -> bool:
                """Simulate Tidal rotating the refresh token on each call."""
                from datetime import datetime, timezone
                with refresh_lock:
                    refresh_counter["count"] += 1
                    seq = refresh_counter["count"]
                new_refresh = f"rotated-refresh-{seq:03d}"
                # Update the session attributes so _write_token_file picks them up.
                session_obj.access_token = f"new-access-{seq:03d}"
                session_obj.refresh_token = new_refresh
                session_obj.expiry_time = datetime(2099, 6, 1, tzinfo=timezone.utc)
                return True
            session_obj.token_refresh = fake_token_refresh

        NUM_THREADS = 12
        errors: list[Exception] = []

        def worker() -> None:
            try:
                session_obj = MagicMock()
                session_obj.config = MagicMock()
                session_obj.config.quality = "HIGH"
                session_obj.request_session = MagicMock()
                session_obj.access_token = "test-access"
                session_obj.refresh_token = "old-refresh-token-aaa"
                session_obj.token_type = "Bearer"
                session_obj.expiry_time = None
                session_obj.load_oauth_session = MagicMock()
                session_obj.check_login = MagicMock(return_value=True)
                make_fake_session(session_obj)

                with patch.object(tidal_session, "tidalapi") as mock_tidalapi:
                    mock_tidalapi.Session.return_value = session_obj
                    tidal_session._create_session()
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(NUM_THREADS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors, f"Thread exceptions: {errors}"

        # token.json must still be valid JSON.
        raw = json.loads(token_file.read_text())
        assert "refresh_token" in raw
        assert raw["refresh_token"].startswith("rotated-refresh-")
        assert raw["token_type"] == "Bearer"

    def test_token_file_never_partial_during_concurrent_writes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Concurrent _write_token_file calls always leave valid JSON."""
        from app.tidal import session as tidal_session

        tidal_cfg = tmp_path / "tidal_config"
        tidal_cfg.mkdir(parents=True, exist_ok=True)
        token_file = tidal_cfg / "token.json"
        monkeypatch.setattr(tidal_session, "_token_file", token_file)

        NUM_THREADS = 20
        errors: list[Exception] = []

        def writer(thread_id: int) -> None:
            try:
                mock_session = MagicMock()
                mock_session.access_token = f"token-{thread_id}"
                mock_session.refresh_token = f"refresh-{thread_id}"
                mock_session.token_type = "Bearer"
                mock_session.expiry_time = None
                tidal_session._write_token_file(mock_session)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(NUM_THREADS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors, f"Thread exceptions: {errors}"
        # Must be valid JSON regardless of interleaving.
        raw = json.loads(token_file.read_text())
        assert raw["token_type"] == "Bearer"
        assert raw["refresh_token"].startswith("refresh-")


# ═══════════════════════════════════════════════════════════════════
# (b) CSRF Origin/Referer guard — allow / deny matrix
# ═══════════════════════════════════════════════════════════════════


class TestCSRFOriginGuard:
    """Verify the Origin/Referer guard on CSRF-exempt endpoints."""

    @pytest.mark.parametrize(
        "origin, expected_status",
        [
            # Untrusted third-party → 403
            ("https://evil.example.com", 403),
            ("https://attacker.org:8080", 403),
            ("http://random-host.local", 403),
            # Same host (127.0.0.1 as test server) → allowed
            ("http://127.0.0.1:19290", 200),
            # Trusted repo-yard hosts → allowed
            ("http://10.8.0.10:19297", 200),
            ("http://10.8.0.10:19290", 200),
            ("http://localhost:19290", 200),
            ("https://ry.n0g.xyz", 200),
        ],
    )
    def test_enqueue_origin_matrix(self, app_client, monkeypatch, origin, expected_status):
        """POST /download/enqueue — Origin header gate."""
        # Mock get_session so the route doesn't need a real Tidal connection
        # when the request is allowed past the origin guard.
        monkeypatch.setattr("app.download.routes.get_session", lambda: MagicMock())
        resp = app_client.post(
            "/download/enqueue",
            data=json.dumps({"url": "https://tidal.com/track/123"}),
            content_type="application/json",
            headers={"Origin": origin},
        )
        assert resp.status_code == expected_status

    @pytest.mark.parametrize(
        "origin, expected_status",
        [
            ("https://evil.com", 403),
            ("http://127.0.0.1:19290", 200),
            ("http://10.8.0.10:19297", 200),
        ],
    )
    def test_discography_origin_matrix(self, app_client, monkeypatch, origin, expected_status):
        """POST /download/discography — Origin header gate."""
        monkeypatch.setattr("app.download.routes.get_session", lambda: MagicMock())
        resp = app_client.post(
            "/download/discography",
            data=json.dumps({"artist_id": "123"}),
            content_type="application/json",
            headers={"Origin": origin},
        )
        assert resp.status_code == expected_status

    def test_no_origin_server_to_server_allowed(self, app_client, monkeypatch):
        """POST /download/enqueue with no Origin header → allowed (S2S)."""
        monkeypatch.setattr("app.download.routes.get_session", lambda: MagicMock())
        resp = app_client.post(
            "/download/enqueue",
            data=json.dumps({"url": "https://tidal.com/track/123"}),
            content_type="application/json",
        )
        # No Origin → bypass guard → route logic runs.
        assert resp.status_code == 200

    @pytest.mark.parametrize(
        "referer, expected_status",
        [
            ("https://evil.example.com/page", 403),
            ("http://127.0.0.1:19290/search", 200),
            ("https://ry.n0g.xyz/search", 200),
        ],
    )
    def test_referer_fallback(self, app_client, monkeypatch, referer, expected_status):
        """POST /download/enqueue — Referer header used when Origin absent."""
        monkeypatch.setattr("app.download.routes.get_session", lambda: MagicMock())
        resp = app_client.post(
            "/download/enqueue",
            data=json.dumps({"url": "https://tidal.com/track/456"}),
            content_type="application/json",
            headers={"Referer": referer},
        )
        assert resp.status_code == expected_status

    def test_non_exempt_endpoint_bypasses_guard(self, app_client):
        """GET /search (not an exempt endpoint) is unaffected by the origin guard."""
        resp = app_client.get("/search?q=test")
        # Not 403 — the origin guard didn't interfere.
        # (503 is expected: no Tidal session in test env.)
        assert resp.status_code != 403


# ═══════════════════════════════════════════════════════════════════
# (c) Tar extraction traversal rejection
# ═══════════════════════════════════════════════════════════════════


class TestTarTraversalRejection:
    """Verify tar extraction rejects traversal attempts."""

    def _make_tar(self, tmp_path: Path, entries: dict[str, bytes]) -> Path:
        """Create a tar.gz at tmp_path/test.tar.gz with the given entries."""
        tar_path = tmp_path / "test.tar.gz"
        with tarfile.open(tar_path, "w:gz") as tf:
            for name, content in entries.items():
                info = tarfile.TarInfo(name=name)
                data = content or b""
                info.size = len(data)
                tf.addfile(info, io.BytesIO(data))
        return tar_path

    def test_dotdot_traversal_rejected(self, tmp_path: Path):
        """Tar with ../path entry → ValueError."""
        tar = self._make_tar(tmp_path, {
            "../../etc/passwd.flac": b"evil",
        })
        dest = tmp_path / "out"
        with pytest.raises(ValueError, match="Path traversal"):
            extract_archive(tar, dest)

    def test_absolute_path_rejected(self, tmp_path: Path):
        """Tar with absolute-path entry → ValueError."""
        tar = self._make_tar(tmp_path, {
            "/etc/passwd.flac": b"evil",
        })
        dest = tmp_path / "out"
        with pytest.raises(ValueError, match="Path traversal"):
            extract_archive(tar, dest)

    def test_dotdot_in_subdir_rejected(self, tmp_path: Path):
        """Tar with sub-dir/../../escape.flac → ValueError."""
        tar = self._make_tar(tmp_path, {
            "subdir/../../escape.flac": b"evil",
        })
        dest = tmp_path / "out"
        with pytest.raises(ValueError, match="Path traversal"):
            extract_archive(tar, dest)

    def test_safe_tar_extracts_normally(self, tmp_path: Path):
        """Tar with only safe paths extracts without error."""
        tar = self._make_tar(tmp_path, {
            "music/song.flac": b"audio-data",
            "music/album/track.flac": b"more-audio",
        })
        dest = tmp_path / "out"
        result = extract_archive(tar, dest)
        assert len(result) == 2
        for p in result:
            assert p.exists()

    def test_tar_filter_data_kwarg_applied(self, tmp_path: Path):
        """On Python >= 3.12, _TAR_EXTRACT_KWARGS includes filter='data'."""
        from app.import_upload.extract import _TAR_EXTRACT_KWARGS
        if sys.version_info >= (3, 12):
            assert _TAR_EXTRACT_KWARGS.get("filter") == "data"
        else:
            assert _TAR_EXTRACT_KWARGS == {}

    def test_tar_member_rejects_link_attack(self, tmp_path: Path):
        """Tar with a hardlink entry is skipped (symlink/link guard)."""
        tar_path = tmp_path / "link_attack.tar.gz"
        import io
        with tarfile.open(tar_path, "w:gz") as tf:
            # Add a normal audio file first
            info = tarfile.TarInfo(name="safe.flac")
            data = b"good"
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
            # Add a hardlink trying to reference /etc/passwd
            link_info = tarfile.TarInfo(name="link.flac")
            link_info.type = tarfile.LNKTYPE
            link_info.linkname = "/etc/passwd"
            link_info.size = 0
            tf.addfile(link_info)

        dest = tmp_path / "out"
        result = extract_archive(tar_path, dest)
        # Hardlink is skipped (symlink/link guard), only safe.flac extracted
        assert len(result) == 1
        assert result[0].name == "safe.flac"
