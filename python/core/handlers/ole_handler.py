"""Legacy OLE documents (.doc/.xls/.ppt and templates): property sets, read-only.

``olefile`` opens the compound file; the property set streams
(``\\x05SummaryInformation``, ``\\x05DocumentSummaryInformation`` with its
user-defined section) are parsed here following [MS-OLEPS], because
``OleFileIO.getproperties`` drops every NUL byte of a CodePageString (which
destroys UTF-16 text when the set's codepage is 1200), never applies the codepage
and turns the total editing time (a duration stored as FILETIME) into a date.

Values follow ``core.values``: strings decoded with the codepage of their set
(PID 1; 1200 = UTF-16LE, 65001 = UTF-8; undecodable bytes fall back to Latin-1
with a warning, never U+FFFD), FILETIME as UTC ``datetime`` (0 = not set, the
field is omitted), total editing time as ``duration`` seconds, VT_CF/VT_BLOB as
binary descriptors, vectors as lists. Every field is read-only: writing legacy
OLE property sets is not supported (use docx/xlsx/pptx).
"""
from __future__ import annotations

import codecs
import math
import struct
import uuid
from dataclasses import dataclass, field as dc_field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from core.base_handler import BaseMetadataHandler
from core.models import MetadataField, MetadataRecord
from core.values import decode_text

_OLE_EXTS = frozenset({".doc", ".xls", ".ppt", ".dot", ".xlt", ".pot"})

SUMMARY_STREAM = "\x05SummaryInformation"
DOC_SUMMARY_STREAM = "\x05DocumentSummaryInformation"

FMTID_SUMMARY = uuid.UUID("F29F85E0-4FF9-1068-AB91-08002B27B3D9")
FMTID_DOC_SUMMARY = uuid.UUID("D5CDD502-2E9C-101B-9397-08002B2CF9AE")
FMTID_USER_DEFINED = uuid.UUID("D5CDD505-2E9C-101B-9397-08002B2CF9AE")

# PID -> (key, label). PID 0 (dictionary) and 1 (codepage) are structural.
_SUMMARY_PROPS = {
    2: ("ole:title", "Title"),
    3: ("ole:subject", "Subject"),
    4: ("ole:author", "Author"),
    5: ("ole:keywords", "Keywords"),
    6: ("ole:comments", "Comments"),
    7: ("ole:template", "Template"),
    8: ("ole:last_author", "Last Author"),
    9: ("ole:revision", "Revision Number"),
    10: ("ole:edit_time", "Total Edit Time"),
    11: ("ole:last_print", "Last Printed"),
    12: ("ole:create_time", "Created"),
    13: ("ole:modified_time", "Modified"),
    14: ("ole:num_pages", "Number of Pages"),
    15: ("ole:num_words", "Number of Words"),
    16: ("ole:num_chars", "Number of Characters"),
    17: ("ole:thumbnail", "Thumbnail"),
    18: ("ole:app_name", "Application Name"),
    19: ("ole:security", "Security"),
}
_EDIT_TIME_PID = 10

# [MS-OSHARED] 2.3.3.2.1 DocumentSummaryInformation.
_DOC_SUMMARY_PROPS = {
    2: ("ole:category", "Category"),
    3: ("ole:presentation_target", "Presentation Format"),
    4: ("ole:bytes", "Bytes"),
    5: ("ole:lines", "Lines"),
    6: ("ole:paragraphs", "Paragraphs"),
    7: ("ole:slides", "Slides"),
    8: ("ole:notes", "Notes"),
    9: ("ole:hidden_slides", "Hidden Slides"),
    10: ("ole:mm_clips", "Multimedia Clips"),
    11: ("ole:scale_crop", "Scale Crop"),
    12: ("ole:heading_pairs", "Heading Pairs"),
    13: ("ole:titles_of_parts", "Titles of Parts"),
    14: ("ole:manager", "Manager"),
    15: ("ole:company", "Company"),
    16: ("ole:links_up_to_date", "Links Up to Date"),
    17: ("ole:chars_with_spaces", "Characters (with spaces)"),
    19: ("ole:shared_doc", "Shared Document"),
    22: ("ole:hyperlinks_changed", "Hyperlinks Changed"),
    23: ("ole:app_version", "Application Version"),
    24: ("ole:dig_sig", "Digital Signature"),
    26: ("ole:content_type", "Content Type"),
    27: ("ole:content_status", "Content Status"),
    28: ("ole:language", "Language"),
    29: ("ole:doc_version", "Document Version"),
}
_HEADING_PAIRS_PID = 12

