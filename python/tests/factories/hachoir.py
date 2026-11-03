"""Samples read through hachoir (the fallback handler and VideoHandler's read-only formats).

Owned by phase F2G: only that phase appends samples here.

* ``wav_as_bin``: a WAV renamed ``.bin`` (recognized from content).
* ``random_xyz``: bytes hachoir does not recognize (not an error: no fields, a warning).
* ``avi_two_streams`` / ``mkv_two_tracks``: minimal AVI (video + audio stream) and
  Matroska (video + audio track) files; they reach ``extract_fields`` through
  VideoHandler, and their groups repeat items such as ``duration``.

The builders are plain functions, also used by ``tests/test_hachoir_handler.py``.

Register with `@sample("name", ...)`; see `tests/factories/__init__.py`.
"""
from __future__ import annotations

import struct
import wave
from pathlib import Path

from . import registrar

SAMPLES: list = []
sample = registrar(SAMPLES, __name__)


def write_wav(path: Path, seconds: int = 1, rate: int = 8000) -> Path:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(2 * rate * seconds))
    return path


# ──────────────────────── AVI (RIFF) ─────────────────────────────────────────

def _chunk(fcc: bytes, data: bytes) -> bytes:
    return fcc + struct.pack("<I", len(data)) + data + (b"\0" if len(data) % 2 else b"")


def _list(kind: bytes, *items: bytes) -> bytes:
    return _chunk(b"LIST", kind + b"".join(items))


def avi_bytes(width: int = 64, height: int = 48, frames: int = 25, fps: int = 25) -> bytes:
    """AVI with one uncompressed video stream and one PCM audio stream (1 s)."""
    avih = struct.pack("<10I4I", 1_000_000 // fps, 0, 0, 0x10, frames, 0, 2, 0,
                       width, height, 0, 0, 0, 0)
    strh_v = b"vids" + b"DIB " + struct.pack("<IHHIIIIIIIIhhhh", 0, 0, 0, 0, 1, fps, 0,
                                             frames, 0, 0xFFFFFFFF, 0, 0, 0, width, height)
    strf_v = struct.pack("<IiiHHIIiiII", 40, width, height, 1, 24, 0, width * height * 3, 0, 0, 0, 0)
    strh_a = b"auds" + bytes(4) + struct.pack("<IHHIIIIIIIIhhhh", 0, 0, 0, 0, 1, 8000, 0,
                                              8000, 0, 0xFFFFFFFF, 2, 0, 0, 0, 0)
    strf_a = struct.pack("<HHIIHH", 1, 1, 8000, 16000, 2, 16)
    hdrl = _list(b"hdrl", _chunk(b"avih", avih),
                 _list(b"strl", _chunk(b"strh", strh_v), _chunk(b"strf", strf_v)),
                 _list(b"strl", _chunk(b"strh", strh_a), _chunk(b"strf", strf_a)))
    movi = _list(b"movi", _chunk(b"00db", bytes(width * height * 3)))
    body = b"AVI " + hdrl + movi
    return b"RIFF" + struct.pack("<I", len(body)) + body


# ──────────────────────── Matroska (EBML) ────────────────────────────────────

MKV_TITLE = "Démo MKV è"
MKV_DURATION = 2.5
MKV_DATE_NS = 86400 * 365 * 10 * 10**9  # 10 * 365 days after 2001-01-01 -> 2010-12-30


def _vint(n: int) -> bytes:
    for length in range(1, 9):
        if n < (1 << (7 * length)) - 1:
            return ((1 << (7 * length)) | n).to_bytes(length, "big")
    raise ValueError(n)


def _el(eid: bytes, data: bytes) -> bytes:
    return eid + _vint(len(data)) + data


def _uint(eid: bytes, value: int) -> bytes:
    return _el(eid, value.to_bytes(max(1, (value.bit_length() + 7) // 8), "big"))


def mkv_bytes() -> bytes:
    """Matroska header + Segment(Info, Tracks[video ita, audio eng]); no clusters."""
    header = _el(b"\x1a\x45\xdf\xa3", _uint(b"\x42\x86", 1) + _uint(b"\x42\xf7", 1)
                 + _uint(b"\x42\xf2", 4) + _uint(b"\x42\xf3", 8) + _el(b"\x42\x82", b"matroska")
                 + _uint(b"\x44\x87", 4) + _uint(b"\x44\x85", 2))
    info = _el(b"\x15\x49\xa9\x66",
               _uint(b"\x2a\xd7\xb1", 1_000_000)
               + _el(b"\x44\x89", struct.pack(">d", MKV_DURATION * 1000))
               + _el(b"\x7b\xa9", MKV_TITLE.encode("utf-8"))
               + _el(b"\x4d\x80", b"MetaLens test") + _el(b"\x57\x41", b"MetaLens test")
               + _el(b"\x44\x61", MKV_DATE_NS.to_bytes(8, "big", signed=True)))
    video = _el(b"\xae", _uint(b"\xd7", 1) + _uint(b"\x83", 1) + _el(b"\x86", b"V_UNCOMPRESSED")
                + _el(b"\x22\xb5\x9c", b"ita") + _el(b"\xe0", _uint(b"\xb0", 64) + _uint(b"\xba", 48)))
    audio = _el(b"\xae", _uint(b"\xd7", 2) + _uint(b"\x83", 2) + _el(b"\x86", b"A_PCM/INT/LIT")
                + _el(b"\x22\xb5\x9c", b"eng")
                + _el(b"\xe1", _el(b"\xb5", struct.pack(">d", 8000.0)) + _uint(b"\x9f", 1)
                      + _uint(b"\x62\x64", 16)))
    segment = _el(b"\x18\x53\x80\x67", info + _el(b"\x16\x54\xae\x6b", video + audio))
    return header + segment


# ──────────────────────── samples ────────────────────────────────────────────

@sample("wav_as_bin")
def wav_as_bin(tmp_path: Path) -> Path:
    return write_wav(tmp_path / "tone.bin")


@sample("random_xyz")  # unrecognized content: valid record, no fields, informative warning
def random_xyz(tmp_path: Path) -> Path:
    path = tmp_path / "noise.xyz"
    path.write_bytes(bytes([0x13, 0x37]) * 64)
    return path


@sample("avi_two_streams")
def avi_two_streams(tmp_path: Path) -> Path:
    path = tmp_path / "clip.avi"
    path.write_bytes(avi_bytes())
    return path


@sample("mkv_two_tracks", mutations={"mkv:Info:Title": "T", "mkv:Info:MuxingApp": "M", "mkv:Info:WritingApp": "W"})
def mkv_two_tracks(tmp_path: Path) -> Path:
    path = tmp_path / "clip.mkv"
    path.write_bytes(mkv_bytes())
    return path
