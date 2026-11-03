"""HEIC / HEIF / AVIF images: read-only metadata from the ISO base media structure.

The still image is never decoded. The ``meta`` box is walked for the item table
(``iinf``), the item locations (``iloc``) and the image properties (``ipco`` /
``ipma``): size and rotation of the primary image, EXIF and XMP items and the ICC
profile. Item payloads are read from their ``iloc`` extents, bounded in size.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path

from core import exif_codec, icc, xmp
from core.base_handler import BaseMetadataHandler
from core.handlers.image_handler import _exif_fields, _xmp_fields
from core.isobmff import Box, Reader, open_reader
from core.models import MetadataField, MetadataRecord

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
        for _ in range(take(2)):
            if index_size:
                take(index_size)
            extents.append((base + take(offset_size), take(length_size)))
        if pos > len(data):
            return
        item = items.setdefault(item_id, _Item(item_id))
        item.extents, item.method = extents, method


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


class HeifHandler(BaseMetadataHandler):
    NAME = "heif"
    EXTENSIONS = frozenset({".heic", ".heif", ".hif", ".avif"})
    PRIORITY = 12

    def read(self, path: Path) -> MetadataRecord:
        fields: list[MetadataField] = []
        errors: list[str] = []
        warnings: list[str] = []
        try:
            fields = self._fields(path, warnings)
        except Exception as exc:       # reading never raises: damaged data is a read error
            errors.append(f"HEIF read error: {type(exc).__name__}: {exc}")
        record = self._make_record(path, read_errors=errors, warnings=warnings,
                                   supports_write=False, supports_delete=False)
        record.fields = fields
        record.read_only_reason = "Metadata of HEIF/AVIF images is read-only for now"
        record.dedupe_keys()
        return record

    def _fields(self, path: Path, warnings: list[str]) -> list[MetadataField]:
        fields: list[MetadataField] = []

        def add(key, label, value, field_type="str", multi=False):
            fields.append(MetadataField(key, label, value, raw_value=value, editable=False, deletable=False,
                                        field_type=field_type, source="heif", multi=multi))

        with open_reader(path) as rd:
            ftyp = rd.find(None, b"ftyp")
            if ftyp is not None and ftyp.end - ftyp.data_start >= 8:
                major = bytes(rd.buf[ftyp.data_start:ftyp.data_start + 4]).decode("latin-1").strip()
                add("heif:MajorBrand", "Major Brand", major)
                brands = [bytes(rd.buf[p:p + 4]).decode("latin-1").strip()
                          for p in range(ftyp.data_start + 8, ftyp.end - 3, 4)]
                if brands:
                    add("heif:CompatibleBrands", "Compatible Brands", brands, multi=True)
            meta, children = _meta_children(rd)
            if meta is None:
                warnings.append("No metadata box found")
                return fields
            props = _primary_properties(rd, children)
            brands = {f.value for f in fields if f.key == "heif:MajorBrand"}
            for f in fields:
                if f.key == "heif:CompatibleBrands":
                    brands |= set(f.value)
            fmt = "AVIF" if brands & {"avif", "avis"} else "HEIF"
            fields.insert(0, MetadataField("img:format", "Format", fmt, raw_value=fmt, editable=False,
                                           deletable=False, source="image"))
            if "width" in props:
                fields.append(MetadataField("img:width", "Width (px)", props["width"], editable=False,
                                            deletable=False, field_type="int", source="image"))
                fields.append(MetadataField("img:height", "Height (px)", props["height"], editable=False,
                                            deletable=False, field_type="int", source="image"))
            if "rotation" in props:
                add("heif:Rotation", "Rotation (°)", props["rotation"], "int")
            idat = next((b for b in children if b.type == b"idat"), None)
            items = _items(rd, children)
            for item in items.values():
                data = None
                if item.kind == "Exif":
                    data = _payload(rd, item, idat)
                    if data and len(data) > 4:
                        skip = struct.unpack_from(">I", data, 0)[0]
                        try:
                            fields += _exif_fields(exif_codec.parse(data[4 + skip:], with_ifd1=False), False)
                        except exif_codec.ExifFormatError as exc:
                            warnings.append(f"EXIF not readable: {exc}")
                elif item.kind == "mime" and item.content_type == _XMP_MIME:
                    data = _payload(rd, item, idat)
                    if data:
                        try:
                            fields += _xmp_fields(xmp.XmpPacket(data), False)
                        except xmp.XmpFormatError as exc:
                            warnings.append(f"XMP not readable: {exc}")
            for prop in (props.get("properties") or []):
                if prop.type == b"colr" and prop.end - prop.data_start > 4:
                    if bytes(rd.buf[prop.data_start:prop.data_start + 4]) in (b"prof", b"rICC"):
                        fields += icc.fields(bytes(rd.buf[prop.data_start + 4:prop.end]))
        return fields

    def write(self, path: Path, fields: list[MetadataField]) -> None:
        raise NotImplementedError("HEIF/AVIF metadata is read-only for now")

    def delete(self, path: Path, keys: list[str]) -> None:
        raise NotImplementedError("HEIF/AVIF metadata is read-only for now")
