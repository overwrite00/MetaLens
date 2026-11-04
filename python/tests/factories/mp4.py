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


_LOCATION = {"qt:mdta:com.apple.quicktime.location.ISO6709": "+10.0000+020.0000/", "qt:udta:©xyz": "+10.0000+020.0000/"}


@sample("m4a_tags", mutations=_LOCATION)
def m4a_tags(tmp_path: Path) -> Path:
    path = write_mp4(tmp_path / "song.m4a")
    add_full_tags(path)
    return path


@sample("mp4_tags", mutations=_LOCATION)
def mp4_tags(tmp_path: Path) -> Path:
    path = write_mp4(tmp_path / "clip.mp4", brand=b"isom")
    add_full_tags(path)
    return path


@sample("mov", mutations=_LOCATION)
def mov(tmp_path: Path) -> Path:
    return write_mp4(tmp_path / "movie.mov", brand=b"qt  ")


# ── Realistic phone-style movie: video + audio tracks, QuickTime keys, udta, XMP ──

def _full(name: bytes, payload: bytes, version: int = 0, flags: int = 0) -> bytes:
    return atom(name, struct.pack(">I", (version << 24) | flags) + payload)


def mac_seconds(year: int, month: int, day: int, hour: int = 0, minute: int = 0, second: int = 0) -> int:
    from datetime import datetime, timezone
    return int((datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)
                - datetime(1904, 1, 1, tzinfo=timezone.utc)).total_seconds())


def _matrix(rotation: int = 0) -> bytes:
    a, b = {0: (0x10000, 0), 90: (0, 0x10000), 180: (-0x10000, 0), 270: (0, -0x10000)}[rotation]
    return struct.pack(">9i", a, b, 0, -b, a, 0, 0, 0, 0x40000000)


def _tkhd(track_id: int, created: int, width: int, height: int, rotation: int = 0, audio: bool = False) -> bytes:
    body = struct.pack(">IIIII", created, created, track_id, 0, 300300) + bytes(8)
    body += struct.pack(">hhhH", 0, 0, 0x0100 if audio else 0, 0) + _matrix(rotation)
    body += struct.pack(">II", width << 16, height << 16)
    return _full(b"tkhd", body, flags=3)


def _mdhd(created: int, timescale: int, duration: int, lang: str = "und") -> bytes:
    code = 0 if lang == "und" else sum((ord(c) - 0x60) << s for c, s in zip(lang, (10, 5, 0)))
    return _full(b"mdhd", struct.pack(">IIIIHH", created, created, timescale, duration, code, 0))


def _hdlr(kind: bytes, name: bytes) -> bytes:
    return _full(b"hdlr", bytes(4) + kind + bytes(12) + name + b"\x00")


def _video_trak(track_id: int, created: int, chunk_offset: int, rotation: int) -> bytes:
    stsd = _full(b"stsd", struct.pack(">I", 1) + atom(b"avc1", bytes(6) + struct.pack(">H", 1) + bytes(70)))
    stts = _full(b"stts", struct.pack(">III", 1, 300, 1001))
    stsz = _full(b"stsz", struct.pack(">III", 0, 1, 1024))
    stco = _full(b"stco", struct.pack(">II", 1, chunk_offset))
    stbl = atom(b"stbl", stsd + stts + stsz + stco)
    mdia = atom(b"mdia", _mdhd(created, 30000, 300300, "ita") + _hdlr(b"vide", b"VideoHandler") + atom(b"minf", stbl))
    return atom(b"trak", _tkhd(track_id, created, 1920, 1080, rotation) + mdia)


