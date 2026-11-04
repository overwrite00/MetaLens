"""Read-only QuickTime / ISO base media structure fields (``qt:`` namespace).

Shared by the MP4 family handlers: brand, movie and track headers, handler,
codec, language, rotation, frame rate, QuickTime ``mdta`` keys (where phones
store the location and capture time), ``udta`` text atoms and an XMP packet.
Offsets, sizes and sample tables are never exposed: they change with any
rewrite and would be reported as changed by the post-write verification.
"""
from __future__ import annotations

import math
import struct
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core.isobmff import Box, Reader, open_reader
from core.models import MetadataField
from core.values import format_datetime

_EPOCH_1904 = datetime(1904, 1, 1, tzinfo=timezone.utc)
_XMP_UUID = bytes.fromhex("be7acfcb97a942e89c71999491e3afac")
_HANDLER_NAMES = {b"vide": "Video", b"soun": "Audio", b"text": "Text", b"sbtl": "Subtitle", b"meta": "Metadata",
                  b"hint": "Hint", b"tmcd": "Timecode", b"pict": "Picture"}


def _field(key: str, label: str, value, field_type: str, *, multi: bool = False) -> MetadataField:
    return MetadataField(key, label, value, raw_value=value, editable=False, deletable=False,
                         field_type=field_type, source="qt", multi=multi)


def _text(raw: bytes) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("latin-1")


def _date(seconds: int) -> str | None:
    """Mac epoch (1904) seconds to canonical UTC, ``None`` when unset (0) or out of range."""
    if not seconds:
        return None
    try:
        return format_datetime(_EPOCH_1904 + timedelta(seconds=seconds))
    except (OverflowError, ValueError):
        return None


def _language(code: int) -> str | None:
    if code == 0 or code == 0x7FFF or code < 0x400:      # unset or a legacy Macintosh code
        return None
    letters = [((code >> shift) & 31) + 0x60 for shift in (10, 5, 0)]
    if not all(0x61 <= c <= 0x7A for c in letters):
        return None
    word = "".join(map(chr, letters))
    return None if word == "und" else word


def _rotation(matrix: tuple[int, ...]) -> int | None:
    """Rotation in degrees (0/90/180/270) from the a/b entries of the matrix, ``None`` if it is not one."""
    a, b = matrix[0] / 65536, matrix[1] / 65536
    if a == 0 and b == 0:
        return None
    angle = math.degrees(math.atan2(b, a)) % 360
    nearest = round(angle / 90) * 90
    return int(nearest % 360) if abs(angle - nearest) < 1 else None


def _ftyp(rd: Reader, out: list[MetadataField]) -> None:
    box = rd.find(None, b"ftyp")
    if box is None or box.end - box.data_start < 8:
        return
    major = bytes(rd.buf[box.data_start:box.data_start + 4])
    minor = rd.u("I", box.data_start + 4)
    out.append(_field("qt:MajorBrand", "Major Brand", _text(major).strip(), "str"))
    if minor and minor[0]:
        out.append(_field("qt:MinorVersion", "Minor Version", minor[0], "int"))
    brands = [_text(bytes(rd.buf[p:p + 4])).strip() for p in range(box.data_start + 8, box.end - 3, 4)]
    brands = [b for b in brands if b]
    if brands:
        out.append(_field("qt:CompatibleBrands", "Compatible Brands", brands, "str", multi=True))


def _mvhd(rd: Reader, moov: Box, out: list[MetadataField]) -> None:
    box = rd.find(moov, b"mvhd")
    if box is None:
        return
    base = box.data_start
    version = rd.buf[base] if base < box.end else 0
    if version == 1:
        head = rd.u("QQI", base + 4)
        rate = rd.u("IH", base + 32)
    else:
        head = rd.u("III", base + 4)
        rate = rd.u("IH", base + 20)
    if head is None:
        return
    created, modified, scale = head
    for key, label, seconds in (("qt:CreateDate", "Create Date", created), ("qt:ModifyDate", "Modify Date", modified)):
        stamp = _date(seconds)
        if stamp:
            out.append(_field(key, label, stamp, "datetime"))
    if scale:
        out.append(_field("qt:TimeScale", "Time Scale", scale, "int"))
    if rate is not None:
        out.append(_field("qt:PreferredRate", "Preferred Rate", rate[0] / 65536, "float"))
        out.append(_field("qt:PreferredVolume", "Preferred Volume", rate[1] / 256, "float"))