# ──────────────────────── [MS-OLEPS] property set parser ─────────────────────

VT_EMPTY, VT_NULL, VT_I2, VT_I4, VT_R4, VT_R8, VT_CY, VT_DATE, VT_BSTR = 0, 1, 2, 3, 4, 5, 6, 7, 8
VT_ERROR, VT_BOOL, VT_VARIANT, VT_DECIMAL = 10, 11, 12, 14
VT_I1, VT_UI1, VT_UI2, VT_UI4, VT_I8, VT_UI8, VT_INT, VT_UINT = 16, 17, 18, 19, 20, 21, 22, 23
VT_LPSTR, VT_LPWSTR, VT_FILETIME, VT_BLOB, VT_CF, VT_CLSID = 30, 31, 64, 65, 71, 72
VT_VECTOR = 0x1000

_FIXED = {  # vt -> (struct format, size)
    VT_I2: ("<h", 2), VT_UI2: ("<H", 2), VT_I4: ("<i", 4), VT_INT: ("<i", 4),
    VT_ERROR: ("<I", 4), VT_UI4: ("<I", 4), VT_UINT: ("<I", 4), VT_I8: ("<q", 8),
    VT_UI8: ("<Q", 8), VT_I1: ("<b", 1), VT_UI1: ("<B", 1), VT_R4: ("<f", 4), VT_R8: ("<d", 8),
}

_FILETIME_EPOCH = datetime(1601, 1, 1, tzinfo=timezone.utc)
_MAX_VECTOR = 1 << 16

# Codepages whose Python codec is not "cp<N>".
_CODECS = {
    1200: "utf-16-le", 1201: "utf-16-be", 65001: "utf-8", 65000: "utf-7",
    10000: "mac-roman", 10006: "mac-greek", 10007: "mac-cyrillic", 10029: "mac-latin2",
    10079: "mac-iceland", 10081: "mac-turkish", 20127: "ascii", 20866: "koi8-r",
    21866: "koi8-u", 20932: "euc-jp", 51932: "euc-jp", 51949: "euc-kr", 50220: "iso2022-jp",
    50221: "iso2022-jp-ext", 50222: "iso2022-jp", 52936: "hz", 54936: "gb18030",
    **{28590 + n: f"iso8859-{n}" for n in range(1, 16) if n not in (12,)},
}


class _Malformed(ValueError):
    """A property value that does not fit in its section."""


@dataclass(frozen=True)
class FileTime:
    ticks: int  # 100 ns intervals since 1601-01-01 UTC


@dataclass(frozen=True)
class Blob:
    data: bytes
    vt: int


@dataclass
class Section:
    fmtid: uuid.UUID
    codepage: int | None = None
    props: dict[int, Any] = dc_field(default_factory=dict)
    names: dict[int, str] = dc_field(default_factory=dict)  # user-defined dictionary
    problems: list[str] = dc_field(default_factory=list)    # (pid, message) as text


def codec_for(codepage: int | None) -> str | None:
    """Python codec of a Windows codepage, or None when unknown."""
    if codepage is None:
        return None
    name = _CODECS.get(codepage, f"cp{codepage}")
    try:
        codecs.lookup(name)
    except LookupError:
        return None
    return name


