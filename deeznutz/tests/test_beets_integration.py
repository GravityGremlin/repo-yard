"""Tests for beets import integration — Component D + Task F6.

Mock ``subprocess.run`` to isolate from the real beets CLI.
"""

from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.download.beets_integration import BeetsResult, beets_import_album


class FakeAudio(dict):
    def save(self) -> None:
        pass


def _run_import(album: Path, beets_dir: Path, tags: dict[str, list[str]] | None):
    audio = FakeAudio(tags or {})
    with (
        patch("app.download.beets_integration.BEETS_DIR", beets_dir),
        patch("app.download.beets_integration.MutagenFile", return_value=audio),
        patch("app.download.beets_integration._album_in_library", return_value=None),
        patch(
            "app.download.beets_integration.subprocess.run",
            return_value=MagicMock(returncode=0, stdout="", stderr=""),
        ) as run,
        patch("app.download.beets_integration._recent_beets_imports", return_value=[]),
    ):
        return beets_import_album(album), run


def test_isrc_already_in_library_skips_import(tmp_path: Path) -> None:
    album = tmp_path / "Artist" / "Album"
    album.mkdir(parents=True)
    (album / "01 - Track.flac").touch()
    beets_dir = tmp_path / "beets"
    beets_dir.mkdir()
    with sqlite3.connect(beets_dir / "library.db") as conn:
        conn.execute("CREATE TABLE items (isrc TEXT)")
        conn.execute("INSERT INTO items VALUES (?)", ("USABC1234567",))

    result, run = _run_import(
        album, beets_dir, {"title": ["Track"], "isrc": ["USABC1234567"]}
    )

    assert result.ok and result.skipped_duplicate
    run.assert_not_called()


def test_isrc_absent_from_library_imports(tmp_path: Path) -> None:
    album = tmp_path / "Artist" / "Album"
    album.mkdir(parents=True)
    (album / "01 - Track.flac").touch()
    beets_dir = tmp_path / "beets"
    beets_dir.mkdir()
    with sqlite3.connect(beets_dir / "library.db") as conn:
        conn.execute("CREATE TABLE items (isrc TEXT)")

    result, run = _run_import(
        album, beets_dir, {"title": ["Track"], "isrc": ["USABC1234567"]}
    )

    assert result.ok and not result.skipped_duplicate
    run.assert_called_once()


def test_missing_beets_database_imports(tmp_path: Path) -> None:
    album = tmp_path / "Artist" / "Album"
    album.mkdir(parents=True)
    (album / "01 - Track.flac").touch()

    result, run = _run_import(
        album, tmp_path / "missing", {"title": ["Track"], "isrc": ["USABC1234567"]}
    )

    assert result.ok and not result.skipped_duplicate
    run.assert_called_once()


def test_empty_title_and_filename_is_rejected(tmp_path: Path) -> None:
    album = tmp_path / "Artist" / "Album"
    album.mkdir(parents=True)
    staged = album / "00 -.opus"
    staged.touch()

    result, run = _run_import(album, tmp_path / "missing", {})

    assert result.ok
    assert result.rejected_files == [album.parent / "_rejected" / staged.name]
    assert not staged.exists()
    run.assert_not_called()


def test_filename_title_is_accepted(tmp_path: Path) -> None:
    album = tmp_path / "Artist" / "Album"
    album.mkdir(parents=True)
    staged = album / "01 - Carousel.opus"
    staged.touch()

    result, run = _run_import(album, tmp_path / "missing", {})

    assert result.ok and result.rejected_files == []
    assert staged.exists()
    run.assert_called_once()


def test_tagged_file_is_accepted(tmp_path: Path) -> None:
    album = tmp_path / "Artist" / "Album"
    album.mkdir(parents=True)
    staged = album / "track.opus"
    staged.touch()

    result, run = _run_import(album, tmp_path / "missing", {"title": ["Track"]})

    assert result.ok and result.rejected_files == []
    assert staged.exists()
    run.assert_called_once()


# ── Component D1 — 5 beets-wrapper tests ───────────────────────────

def test_beets_import_album_success(tmp_path: Path) -> None:
    """Album dir with a flac → subprocess returns rc=0 → result ok, files match."""
    album = tmp_path / "Artist" / "Album (2020)"
    album.mkdir(parents=True)
    (album / "01 Track.flac").touch()

    expected = [Path("/music/Artist/Album/01 - Track.opus")]

    with (
        patch("app.download.beets_integration._album_in_library", return_value=None),
        patch(
            "app.download.beets_integration.subprocess.run",
            return_value=MagicMock(returncode=0, stdout="", stderr=""),
        ),
        patch(
            "app.download.beets_integration._recent_beets_imports",
            return_value=expected,
        ),
    ):
        r = beets_import_album(album)

    assert r.ok
    assert r.imported_files == expected
    assert not r.skipped_duplicate
    assert not r.removed_existing
    assert r.error is None