def _audio_trak(track_id: int, created: int, chunk_offset: int) -> bytes:
    entry = bytes(6) + struct.pack(">H", 1) + bytes(8) + struct.pack(">HHHHI", 2, 16, 0, 0, 48000 << 16)
    # ES descriptor: AAC-LC, 48 kHz, stereo (AudioSpecificConfig 0x1190)
    esds = _full(b"esds", bytes.fromhex("03808080190001" "00" "0480808011" "401500000000fa0000fa00"
                                        "058080800211" "90" "068080800102"))
    stsd = _full(b"stsd", struct.pack(">I", 1) + atom(b"mp4a", entry + esds))
    stco = _full(b"stco", struct.pack(">II", 1, chunk_offset))
    stbl = atom(b"stbl", stsd + _full(b"stts", struct.pack(">III", 1, 1, 1024)) + stco)
    mdia = atom(b"mdia", _mdhd(created, 48000, 144000, "eng") + _hdlr(b"soun", b"SoundHandler") + atom(b"minf", stbl))
    return atom(b"trak", _tkhd(track_id, created, 0, 0, audio=True) + mdia)


def _mdta_meta(items: list[tuple[str, int, bytes]], full_box: bool = True) -> bytes:
    """``meta`` with an ``mdta`` handler, ``keys`` and ``ilst``. Phones write it without the
    ISO full-box header (``full_box=False``); mutagen only parses the ISO form."""
    keys = struct.pack(">I", len(items))
    ilst = b""
    for index, (name, kind, value) in enumerate(items, start=1):
        keys += atom(b"mdta", name.encode())
        ilst += struct.pack(">I", 8 + 16 + len(value)) + struct.pack(">I", index) \
            + struct.pack(">I", 16 + len(value)) + b"data" + struct.pack(">II", kind, 0) + value
    return atom(b"meta", (bytes(4) if full_box else b"") + _hdlr(b"mdta", b"") + _full(b"keys", keys) + atom(b"ilst", ilst))


def write_phone_movie(path: Path, full_box_meta: bool = True) -> Path:
    from .image import XMP_PACKET
    ftyp = atom(b"ftyp", b"qt  " + struct.pack(">I", 0) + b"qt  ")
    payload = MDAT_PAYLOAD
    mdat = atom(b"mdat", payload)
    payload_at = len(ftyp) + 8
    created = mac_seconds(2026, 5, 1, 8, 20, 30)
    udta_text = b"+45.4642+009.1900+120.500/"
    udta = atom(b"udta", struct.pack(">I", 12 + len(udta_text)) + "©xyz".encode("latin-1")
                + struct.pack(">HH", len(udta_text), 0x15C7) + udta_text)
    meta = _mdta_meta([
        ("com.apple.quicktime.location.ISO6709", 1, b"+45.4642+009.1900+120.500/"),
        ("com.apple.quicktime.make", 1, b"Apple"),
        ("com.apple.quicktime.model", 1, "iPhone 15 Pro".encode()),
        ("com.apple.quicktime.software", 1, b"17.4.1"),
        ("com.apple.quicktime.creationdate", 1, b"2026-05-01T10:20:30+0200"),
        ("com.apple.quicktime.live-photo.vitality-score", 23, struct.pack(">f", 0.5)),
        ("com.apple.quicktime.content.identifier", 21, struct.pack(">i", 7)),
    ], full_box=full_box_meta)
    mvhd_body = struct.pack(">IIII", created, created, 1000, 10010) + struct.pack(">IH", 0x00010000, 0x0100) \
        + bytes(10) + _matrix() + bytes(24) + struct.pack(">I", 3)
    moov = atom(b"moov", _full(b"mvhd", mvhd_body) + _video_trak(1, created, payload_at, 90)
                + _audio_trak(2, created, payload_at + 512) + meta + udta)
    xmp_box = atom(b"uuid", bytes.fromhex("be7acfcb97a942e89c71999491e3afac") + XMP_PACKET.encode("utf-8"))
    path.write_bytes(ftyp + mdat + moov + xmp_box)       # moov after mdat, like a phone recording
    return path


@sample("mov_full", mutations=_LOCATION)
def mov_full(tmp_path: Path) -> Path:
    path = write_phone_movie(tmp_path / "phone.mov")
    add_full_tags(path)
    return path
