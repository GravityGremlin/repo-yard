"""Tests for the import upload feature — routes, extraction, and job lifecycle.

Uses the ``app_client`` fixture from conftest.py for route tests and
``tmp_path`` for isolated extraction tests.
"""

from __future__ import annotations

import io
import tarfile
import zipfile

import pytest

from app.import_upload.extract import extract_archive


# ── Route Validation Tests ─────────────────────────────────────────


def test_upload_no_file(app_client):
    """POST /import/upload with no files → 400"""
    resp = app_client.post("/import/upload", data={}, content_type="multipart/form-data")
    assert resp.status_code == 400
    assert resp.get_json()["error"] == "No files uploaded"


def test_upload_bad_extension(app_client, tmp_path):
    """POST /import/upload with .txt file → 400"""
    txt = tmp_path / "test.txt"
    txt.write_text("not audio")
    with open(txt, "rb") as f:
        data = {"files": (f, "test.txt")}
        resp = app_client.post("/import/upload", data=data, content_type="multipart/form-data")
    assert resp.status_code == 400


def test_upload_single_flac(app_client, tmp_path):
    """POST /import/upload with a single FLAC file → 200, returns job_id"""
    flac = tmp_path / "test.flac"
    flac.write_bytes(b"fake flac data")
    with open(flac, "rb") as f:
        data = {"files": (f, "test.flac")}
        resp = app_client.post("/import/upload", data=data, content_type="multipart/form-data")
    assert resp.status_code == 200
    json_data = resp.get_json()
    assert "job_id" in json_data
    assert json_data["files"] == 1


def test_upload_zip(app_client, tmp_path):
    """POST /import/upload with a valid ZIP → 200"""
    zip_path = tmp_path / "test.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("track.flac", b"fake flac")
    with open(zip_path, "rb") as f:
        data = {"files": (f, "test.zip")}
        resp = app_client.post("/import/upload", data=data, content_type="multipart/form-data")
    assert resp.status_code == 200
    assert "job_id" in resp.get_json()


def test_upload_mixed_archive_and_audio(app_client, tmp_path):
    """POST /import/upload with zip + flac → 400 (mixed rejected)"""
    zip_path = tmp_path / "test.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("track.flac", b"fake")
    flac = tmp_path / "singles.flac"
    flac.write_bytes(b"fake")
    with open(zip_path, "rb") as f1, open(flac, "rb") as f2:
        data = {"files": [(f1, "test.zip"), (f2, "singles.flac")]}
        resp = app_client.post("/import/upload", data=data, content_type="multipart/form-data")
    assert resp.status_code == 400


def test_upload_multiple_audio_files(app_client):
    """POST /import/upload with 3 FLAC files → 200"""
    data = {"files": [(io.BytesIO(b"fake"), f"track{i}.flac") for i in range(3)]}
    resp = app_client.post("/import/upload", data=data, content_type="multipart/form-data")
    assert resp.status_code == 200
    json_data = resp.get_json()
    assert json_data["files"] == 3


# ── Page / endpoint reachability ────────────────────────────────────


def test_upload_page_renders(app_client):
    """GET /import/ returns 200"""
    resp = app_client.get("/import/")
    assert resp.status_code == 200


def test_jobs_page_renders(app_client):
    """GET /import/jobs returns 200 with HTML"""
    resp = app_client.get("/import/jobs")
    assert resp.status_code == 200


def test_jobs_list_partial(app_client):
    """GET /import/jobs/list returns 200"""
    resp = app_client.get("/import/jobs/list")
    assert resp.status_code == 200


def test_sse_stream_not_found(app_client):
    """GET /import/stream/<id> for non-existent job returns 404"""
    resp = app_client.get("/import/stream/nonexistent")
    assert resp.status_code == 404


# ── Job lifecycle (upload → cancel → delete) ─────────────────────


def test_enqueue_and_cancel(app_client, monkeypatch):
    """Enqueue a job via upload, then cancel it."""
    # Suppress background worker so it does not race with cancel.
    monkeypatch.setattr(
        "app.import_upload.controller._run_import", lambda _job_id: None
    )

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("track.flac", b"fake")
    buf.seek(0)

    data = {"files": (buf, "test.zip")}
    resp = app_client.post("/import/upload", data=data,
                           content_type="multipart/form-data")
    assert resp.status_code == 200
    job_id = resp.get_json()["job_id"]

    resp2 = app_client.post(f"/import/jobs/{job_id}/cancel")
    assert resp2.status_code == 200
    assert resp2.get_json()["status"] == "cancelled"


