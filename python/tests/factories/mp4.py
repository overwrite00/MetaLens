"""MP4 container samples (m4a, mp4, mov).

Owned by phase F2C. Files are minimal ISO-BMFF built in Python: ``ftyp`` +
``moov`` (only ``mvhd`` v0) + ``mdat`` with a fixed payload, then tagged with
mutagen (``MP4.add_tags``). ``mdat_payload`` extracts the media bytes so tests
can check that tag writes never touch them.

Register with `@sample("name", ...)`; see `tests/factories/__init__.py`.
"""
from __future__ import annotations

import struct
from pathlib import Path

from . import registrar

SAMPLES: list = []
sample = registrar(SAMPLES, __name__)

MDAT_PAYLOAD = bytes(range(256)) * 4


def atom(name: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", 8 + len(payload)) + name + payload


def mvhd(timescale: int = 1000, duration: int = 2500) -> bytes:
    body = bytes(4)                                         # version 0, flags
    body += struct.pack(">IIII", 0, 0, timescale, duration)  # ctime, mtime, scale, duration
    body += struct.pack(">IH", 0x00010000, 0x0100) + bytes(10)  # rate, volume, reserved
    body += struct.pack(">9I", 0x10000, 0, 0, 0, 0x10000, 0, 0, 0, 0x40000000)  # matrix
    body += bytes(24) + struct.pack(">I", 2)                # pre_defined, next_track_ID
    return atom(b"mvhd", body)


def write_mp4(path: Path, brand: bytes = b"M4A ") -> Path:
    ftyp = atom(b"ftyp", brand + struct.pack(">I", 0x200) + brand + b"isom")
    path.write_bytes(ftyp + atom(b"moov", mvhd()) + atom(b"mdat", MDAT_PAYLOAD))
    return path


def mdat_payload(path: Path) -> bytes:
    """Payload of the top-level ``mdat`` atom."""
    data = Path(path).read_bytes()
    pos = 0
    while pos + 8 <= len(data):
        size, name = struct.unpack(">I4s", data[pos:pos + 8])
        if name == b"mdat":
            return data[pos + 8:pos + size]
        pos += size
    raise AssertionError("no mdat atom")


def png_bytes() -> bytes:
    return b"\x89PNG\r\n\x1a\n" + bytes(range(40))


def add_full_tags(path: Path) -> None:
    from mutagen.mp4 import MP4, AtomDataType, MP4Cover, MP4FreeForm
    mp4 = MP4(path)
    mp4.add_tags()
    t = mp4.tags
    t["\xa9nam"] = ["Title"]
    t["\xa9ART"] = ["Artist A", "Artist B"]
    t["\xa9day"] = ["2026"]
    t["trkn"] = [(3, 10)]
    t["disk"] = [(1, 2)]
    t["tmpo"] = [120]
    t["cpil"] = False
    t["covr"] = [MP4Cover(png_bytes(), imageformat=MP4Cover.FORMAT_PNG)]
    t["----:com.apple.iTunes:MOOD"] = [MP4FreeForm("calm é".encode(), dataformat=AtomDataType.UTF8)]
    t["----:com.apple.iTunes:BLOB"] = [MP4FreeForm(b"\x00\x01\x02", dataformat=AtomDataType.IMPLICIT)]
    mp4.save()


@sample("m4a_tags")
def m4a_tags(tmp_path: Path) -> Path:
    path = write_mp4(tmp_path / "song.m4a")
    add_full_tags(path)
    return path


@sample("mp4_tags")
def mp4_tags(tmp_path: Path) -> Path:
    path = write_mp4(tmp_path / "clip.mp4", brand=b"isom")
    add_full_tags(path)
    return path


@sample("mov")
def mov(tmp_path: Path) -> Path:
    return write_mp4(tmp_path / "movie.mov", brand=b"qt  ")
