"""Unit tests for attachment magic-byte sniffing and validation (all formats)."""

from __future__ import annotations

import pytest

from config import MAX_IMAGE_BYTES, AttachmentError, sniff_media_type, validate_image

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 16
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
GIF87 = b"GIF87a" + b"\x00" * 16
GIF89 = b"GIF89a" + b"\x00" * 16
WEBP = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"\x00" * 12


def test_sniff_all_four_formats() -> None:
    assert sniff_media_type(JPEG) == "image/jpeg"
    assert sniff_media_type(PNG) == "image/png"
    assert sniff_media_type(GIF87) == "image/gif"
    assert sniff_media_type(GIF89) == "image/gif"
    assert sniff_media_type(WEBP) == "image/webp"


def test_sniff_rejects_non_image() -> None:
    assert sniff_media_type(b"this is plainly not an image") is None


def test_sniff_short_buffer_guard() -> None:
    # Fewer than 12 bytes must never be sniffed (avoids index errors / false hits).
    assert sniff_media_type(b"\xff\xd8\xff") is None


def test_validate_accepts_each_format() -> None:
    for data, media_type in [
        (JPEG, "image/jpeg"),
        (PNG, "image/png"),
        (GIF89, "image/gif"),
        (WEBP, "image/webp"),
    ]:
        assert validate_image(data) == media_type


def test_validate_rejects_empty() -> None:
    with pytest.raises(AttachmentError):
        validate_image(b"")


def test_validate_rejects_non_image() -> None:
    with pytest.raises(AttachmentError):
        validate_image(b"hello world, definitely not an image payload")


def test_validate_rejects_oversize() -> None:
    oversize = b"\x89PNG\r\n\x1a\n" + b"\x00" * (MAX_IMAGE_BYTES + 1)
    with pytest.raises(AttachmentError):
        validate_image(oversize)