class _Reader:
    """Bounds-checked reader over one section (offsets relative to its start)."""

    def __init__(self, data: bytes, codepage: int | None) -> None:
        self.data = data
        self.codepage = codepage
        self.notes: list[str] = []

    def _take(self, offset: int, size: int) -> bytes:
        if offset < 0 or size < 0 or offset + size > len(self.data):
            raise _Malformed(f"value at offset {offset} ({size} bytes) beyond the section")
        return self.data[offset:offset + size]

    def u32(self, offset: int) -> int:
        return struct.unpack("<I", self._take(offset, 4))[0]

    def typed(self, offset: int) -> tuple[Any, int]:
        """TypedPropertyValue at ``offset`` -> (value, offset after it)."""
        vt = struct.unpack("<H", self._take(offset, 2))[0]
        return self.value(vt, offset + 4)

    def value(self, vt: int, offset: int, *, in_vector: bool = False) -> tuple[Any, int]:
        if vt & VT_VECTOR:
            base = vt & ~VT_VECTOR
            count = self.u32(offset)
            if count > _MAX_VECTOR:
                raise _Malformed(f"vector of {count} items")
            offset += 4
            items = []
            for _ in range(count):
                if base == VT_VARIANT:
                    item, offset = self.typed(offset)
                else:
                    item, offset = self.value(base, offset, in_vector=True)
                items.append(item)
            return items, offset
        if vt in (VT_EMPTY, VT_NULL):
            return None, offset
        if vt in _FIXED:
            fmt, size = _FIXED[vt]
            number = struct.unpack(fmt, self._take(offset, size))[0]
            return number, offset + (size if in_vector else max(size, 4))
        if vt == VT_BOOL:
            raw = struct.unpack("<H", self._take(offset, 2))[0]
            return raw != 0, offset + (2 if in_vector else 4)
        if vt == VT_FILETIME:
            return FileTime(struct.unpack("<Q", self._take(offset, 8))[0]), offset + 8
        if vt == VT_CLSID:
            return str(uuid.UUID(bytes_le=self._take(offset, 16))).upper(), offset + 16
        if vt in (VT_LPSTR, VT_BSTR):
            size = self.u32(offset)
            text = self._codepage_string(self._take(offset + 4, size))
            return text, self._after(offset + 4 + size, in_vector)
        if vt == VT_LPWSTR:
            chars = self.u32(offset)
            raw = self._take(offset + 4, chars * 2)
            text = raw.decode("utf-16-le", errors="surrogatepass").split("\x00", 1)[0]
            return text, self._after(offset + 4 + chars * 2, in_vector)
        if vt in (VT_BLOB, VT_CF):
            size = self.u32(offset)
            return Blob(self._take(offset + 4, size), vt), self._after(offset + 4 + size, in_vector)
        raise _Malformed(f"unsupported property type 0x{vt:04X}")

    def _after(self, end: int, in_vector: bool) -> int:
        """Skip the zero padding to a 4-byte boundary.

        Scalars are always padded; inside vectors some writers omit it, so only
        zero bytes are skipped there (as Apache POI does).
        """
        pad = -end % 4
        if not in_vector:
            return end + pad
        while pad and end < len(self.data) and self.data[end] == 0:
            end += 1
            pad -= 1
        return end

    def _codepage_string(self, raw: bytes) -> str:
        codec = codec_for(self.codepage)
        if codec in ("utf-16-le", "utf-16-be"):
            try:
                return raw.decode(codec).split("\x00", 1)[0]
            except UnicodeDecodeError:
                pass
        else:
            raw = raw.split(b"\x00", 1)[0]
            if codec is not None:
                try:
                    return raw.decode(codec)
                except UnicodeDecodeError:
                    pass
        text = decode_text(raw, ("utf-8",))
        self.notes.append(f"text not valid in codepage {self.codepage}; shown as "
                          f"{'UTF-8' if text.encode('utf-8', 'surrogatepass') == raw else 'Latin-1'}")
        return text

    def dictionary(self, offset: int) -> dict[int, str]:
        count = self.u32(offset)
        if count > _MAX_VECTOR:
            raise _Malformed(f"dictionary of {count} entries")
        offset += 4
        unicode = self.codepage == 1200
        names: dict[int, str] = {}
        for _ in range(count):
            pid, length = self.u32(offset), self.u32(offset + 4)
            offset += 8
            if unicode:
                raw = self._take(offset, length * 2)
                name = raw.decode("utf-16-le", errors="surrogatepass").split("\x00", 1)[0]
                offset += length * 2
                offset += -offset % 4
            else:
                name = self._codepage_string(self._take(offset, length))
                offset += length
            names[pid] = name
        return names


