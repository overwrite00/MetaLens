"""HEIC / AVIF samples: ISO base media files with a ``meta`` box (no decodable picture).

``ftyp`` + ``meta`` (``hdlr``, ``pitm``, ``iinf``, ``iloc``, ``iprp``) + ``mdat`` holding a
placeholder image payload, an Exif item (4-byte TIFF header offset + TIFF) and an XMP item.
"""
from __future__ import annotations

import struct
from pathlib import Path

from . import registrar
from .image import XMP_PACKET, camera_exif, icc_profile
from .mp4 import atom

SAMPLES: list = []
sample = registrar(SAMPLES, __name__)

IMAGE_PAYLOAD = bytes(range(64)) * 2


def _full(name: bytes, body: bytes, version: int = 0, flags: int = 0) -> bytes:
    return atom(name, struct.pack(">I", (version << 24) | flags) + body)


def _infe(item_id: int, kind: bytes, name: bytes, content_type: bytes = b"") -> bytes:
    body = struct.pack(">HH", item_id, 0) + kind + name + bytes(1)
    if content_type:
        body += content_type + bytes(1)
    return _full(b"infe", body, version=2)


def _iloc(extents: list[tuple[int, int, int]]) -> bytes:
    """Version 0 item location box: (item_id, offset, length) per item, 4-byte offsets and lengths."""
    body = bytes([0x44, 0x00]) + struct.pack(">H", len(extents))
    for item_id, offset, length in extents:
        body += struct.pack(">HHHII", item_id, 0, 1, offset, length)
    return _full(b"iloc", body)


def _meta(offsets: tuple[int, int, int], sizes: tuple[int, int, int], with_icc: bool, rotation: int) -> bytes:
    ispe = _full(b"ispe", struct.pack(">II", 4032, 3024))
    irot = atom(b"irot", bytes([rotation]))
    props = [ispe, irot]
    if with_icc:
        props.append(atom(b"colr", b"prof" + icc_profile()))
    ipco = atom(b"ipco", b"".join(props))
    ipma = _full(b"ipma", struct.pack(">IHB", 1, 1, len(props)) + bytes(range(1, len(props) + 1)))
    iinf = _full(b"iinf", struct.pack(">H", 3) + _infe(1, b"hvc1", b"Image") + _infe(2, b"Exif", b"Exif")
                 + _infe(3, b"mime", b"XMP", b"application/rdf+xml"))
    hdlr = _full(b"hdlr", bytes(4) + b"pict" + bytes(12) + bytes(1))
    pitm = _full(b"pitm", struct.pack(">H", 1))
    iloc = _iloc([(1, offsets[0], sizes[0]), (2, offsets[1], sizes[1]), (3, offsets[2], sizes[2])])
    return _full(b"meta", hdlr + pitm + iinf + iloc + atom(b"iprp", ipco + ipma))


def write_heif(path: Path, major: bytes = b"heic", compatible: tuple[bytes, ...] = (b"mif1", b"heic"),
               with_icc: bool = True, rotation: int = 1) -> Path:
    ftyp = atom(b"ftyp", major + bytes(4) + b"".join(compatible))
    exif = struct.pack(">I", 0) + camera_exif(thumbnail=False)[6:]
    xmp = XMP_PACKET.encode("utf-8")
    sizes = (len(IMAGE_PAYLOAD), len(exif), len(xmp))
    size = len(_meta((0, 0, 0), sizes, with_icc, rotation))
    start = len(ftyp) + size + 8                       # first byte of the mdat payload
    offsets = (start, start + sizes[0], start + sizes[0] + sizes[1])
    path.write_bytes(ftyp + _meta(offsets, sizes, with_icc, rotation) + atom(b"mdat", IMAGE_PAYLOAD + exif + xmp))
    return path


@sample("heic_camera", tags={"read_only"})
def heic_camera(tmp_path: Path) -> Path:
    return write_heif(tmp_path / "IMG_0001.heic")


@sample("avif_basic", tags={"read_only"})
def avif_basic(tmp_path: Path) -> Path:
    return write_heif(tmp_path / "photo.avif", major=b"avif", compatible=(b"avif", b"mif1", b"miaf"),
                      with_icc=False, rotation=0)
