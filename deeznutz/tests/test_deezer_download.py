"""Tests for Blowfish decrypt helpers in app.deezer.downloader."""

from __future__ import annotations

import hashlib
import os

import pytest
from Crypto.Cipher import Blowfish

from app.deezer.downloader import _derive_blowfish_key, _decrypt_chunk


def test_derive_blowfish_key_known():
    """Known-answer test: derive key from track_id "1" using hashlib directly."""
    track_id = "1"
    md5 = hashlib.md5(track_id.encode()).hexdigest()
    # Build expected key: XOR two 16-char hex halves
    expected = bytes(
        a ^ b
        for a, b in zip(
            bytes.fromhex(md5[:16]),
            bytes.fromhex(md5[16:]),
        )
    )
    assert _derive_blowfish_key(track_id) == expected


def test_decrypt_chunk_roundtrip():
    """Encrypt then decrypt a chunk — verify we get the original plaintext."""
    key = _derive_blowfish_key("1")
    iv = b"\x00" * 8
    plaintext = os.urandom(4096)

    # Encrypt the first 2048 bytes with Blowfish/CBC (simulating Deezer format)
    cipher = Blowfish.new(key, Blowfish.MODE_CBC, iv=iv)
    encrypted_head = cipher.encrypt(plaintext[:2048])
    # Deezer chunk format: encrypted head + plaintext tail
    chunk = encrypted_head + plaintext[2048:]

    decrypted = _decrypt_chunk(chunk, key, iv=iv)
    assert decrypted == plaintext


def test_decrypt_chunk_too_short():
    """Data shorter than 2048 bytes raises ValueError."""
    key = _derive_blowfish_key("1")
    with pytest.raises(ValueError, match="at least 2048 bytes"):
        _decrypt_chunk(b"\x00" * 1024, key)


def test_decrypt_zero_block():
    """Encrypt b'\\x00' * 4096 with key from track_id '1', decrypt, verify all zero."""
    key = _derive_blowfish_key("1")
    iv = b"\x00" * 8
    plaintext = b"\x00" * 4096

    # Encrypt first 2048 bytes (zeros encrypted with zeros IV → non-zero ciphertext)
    cipher = Blowfish.new(key, Blowfish.MODE_CBC, iv=iv)
    encrypted_head = cipher.encrypt(plaintext[:2048])
    chunk = encrypted_head + plaintext[2048:]

    decrypted = _decrypt_chunk(chunk, key, iv=iv)
    assert decrypted == b"\x00" * 4096