def test_delete_job(app_client, monkeypatch):
    """Upload then cancel then delete the job."""
    monkeypatch.setattr(
        "app.import_upload.controller._run_import", lambda _job_id: None
    )

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("track.flac", b"fake")
    buf.seek(0)

    data = {"files": (buf, "test.zip")}
    resp = app_client.post("/import/upload", data=data,
                           content_type="multipart/form-data")
    assert resp.status_code == 200
    job_id = resp.get_json()["job_id"]

    # Must reach a terminal state before delete.
    app_client.post(f"/import/jobs/{job_id}/cancel")
    resp3 = app_client.post(f"/import/jobs/{job_id}/delete")
    assert resp3.status_code == 200
    assert resp3.get_json()["status"] == "deleted"


# ── Extraction — ZIP ────────────────────────────────────────────────


def test_extract_zip(tmp_path):
    """Extract a valid ZIP with FLAC files → returns paths."""
    archive = tmp_path / "test.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("music/track.flac", b"fake flac data")
    dest = tmp_path / "out"
    result = extract_archive(archive, dest)
    assert len(result) == 1
    assert result[0].name == "track.flac"


def test_extract_non_audio_skipped(tmp_path):
    """ZIP with .txt files → skipped, only audio extracted."""
    archive = tmp_path / "mixed.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("readme.txt", b"hello")
        zf.writestr("song.flac", b"fake flac")
    dest = tmp_path / "out"
    result = extract_archive(archive, dest)
    assert len(result) == 1
    assert result[0].suffix == ".flac"


def test_extract_empty_archive(tmp_path):
    """ZIP with no audio files → returns empty list."""
    archive = tmp_path / "empty.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("readme.txt", b"hello")
    dest = tmp_path / "out"
    result = extract_archive(archive, dest)
    assert len(result) == 0


def test_extract_path_traversal_zip(tmp_path):
    """ZIP with ../ member → raises ValueError."""
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("../../../etc/passwd.flac", b"bad")
    with pytest.raises(ValueError, match="traversal"):
        extract_archive(archive, tmp_path / "out")


def test_extract_corrupt_zip(tmp_path):
    """Corrupt ZIP → raises ValueError."""
    archive = tmp_path / "corrupt.zip"
    archive.write_bytes(b"not a zip file at all")
    with pytest.raises(ValueError, match="[Cc]orrupt|[Ii]nvalid"):
        extract_archive(archive, tmp_path / "out")


# ── Extraction — TAR / TAR.GZ ───────────────────────────────────────


def test_extract_tar(tmp_path):
    """Extract a valid .tar → returns paths."""
    archive = tmp_path / "test.tar"
    flac = tmp_path / "track.flac"
    flac.write_bytes(b"fake")
    with tarfile.open(archive, "w") as tf:
        tf.add(flac, arcname="track.flac")
    dest = tmp_path / "out"
    result = extract_archive(archive, dest)
    assert len(result) == 1


def test_extract_tar_gz(tmp_path):
    """Extract a valid tar.gz with FLAC files → returns paths."""
    archive = tmp_path / "test.tar.gz"
    flac = tmp_path / "track.flac"
    flac.write_bytes(b"fake")
    with tarfile.open(archive, "w:gz") as tf:
        tf.add(flac, arcname="track.flac")
    dest = tmp_path / "out"
    result = extract_archive(archive, dest)
    assert len(result) == 1


def test_extract_path_traversal_tar(tmp_path):
    """TAR with ../ member → raises ValueError."""
    archive = tmp_path / "bad.tar"
    info = tarfile.TarInfo(name="../../../etc/passwd.flac")
    info.size = 4
    with tarfile.open(archive, "w") as tf:
        tf.addfile(info, io.BytesIO(b"bad!"))
    with pytest.raises(ValueError, match="traversal"):
        extract_archive(archive, tmp_path / "out")


def test_extract_symlink_skipped(tmp_path):
    """TAR with symlink → symlink silently skipped."""
    archive = tmp_path / "sym.tar"
    info = tarfile.TarInfo(name="link")
    info.type = tarfile.SYMTYPE
    info.linkname = "/etc/passwd"
    with tarfile.open(archive, "w") as tf:
        tf.addfile(info)
    dest = tmp_path / "out"
    result = extract_archive(archive, dest)
    assert len(result) == 0


# ── Extraction — edge cases ────────────────────────────────────────


def test_extract_unsupported_format(tmp_path):
    """Unsupported extension (.rar) → raises ValueError."""
    archive = tmp_path / "test.rar"
    archive.write_bytes(b"junk")
    with pytest.raises(ValueError, match="Unsupported"):
        extract_archive(archive, tmp_path / "out")
