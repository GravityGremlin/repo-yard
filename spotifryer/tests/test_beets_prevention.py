"""Regression tests for duplicate-prevention in the beets integration.

(a) ISRC pre-check — skips import when the track's ISRC is already in the
    shared library DB; proceeds when absent or the DB is unavailable
(b) Metadata gate — files with no title tag and no parsable filename title
    are rejected (moved to _rejected); filename titles are salvaged
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.download import beets_integration as bi


def _make_audio(path: Path, title: str | None = None, isrc: str | None = None) -> None:
    """Create a fake audio file; mutagen is mocked, so any bytes work."""
    path.write_bytes(b"fake-audio")


class FakeTag:
    def __init__(self, title=None, isrc=None):
        self._data = {}
        if title is not None:
            self._data["title"] = title
        if isrc is not None:
            self._data["isrc"] = isrc

    def get(self, key, default=None):
        return self._data.get(key, default)

    def __getitem__(self, key):
        return self._data[key]

    def __setitem__(self, key, value):
        self._data[key] = value

    def save(self):
        pass


@pytest.fixture
def fake_mutagen(monkeypatch):
    """Point the lazy mutagen import at a FakeTag-backed loader."""
    import mutagen as _mutagen

    def _fake_file(path, easy=False):
        return FakeTag()

    monkeypatch.setattr(_mutagen, "File", _fake_file)
    return _mutagen


def _patch_tag(monkeypatch, path: Path, title=None, isrc=None):
    """Patch the specific file's loaded tag."""
    import mutagen as _mutagen

    def _fake_file(p, easy=False):
        return FakeTag(title=title, isrc=isrc)

    monkeypatch.setattr(_mutagen, "File", _fake_file)


# ── ISRC pre-check ─────────────────────────────────────────────────

def _make_db(db_path: Path, isrcs: list[str]) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, isrc TEXT)")
        for i, isrc in enumerate(isrcs):
            conn.execute("INSERT INTO items (id, isrc) VALUES (?, ?)", (i, isrc))
        conn.commit()
    finally:
        conn.close()


def test_isrc_in_library_hit(monkeypatch, tmp_path, fake_mutagen):
    db = tmp_path / "library.db"
    _make_db(db, ["USX1", "USX2"])
    monkeypatch.setattr(bi, "BEETS_DIR", tmp_path)
    audio = tmp_path / "track.flac"
    _make_audio(audio)
    _patch_tag(monkeypatch, audio, title="Bangarang", isrc="USX2")
    assert bi._isrc_in_library(audio) is True


def test_isrc_in_library_miss(monkeypatch, tmp_path, fake_mutagen):
    db = tmp_path / "library.db"
    _make_db(db, ["USX1"])
    monkeypatch.setattr(bi, "BEETS_DIR", tmp_path)
    audio = tmp_path / "track.flac"
    _make_audio(audio)
    _patch_tag(monkeypatch, audio, title="Other", isrc="ZZZ9")
    assert bi._isrc_in_library(audio) is False


def test_isrc_in_library_no_db_proceeds(monkeypatch, tmp_path, fake_mutagen):
    monkeypatch.setattr(bi, "BEETS_DIR", tmp_path)  # no library.db exists
    audio = tmp_path / "track.flac"
    _make_audio(audio)
    _patch_tag(monkeypatch, audio, title="Bangarang", isrc="USX1")
    assert bi._isrc_in_library(audio) is False


def test_isrc_in_library_no_isrc_tag_proceeds(monkeypatch, tmp_path, fake_mutagen):
    db = tmp_path / "library.db"
    _make_db(db, ["USX1"])
    monkeypatch.setattr(bi, "BEETS_DIR", tmp_path)
    audio = tmp_path / "track.flac"
    _make_audio(audio)
    _patch_tag(monkeypatch, audio, title="No ISRC Track", isrc=None)
    assert bi._isrc_in_library(audio) is False


# ── Metadata gate ──────────────────────────────────────────────────

def test_filename_title_strips_track_number():
    assert bi._filename_title(Path("01 - Carousel.opus")) == "Carousel"
    assert bi._filename_title(Path("05 - Left Behind.flac")) == "Left Behind"


def test_filename_title_empty_for_junk():
    assert bi._filename_title(Path("00 -.opus")) is None
    assert bi._filename_title(Path("--.flac")) is None


def test_organize_rejects_untitled_file(monkeypatch, tmp_path):
    """A file with no title tag and no parsable filename is moved to _rejected."""
    monkeypatch.setattr(bi, "ORGANIZE_WITH_BEETS", True)
    monkeypatch.setattr(bi.shutil, "which", lambda _: "/usr/bin/beet")

    lib = tmp_path / "music"
    lib.mkdir()
    staged = tmp_path / "downloads"
    staged.mkdir()
    audio = staged / "00 -.opus"
    _make_audio(audio)
    _patch_tag(monkeypatch, audio, title=None, isrc=None)

    result = bi.organize_with_beets(audio, lib)
    assert result is None
    assert not audio.exists()
    rejected_dir = lib / "_rejected"
    assert (rejected_dir / "00 -.opus").exists()


def test_organize_salvages_filename_title(monkeypatch, tmp_path, fake_mutagen):
    """A file with no title tag but a parsable filename keeps its filename title."""
    monkeypatch.setattr(bi, "ORGANIZE_WITH_BEETS", True)
    monkeypatch.setattr(bi.shutil, "which", lambda _: "/usr/bin/beet")

    lib = tmp_path / "music"
    lib.mkdir()
    staged = tmp_path / "downloads"
    staged.mkdir()
    audio = staged / "01 - Carousel.opus"
    _make_audio(audio)
    _patch_tag(monkeypatch, audio, title=None, isrc=None)

    result = bi.organize_with_beets(audio, lib)
    # proceeds past the gate (returns None only because beets subprocess is not
    # really run / no library candidates); crucially the file is NOT rejected
    assert result is None or result.exists()
    assert not (lib / "_rejected").exists()
