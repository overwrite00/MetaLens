"""A Matroska file as a muxer writes it: Info, Tracks, Tags, an attachment and a cluster with media data."""
from __future__ import annotations

import struct
from pathlib import Path

from . import registrar
from .hachoir import MKV_DATE_NS, _el, _uint, mkv_bytes

SAMPLES: list = []
sample = registrar(SAMPLES, __name__)

CLUSTER_PAYLOAD = bytes(range(256)) * 8


def _tag(name: str, text: str, language: str = "und") -> bytes:
    return _el(b"\x67\xc8", _el(b"\x45\xa3", name.encode()) + _el(b"\x44\x87", text.encode("utf-8"))
               + _el(b"\x44\x7a", language.encode()))


def mkv_full_bytes() -> bytes:
    header = _el(b"\x1a\x45\xdf\xa3", _el(b"\x42\x82", b"matroska") + _uint(b"\x42\x85", 4))
    info = _el(b"\x15\x49\xa9\x66", _uint(b"\x2a\xd7\xb1", 1_000_000) + _el(b"\x44\x89", struct.pack(">d", 2500.0))
               + _el(b"\x7b\xa9", "Vacanze è 2026".encode("utf-8")) + _el(b"\x4d\x80", b"libebml v1.4.5 + libmatroska v1.7.1")
               + _el(b"\x57\x41", b"mkvmerge v81.0 ('Circles') 64-bit")
               + _el(b"\x44\x61", MKV_DATE_NS.to_bytes(8, "big", signed=True))
               + _el(b"\xec", bytes(40)))
    track = _el(b"\xae", _uint(b"\xd7", 1) + _uint(b"\x83", 1) + _el(b"\x86", b"V_MPEG4/ISO/AVC")
                + _el(b"\x22\xb5\x9c", b"ita") + _el(b"\xe0", _uint(b"\xb0", 1920) + _uint(b"\xba", 1080)))
    tags = _el(b"\x12\x54\xc3\x67", _el(b"\x73\x73", _el(b"\x63\xc0", _uint(b"\x68\xca", 50))
                                          + _tag("TITLE", "Vacanze") + _tag("ARTIST", "Ada Lovelace")
                                          + _tag("ENCODER", "mkvmerge")))
    cover = _el(b"\x19\x41\xa4\x69", _el(b"\x61\xa7", _el(b"\x46\x6e", b"cover.jpg") + _el(b"\x46\x60", b"image/jpeg")
                                           + _el(b"\x46\x5c", b"\xff\xd8\xff\xe0" + bytes(60))))
    cluster = _el(b"\x1f\x43\xb6\x75", _uint(b"\xe7", 0) + _el(b"\xa3", CLUSTER_PAYLOAD))
    segment = _el(b"\x18\x53\x80\x67", info + _el(b"\x16\x54\xae\x6b", track) + tags + cover + cluster)
    return header + segment


@sample("mkv_full", mutations={"mkv:Info:Title": "T", "mkv:Info:MuxingApp": "M", "mkv:Info:WritingApp": "W",
                               "mkv:tag:TITLE": "V", "mkv:tag:ARTIST": "A", "mkv:tag:ENCODER": "E"})
def mkv_full(tmp_path: Path) -> Path:
    path = tmp_path / "holiday.mkv"
    path.write_bytes(mkv_full_bytes())
    return path
