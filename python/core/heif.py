"""HEIC / HEIF / AVIF container structure: item table, locations, image properties, metadata layout.

Pure parsing of the ISO base media ``meta`` box, with no dependency on the handlers (the image handler
and the HEIF handler both use it). The still image is never decoded; item payloads are read from their
``iloc`` extents, bounded in size.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field

from core.isobmff import Box, Reader

MAX_ITEM_BYTES = 16 * 1024 * 1024
_NUL = bytes([0])
_XMP_MIME = "application/rdf+xml"


@dataclass
class _Item:
    item_id: int
    kind: str = ""
    content_type: str = ""
    extents: list[tuple[int, int]] = field(default_factory=list)   # (offset, length)
    method: int = 0                                                  # 0 = file offset, 1 = idat
    loc: tuple[int, int, int, int, int] | None = None                # single extent: where its fields are


def _cstring(buf: bytes, pos: int) -> tuple[str, int]:
    end = buf.find(_NUL, pos)
    if end < 0:
        return buf[pos:].decode("utf-8", "replace"), len(buf)
    return buf[pos:end].decode("utf-8", "replace"), end + 1


def _meta_children(rd: Reader) -> tuple[Box | None, list[Box]]:
    meta = rd.find(None, b"meta")
    if meta is None:
        return None, []
    return meta, rd.children(meta.data_start + 4, meta.end)       # meta is a full box


def _items(rd: Reader, children: list[Box]) -> dict[int, _Item]:
    items: dict[int, _Item] = {}
    iinf = next((b for b in children if b.type == b"iinf"), None)
    if iinf is not None:
        version = rd.buf[iinf.data_start]
        count_at = iinf.data_start + 4
        entries_at = count_at + (2 if version == 0 else 4)
        for infe in rd.children(entries_at, iinf.end):
            if infe.type != b"infe":
                continue
            data = rd.payload(infe)
            v = data[0]
            if v < 2 or len(data) < 12:
                continue
            item_id = int.from_bytes(data[4:6] if v == 2 else data[4:8], "big")
            pos = 8 if v == 2 else 10
            kind = data[pos:pos + 4].decode("latin-1")
            item = items.setdefault(item_id, _Item(item_id, kind))
            _, pos = _cstring(data, pos + 4)               # item name
            if kind == "mime":
                item.content_type, _ = _cstring(data, pos)
    iloc = next((b for b in children if b.type == b"iloc"), None)
    if iloc is not None:
        _locations(rd, iloc, items)
    return items


def _locations(rd: Reader, iloc: Box, items: dict[int, _Item]) -> None:
    data = rd.payload(iloc)
    if len(data) < 8:
        return
    version = data[0]
    offset_size, length_size = data[4] >> 4, data[4] & 15
    base_size, index_size = data[5] >> 4, (data[5] & 15) if version in (1, 2) else 0
    pos = 6
    count_width = 2 if version < 2 else 4
    count = int.from_bytes(data[pos:pos + count_width], "big")
    pos += count_width

    def take(size: int) -> int:
        nonlocal pos
        value = int.from_bytes(data[pos:pos + size], "big")
        pos += size
        return value

    for _ in range(min(count, 10_000)):
        item_id = take(2 if version < 2 else 4)
        method = take(2) & 15 if version in (1, 2) else 0
        take(2)                                             # data_reference_index
        base = take(base_size)
        extents = []
        loc = None
        count_of_extents = take(2)
        for _ in range(count_of_extents):
            if index_size:
                take(index_size)
            offset_at = pos
            offset = take(offset_size)
            length_at = pos
            extents.append((base + offset, take(length_size)))
            if count_of_extents == 1:
                # absolute positions in the file of the offset and length fields, their widths, the base offset
                loc = (iloc.data_start + offset_at, offset_size, iloc.data_start + length_at, length_size, base)
        if pos > len(data):
            return
        item = items.setdefault(item_id, _Item(item_id))
        item.extents, item.method, item.loc = extents, method, loc


def _payload(rd: Reader, item: _Item, idat: Box | None) -> bytes | None:
    base = idat.data_start if item.method == 1 and idat is not None else 0
    if item.method not in (0, 1) or (item.method == 1 and idat is None):
        return None
    total = sum(length for _, length in item.extents)
    if not item.extents or total > MAX_ITEM_BYTES:
        return None
    chunks = []
    for offset, length in item.extents:
        start = base + offset
        if length and start + length > len(rd.buf):
            return None
        chunks.append(bytes(rd.buf[start:start + length]))
    return b"".join(chunks)


def _primary_properties(rd: Reader, children: list[Box]) -> dict[str, object]:
    """Width, height and rotation of the primary item through ``pitm`` / ``ipma`` / ``ipco``."""
    pitm = next((b for b in children if b.type == b"pitm"), None)
    iprp = next((b for b in children if b.type == b"iprp"), None)
    if pitm is None or iprp is None:
        return {}
    version = rd.buf[pitm.data_start]
    primary = int.from_bytes(rd.buf[pitm.data_start + 4:pitm.data_start + (6 if version == 0 else 8)], "big")
    ipco = rd.find(iprp, b"ipco")
    ipma = rd.find(iprp, b"ipma")
    if ipco is None or ipma is None:
        return {}
    props = rd.children(ipco.data_start, ipco.end)
    data = rd.payload(ipma)
    version, flags = data[0], int.from_bytes(data[1:4], "big")
    count = int.from_bytes(data[4:8], "big")
    pos = 8
    wanted: list[int] = []
    for _ in range(min(count, 10_000)):
        width = 2 if version < 1 else 4
        if pos + width + 1 > len(data):
            break
        item_id = int.from_bytes(data[pos:pos + width], "big")
        pos += width
        associations = data[pos]
        pos += 1
        for _ in range(associations):
            if flags & 1:
                index = int.from_bytes(data[pos:pos + 2], "big") & 0x7FFF
                pos += 2
            else:
                index = data[pos] & 0x7F
                pos += 1
            if item_id == primary:
                wanted.append(index)
    out: dict[str, object] = {"properties": [props[i - 1] for i in wanted if 0 < i <= len(props)]}
    for box in out["properties"]:                                  # type: ignore[union-attr]
        if box.type == b"ispe" and box.end - box.data_start >= 12:
            out["width"], out["height"] = struct.unpack_from(">II", rd.buf, box.data_start + 4)
        elif box.type == b"irot" and box.end > box.data_start:
            out["rotation"] = (rd.buf[box.data_start] & 3) * 90
    return out


MAX_EDIT_BYTES = 256 * 1024 * 1024
_HEIF_BRANDS = frozenset({b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"mif1", b"msf1", b"avif", b"avis"})


def is_heif_brand(data: bytes) -> bool:
    return data[4:8] == b"ftyp" and bytes(data[8:12]) in _HEIF_BRANDS


def metadata_layout(data) -> dict[str, dict]:
    """Where the Exif item and the XMP item are, when each is stored in one extent of the file (the only
    layout that can be changed): ``{"exif": {"item": (start, end), "range": (tiff start, end), "loc": ...},
    "xmp": {"item": (start, end), "range": (start, end), "loc": ...}}``."""
    from core.isobmff import Reader
    rd = Reader(data)
    meta, children = _meta_children(rd)
    out: dict[str, dict] = {}
    if meta is None:
        return out
    for item in _items(rd, children).values():
        if len(item.extents) != 1 or item.method != 0 or item.extents[0][1] <= 0 or item.loc is None:
            continue
        start, length = item.extents[0]
        if start + length > len(data):
            continue
        if item.kind == "Exif" and length > 4:
            skip = struct.unpack_from(">I", data, start)[0]
            if 4 + skip < length:
                out.setdefault("exif", {"item": (start, start + length), "range": (start + 4 + skip, start + length),
                                        "loc": item.loc})
        elif item.kind == "mime" and item.content_type == _XMP_MIME:
            out.setdefault("xmp", {"item": (start, start + length), "range": (start, start + length), "loc": item.loc})
    return out


def metadata_ranges(data) -> dict[str, tuple[int, int]]:
    """Absolute ``(start, end)`` of the TIFF part of the Exif item and of the XMP item."""
    return {name: entry["range"] for name, entry in metadata_layout(data).items()}
