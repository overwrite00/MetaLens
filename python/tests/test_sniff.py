"""Format recognition from content (core/sniff.py)."""
import struct
import zipfile
from pathlib import Path

import pytest

from core import sniff


def _write(tmp_path: Path, name: str, data: bytes) -> Path:
    path = tmp_path / name
    path.write_bytes(data)
    return path


def _ftyp(brand: bytes) -> bytes:
    return struct.pack(">I", 24) + b"ftyp" + brand + b"\x00\x00\x00\x00" + b"isom" + b"mp42"


@pytest.mark.parametrize("data, expected", [
    (b"\xff\xd8\xff\xe0" + bytes(60), "jpeg"),
    (b"\x89PNG\r\n\x1a\n" + bytes(56), "png"),
    (b"GIF89a" + bytes(58), "gif"),
    (b"II*\x00" + bytes(60), "tiff"),
    (b"MM\x00*" + bytes(60), "tiff"),
    (b"RIFF\x00\x00\x00\x00WEBPVP8 " + bytes(40), "webp"),
    (b"RIFF\x00\x00\x00\x00AVI LIST" + bytes(40), "avi"),
    (b"RIFF\x00\x00\x00\x00WAVEfmt " + bytes(40), "wav"),
    (b"FORM\x00\x00\x00\x00AIFFCOMM" + bytes(40), "aiff"),
    (_ftyp(b"heic"), "heif"),
    (_ftyp(b"avif"), "avif"),
    (_ftyp(b"crx "), "cr3"),
    (_ftyp(b"M4A "), "m4a"),
    (_ftyp(b"qt  "), "mov"),
    (_ftyp(b"isom"), "mp4"),
    (b"\x1aE\xdf\xa3" + bytes(60), "mkv"),
    (b"fLaC" + bytes(60), "flac"),
    (b"OggS" + bytes(60), "ogg"),
    (b"%PDF-1.7\n" + bytes(50), "pdf"),
    (bytes.fromhex("d0cf11e0a1b11ae1") + bytes(56), "ole"),
    (b"ID3\x04\x00" + bytes(59), "mp3"),
    (b"\xff\xfb\x90\x00" + bytes(60), "mp3"),
])
def test_signatures(tmp_path, data, expected):
    assert sniff.detect(_write(tmp_path, "sample", data)) == expected


@pytest.mark.parametrize("data", [
    b"", b"II", b"hello world, this is text", b"\x13\x37" * 32,
    b"II text that starts with the byte order mark letters",
    b"\xff\xfb\xf0\x00" + bytes(60),      # bitrate index 15 / free: not an MPEG frame
])
def test_not_recognized(tmp_path, data):
    assert sniff.detect(_write(tmp_path, "sample", data)) is None


def test_zip_packages(tmp_path):
    ooxml = tmp_path / "pkg"
    with zipfile.ZipFile(ooxml, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
    plain = tmp_path / "plain"
    with zipfile.ZipFile(plain, "w") as z:
        z.writestr("readme.txt", "x")
    assert sniff.detect(ooxml) == "ooxml"
    assert sniff.detect(plain) is None


def test_missing_file_is_not_recognized(tmp_path):
    assert sniff.detect(tmp_path / "absent") is None


def test_handler_names():
    assert sniff.handler_name("jpeg") == "image"
    assert sniff.handler_name("mkv") == "video"
    assert sniff.handler_name("heif") == "heif" and sniff.handler_name("avif") == "heif"
    assert sniff.handler_name("cr3") == "image" and sniff.handler_name("raf") == "image"
    assert sniff.handler_name(None) is None


def test_only_the_head_is_read(tmp_path, monkeypatch):
    path = _write(tmp_path, "big", b"\xff\xd8\xff" + bytes(10_000))
    seen = []
    real_open = open

    def spy(file, mode="r", *a, **k):
        stream = real_open(file, mode, *a, **k)
        original = stream.read
        stream.read = lambda n=-1: (seen.append(n), original(n))[1]
        return stream

    monkeypatch.setattr("builtins.open", spy)
    assert sniff.detect(path) == "jpeg"
    assert seen == [sniff.HEAD_BYTES]