def parse_property_set(data: bytes) -> list[Section]:
    """All sections of a property set stream. Raises ``ValueError`` on a bad header.

    A malformed property only adds a note to ``Section.problems``; the others
    are still parsed.
    """
    if len(data) < 28:
        raise ValueError("property set stream too short")
    byte_order, _version = struct.unpack_from("<HH", data, 0)
    if byte_order != 0xFFFE:
        raise ValueError("bad property set byte order")
    count = struct.unpack_from("<I", data, 24)[0]
    if count < 1 or count > 16 or len(data) < 28 + 20 * count:
        raise ValueError(f"bad property set header ({count} sections)")
    sections = []
    for i in range(count):
        fmtid = uuid.UUID(bytes_le=data[28 + 20 * i:44 + 20 * i])
        start = struct.unpack_from("<I", data, 44 + 20 * i)[0]
        if start + 8 > len(data):
            raise ValueError(f"section {i} beyond the stream")
        size, num = struct.unpack_from("<II", data, start)
        size = min(size, len(data) - start)
        if num > (size - 8) // 8:
            raise ValueError(f"section {i}: {num} properties do not fit in {size} bytes")
        body = data[start:start + size]
        table = [struct.unpack_from("<II", body, 8 + 8 * j) for j in range(num)]
        sections.append(_parse_section(fmtid, body, table))
    return sections


def _parse_section(fmtid: uuid.UUID, body: bytes, table: list[tuple[int, int]]) -> Section:
    sec = Section(fmtid)
    offsets = dict(table)
    reader = _Reader(body, None)
    if 1 in offsets:  # the codepage decides how every string is read
        try:
            value, _ = reader.typed(offsets[1])
            sec.codepage = value & 0xFFFF if isinstance(value, int) else None
        except (_Malformed, struct.error) as exc:
            sec.problems.append(f"codepage: {exc}")
    reader.codepage = sec.codepage
    for pid, offset in table:
        if pid == 1:
            continue
        reader.notes = []
        try:
            if pid == 0:
                sec.names = reader.dictionary(offset)
            else:
                sec.props[pid] = reader.typed(offset)[0]
        except (_Malformed, struct.error, ValueError) as exc:
            sec.problems.append(f"property {pid}: {exc}")
            continue
        sec.problems += [f"property {pid}: {note}" for note in reader.notes]
    return sec


# ──────────────────────── values -> fields ───────────────────────────────────