def _track(rd: Reader, trak: Box, out: list[MetadataField]) -> None:
    tkhd = rd.find(trak, b"tkhd")
    if tkhd is None:
        return
    base = tkhd.data_start
    version = rd.buf[base] if base < tkhd.end else 0
    if version == 1:
        ids = rd.u("QQI", base + 4)
        matrix_at = base + 4 + 8 + 8 + 4 + 4 + 8 + 8 + 8
    else:
        ids = rd.u("III", base + 4)
        matrix_at = base + 4 + 4 + 4 + 4 + 4 + 4 + 8 + 8
    if ids is None:
        return
    track_id = ids[2]
    prefix = f"qt:track{track_id}"
    label = f"Track {track_id}"
    matrix = rd.u("9i", matrix_at)
    size = rd.u("II", matrix_at + 36)
    created = _date(ids[0])
    if created:
        out.append(_field(f"{prefix}:CreateDate", f"{label} Create Date", created, "datetime"))

    mdia = rd.find(trak, b"mdia")
    handler = None
    scale = duration = None
    language = None
    if mdia is not None:
        hdlr = rd.find(mdia, b"hdlr")
        if hdlr is not None:
            found = rd.u("4s", hdlr.data_start + 8)
            handler = bytes(found[0]) if found else None
        mdhd = rd.find(mdia, b"mdhd")
        if mdhd is not None:
            v = rd.buf[mdhd.data_start] if mdhd.data_start < mdhd.end else 0
            m = rd.u("QQIQH", mdhd.data_start + 4) if v == 1 else rd.u("IIIIH", mdhd.data_start + 4)
            if m is not None:
                scale, duration, language = m[2], m[3], _language(m[4])
    if handler:
        out.append(_field(f"{prefix}:HandlerType", f"{label} Handler",
                          _HANDLER_NAMES.get(handler, _text(handler).strip()), "str"))
    if language:
        out.append(_field(f"{prefix}:Language", f"{label} Language", language, "str"))
    if scale:
        out.append(_field(f"{prefix}:MediaTimeScale", f"{label} Media Time Scale", scale, "int"))
    if matrix is not None and handler == b"vide":
        rotation = _rotation(matrix)
        if rotation is not None:
            out.append(_field(f"{prefix}:Rotation", f"{label} Rotation (°)", rotation, "int"))
    if size is not None and handler == b"vide" and size[0] and size[1]:
        out.append(_field(f"{prefix}:Width", f"{label} Width", size[0] >> 16, "int"))
        out.append(_field(f"{prefix}:Height", f"{label} Height", size[1] >> 16, "int"))

    stbl = rd.find(mdia, b"minf", b"stbl") if mdia is not None else None
    if stbl is None:
        return
    stsd = rd.find(stbl, b"stsd")
    if stsd is not None:
        entry = stsd.data_start + 8
        head = rd.u("I4s", entry)
        if head is not None:
            out.append(_field(f"{prefix}:CodecID", f"{label} Codec", _text(bytes(head[1])).strip(), "str"))
            if handler == b"soun":
                audio = rd.u("HHHHI", entry + 24)
                if audio is not None:
                    if audio[0]:
                        out.append(_field(f"{prefix}:Channels", f"{label} Channels", audio[0], "int"))
                    if audio[4] >> 16:
                        out.append(_field(f"{prefix}:SampleRate", f"{label} Sample Rate (Hz)", audio[4] >> 16, "int"))
    stts = rd.find(stbl, b"stts")
    if stts is not None and handler == b"vide" and scale and duration:
        count = rd.u("I", stts.data_start + 4)
        if count and count[0] * 8 + 8 <= stts.end - stts.data_start:
            samples = sum(rd.u("II", stts.data_start + 8 + 8 * i)[0] for i in range(count[0]))
            if samples:
                out.append(_field(f"{prefix}:FrameRate", f"{label} Frame Rate",
                                  round(samples * scale / duration, 3), "float"))


def _mdta_value(kind: int, raw: bytes):
    """(value, field_type) of a ``data`` atom, ``None`` when the type is not understood."""
    if kind == 1:
        return _text(raw), "str"
    if kind in (21, 22) and len(raw) in (1, 2, 4, 8):
        return int.from_bytes(raw, "big", signed=(kind == 21)), "int"
    if kind == 23 and len(raw) == 4:
        value = struct.unpack(">f", raw)[0]
        return (float(value), "float") if math.isfinite(value) else None
    if kind == 24 and len(raw) == 8:
        value = struct.unpack(">d", raw)[0]
        return (float(value), "float") if math.isfinite(value) else None
    return None