def test_beets_import_album_empty_dir(tmp_path: Path) -> None:
    """Empty dir → returns early with ok=True, files=[], no subprocess call."""
    empty = tmp_path / "empty"
    empty.mkdir()
    r = beets_import_album(empty)
    assert r.ok
    assert r.imported_files == []
    assert not r.skipped_duplicate


def test_beets_import_album_failure(tmp_path: Path) -> None:
    """subprocess rc=1 stderr='boom' → ok=False, 'boom' in error."""
    album = tmp_path / "Artist" / "Bad"
    album.mkdir(parents=True)
    (album / "01.flac").touch()

    with (
        patch("app.download.beets_integration._album_in_library", return_value=None),
        patch(
            "app.download.beets_integration.subprocess.run",
            return_value=MagicMock(returncode=1, stdout="", stderr="boom"),
        ),
    ):
        r = beets_import_album(album)

    assert not r.ok
    assert "boom" in (r.error or "")


def test_beets_import_album_timeout(tmp_path: Path) -> None:
    """subprocess.TimeoutExpired → ok=False, 'timed out' in error."""
    album = tmp_path / "Artist" / "Slow"
    album.mkdir(parents=True)
    (album / "01.flac").touch()

    with (
        patch("app.download.beets_integration._album_in_library", return_value=None),
        patch(
            "app.download.beets_integration.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="beet", timeout=1),
        ),
    ):
        r = beets_import_album(album)

    assert not r.ok
    assert "timed out" in (r.error or "")


def test_beets_import_album_beet_missing(tmp_path: Path) -> None:
    """FileNotFoundError → ok=False, 'not found' in error."""
    album = tmp_path / "Artist" / "Miss"
    album.mkdir(parents=True)
    (album / "01.flac").touch()

    with (
        patch("app.download.beets_integration._album_in_library", return_value=None),
        patch(
            "app.download.beets_integration.subprocess.run",
            side_effect=FileNotFoundError("no beet"),
        ),
    ):
        r = beets_import_album(album)

    assert not r.ok
    assert "not found" in (r.error or "")


# ── Task F6 — 3 override-path tests ────────────────────────────────

def test_beets_import_duplicate_skip_by_default(tmp_path: Path) -> None:
    """Album in library, override=False → skipped_duplicate=True, no subprocess call."""
    album = tmp_path / "Static-X" / "Shadow Zone (2003)"
    album.mkdir(parents=True)
    (album / "01.flac").touch()

    with (
        patch(
            "app.download.beets_integration._album_in_library",
            return_value="album:'Shadow Zone' albumartist:'Static-X'",
        ),
        patch("app.download.beets_integration.subprocess.run") as mock_run,
    ):
        r = beets_import_album(album, override=False)

    assert r.ok
    assert r.skipped_duplicate
    assert not r.removed_existing
    mock_run.assert_not_called()


def test_beets_import_duplicate_override_removes_then_imports(
    tmp_path: Path,
) -> None:
    """Album in library, override=True → removes then imports, both cmds recorded."""
    album = tmp_path / "Static-X" / "Shadow Zone (2003)"
    album.mkdir(parents=True)
    (album / "01.flac").touch()

    runs: list[list[str]] = []

    def fake_run(cmd: list[str], **kw: object) -> MagicMock:
        runs.append(cmd)
        return MagicMock(returncode=0, stdout="", stderr="")

    with (
        patch(
            "app.download.beets_integration._album_in_library",
            return_value="album:'Shadow Zone' albumartist:'Static-X'",
        ),
        patch(
            "app.download.beets_integration.subprocess.run",
            side_effect=fake_run,
        ),
        patch(
            "app.download.beets_integration._recent_beets_imports",
            return_value=[
                Path("/music/Static-X/Shadow Zone/01 - Track.opus")
            ],
        ),
    ):
        r = beets_import_album(album, override=True)

    assert r.ok
    assert r.removed_existing
    assert not r.skipped_duplicate
    assert any("remove" in c for c in runs), f"remove not found in: {runs}"
    assert any("import" in c for c in runs), f"import not found in: {runs}"


def test_beets_import_override_when_not_in_library_is_just_normal_import(
    tmp_path: Path,
) -> None:
    """override=True but album NOT in library → normal import, removed_existing=False."""
    album = tmp_path / "New" / "Artist"
    album.mkdir(parents=True)
    (album / "01.flac").touch()

    expected = [Path("/music/New/Artist/01 - T.opus")]

    with (
        patch("app.download.beets_integration._album_in_library", return_value=None),
        patch(
            "app.download.beets_integration.subprocess.run",
            return_value=MagicMock(returncode=0, stdout="", stderr=""),
        ),
        patch(
            "app.download.beets_integration._recent_beets_imports",
            return_value=expected,
        ),
    ):
        r = beets_import_album(album, override=True)

    assert r.ok
    assert not r.removed_existing
    assert r.imported_files == expected