def _filetime_iso(ft: FileTime) -> str | None:
    if ft.ticks == 0:
        return None
    return (_FILETIME_EPOCH + timedelta(microseconds=ft.ticks // 10)).isoformat()


def _clipboard_mime(data: bytes) -> str | None:
    if len(data) < 8:
        return None
    tag, fmt = struct.unpack_from("<iI", data, 0)
    if tag == -1:
        return {3: "image/wmf", 8: "image/bmp", 14: "image/emf"}.get(fmt)
    if tag == -2:
        return "image/x-pict"
    return None


def _scalar(value: Any) -> tuple[str, Any] | None:
    """(field_type, canonical JSON value) of a scalar, or None to omit it."""
    if value is None:
        return None
    if isinstance(value, bool):
        return "bool", value
    if isinstance(value, int):
        return "int", value
    if isinstance(value, float):
        return ("float", value) if math.isfinite(value) else ("str", str(value))
    if isinstance(value, FileTime):
        iso = _filetime_iso(value)
        return None if iso is None else ("datetime", iso)
    if isinstance(value, str):
        return ("text" if "\n" in value or "\r" in value else "str"), value
    return "str", str(value)


def _make_field(key: str, label: str, value: Any, *, duration: bool = False) -> MetadataField | None:
    common = dict(editable=False, deletable=False, source="ole")
    if isinstance(value, Blob):
        mime = _clipboard_mime(value.data) if value.vt == VT_CF else None
        return MetadataField.binary_field(key, label, value.data, mime=mime,
                                          deletable=False, source="ole")
    if duration and isinstance(value, FileTime):
        return MetadataField(key, label, value.ticks / 10**7, raw_value=value.ticks,
                             field_type="duration", **common)
    if isinstance(value, list):
        if value and all(isinstance(v, Blob) for v in value):
            return MetadataField.binary_field(key, label, [v.data for v in value],
                                              deletable=False, source="ole")
        items = [s for s in (_scalar(v) for v in value) if s is not None]
        if not items:
            return None
        types = {t for t, _ in items}
        if len(types) == 1:
            field_type, values = types.pop(), [v for _, v in items]
        elif types <= {"str", "text"}:
            field_type, values = "text", [v for _, v in items]
        else:
            field_type, values = "str", [str(v) for _, v in items]
        return MetadataField(key, label, values, field_type=field_type, multi=True, **common)
    scalar = _scalar(value)
    if scalar is None:
        return None
    return MetadataField(key, label, scalar[1], field_type=scalar[0], **common)


def _heading_pairs(value: Any) -> Any:
    """HeadingPairs (heading, count, heading, count...) -> ["heading (count)", ...]."""
    if not isinstance(value, list) or len(value) % 2:
        return value
    pairs = []
    for heading, count in zip(value[::2], value[1::2]):
        if not isinstance(heading, str) or isinstance(count, bool) or not isinstance(count, int):
            return value
        pairs.append(f"{heading} ({count})")
    return pairs


class OleHandler(BaseMetadataHandler):
    NAME = "ole"
    EXTENSIONS = _OLE_EXTS
    PRIORITY = 15

    def read(self, path: Path) -> MetadataRecord:
        fields: list[MetadataField] = []
        errors: list[str] = []
        warnings: list[str] = []
        try:
            import olefile
            if not olefile.isOleFile(str(path)):
                errors.append("Not a valid OLE file")
            else:
                with olefile.OleFileIO(str(path)) as ole:
                    for stream in (SUMMARY_STREAM, DOC_SUMMARY_STREAM):
                        if not ole.exists(stream):
                            continue
                        data = ole.openstream(stream).read()
                        try:
                            sections = parse_property_set(data)
                        except (ValueError, struct.error) as exc:
                            errors.append(f"OLE read error: {stream.lstrip(chr(5))}: {exc}")
                            continue
                        for sec in sections:
                            fields += _section_fields(sec, warnings)
        except Exception as e:
            errors.append(f"OLE read error: {e}")

        record = self._make_record(path, read_errors=errors, warnings=warnings,
                                   supports_write=False, supports_delete=False)
        record.fields = fields
        record.dedupe_keys()
        return record

    def write(self, path: Path, fields: list[MetadataField]) -> None:
        raise NotImplementedError("OLE write not supported — use docx/xlsx/pptx format")

    def delete(self, path: Path, keys: list[str]) -> None:
        raise NotImplementedError("OLE delete not supported — use docx/xlsx/pptx format")


def _section_fields(sec: Section, warnings: list[str]) -> list[MetadataField]:
    if sec.fmtid == FMTID_SUMMARY:
        table, where = _SUMMARY_PROPS, "SummaryInformation"
    elif sec.fmtid == FMTID_DOC_SUMMARY:
        table, where = _DOC_SUMMARY_PROPS, "DocumentSummaryInformation"
    elif sec.fmtid == FMTID_USER_DEFINED:
        table, where = None, "custom properties"
    else:
        return []
    keys: dict[int, tuple[str, str]] = {}
    if table is None:
        for pid in sec.props:
            if pid < 2 or pid & 0x80000000:  # codepage, locale, behaviour flags
                continue
            name = sec.names.get(pid) or f"pid{pid}"
            keys[pid] = (f"ole:custom:{name}", f"Custom: {name}")
    else:
        keys = {pid: table[pid] for pid in sec.props if pid in table}

    fields = []
    for pid, (key, label) in keys.items():
        value = sec.props[pid]
        if sec.fmtid == FMTID_DOC_SUMMARY and pid == _HEADING_PAIRS_PID:
            value = _heading_pairs(value)
        f = _make_field(key, label, value,
                        duration=sec.fmtid == FMTID_SUMMARY and pid == _EDIT_TIME_PID)
        if f is not None:
            fields.append(f)
    for problem in sec.problems:
        pid = problem.split(":", 1)[0].removeprefix("property ")
        key = keys.get(int(pid), (None,))[0] if pid.isdigit() else None
        warnings.append(f"OLE {where}: {key + ': ' if key else ''}{problem}")
    return fields