def _mdta(rd: Reader, moov: Box, out: list[MetadataField]) -> None:
    meta = rd.find(moov, b"meta")
    if meta is None:
        return
    start = meta.data_start
    if bytes(rd.buf[start:start + 4]) == b"\x00\x00\x00\x00":      # ISO full box; QuickTime has none
        start += 4
    keys = ilst = None
    for child in rd.children(start, meta.end):
        if child.type == b"keys":
            keys = child
        elif child.type == b"ilst":
            ilst = child
    if keys is None or ilst is None:
        return
    count = rd.u("I", keys.data_start + 4)
    names: list[str] = []
    pos = keys.data_start + 8
    for _ in range(min(count[0], 10_000) if count else 0):
        entry = rd.u("I4s", pos)
        if entry is None or entry[0] < 8 or pos + entry[0] > keys.end:
            break
        names.append(_text(bytes(rd.buf[pos + 8:pos + entry[0]])))
        pos += entry[0]
    seen: set[str] = set()
    for item in rd.children(ilst.data_start, ilst.end):
        index = struct.unpack(">I", item.type)[0]
        if not 1 <= index <= len(names) or names[index - 1] in seen:
            continue
        name = names[index - 1]
        data = rd.find(item, b"data")
        head = rd.u("I", data.data_start) if data is not None else None
        if head is None:
            continue
        raw = bytes(rd.buf[data.data_start + 8:data.end])
        seen.add(name)
        parsed = _mdta_value(head[0] & 0xFFFFFF, raw)
        if parsed is not None:
            out.append(_field(f"qt:mdta:{name}", f"QuickTime {name}", parsed[0], parsed[1]))
        elif raw:
            out.append(MetadataField.binary_field(f"qt:mdta:{name}", f"QuickTime {name}", raw,
                                                  deletable=False, source="qt"))


def _udta(rd: Reader, moov: Box, out: list[MetadataField]) -> None:
    udta = rd.find(moov, b"udta")
    if udta is None:
        return
    for child in rd.children(udta.data_start, udta.end):
        if child.type[:1] != b"\xa9":
            continue
        head = rd.u("HH", child.data_start)
        if head is None:
            continue
        text = _text(bytes(rd.buf[child.data_start + 4:min(child.end, child.data_start + 4 + head[0])]))
        name = child.type.decode("latin-1")
        out.append(_field(f"qt:udta:{name}", f"QuickTime {name}", text.rstrip("\x00"), "str"))


def _xmp(rd: Reader, moov: Box | None, out: list[MetadataField], warnings: list[str], writable: bool = False) -> None:
    from core import xmp
    from core.handlers.image_handler import _xmp_fields
    packets = [b for b in rd.children() if b.type == b"uuid" and b.usertype == _XMP_UUID]
    if moov is not None:
        udta = rd.find(moov, b"udta")
        if udta is not None:
            packets += [b for b in rd.children(udta.data_start, udta.end) if b.type == b"XMP_"]
    for box in packets[:1]:
        try:
            out.extend(_xmp_fields(xmp.XmpPacket(rd.payload(box)), writable))
        except xmp.XmpFormatError as exc:
            warnings.append(f"XMP not readable: {exc}")


def qt_fields(path: Path, xmp_writable: bool = False) -> tuple[list[MetadataField], list[str]]:
    """Structure fields of an MP4-family file and the notices to show with them."""
    out: list[MetadataField] = []
    warnings: list[str] = []
    with open_reader(path) as rd:
        _ftyp(rd, out)
        moov = rd.find(None, b"moov")
        if moov is not None:
            _mvhd(rd, moov, out)
            for trak in rd.find_all(moov, b"trak")[:64]:
                _track(rd, trak, out)
            _mdta(rd, moov, out)
            _udta(rd, moov, out)
        _xmp(rd, moov, out, warnings, xmp_writable)
    if any(f.key in ("qt:CreateDate", "qt:ModifyDate") for f in out):
        warnings.append("QuickTime dates are UTC by specification, but many cameras store local time there.")
    return out, warnings
