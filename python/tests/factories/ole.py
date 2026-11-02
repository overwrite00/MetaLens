"""Legacy OLE samples (.doc/.xls/.ppt), generated as real Compound File Binary files.

Owned by phase F2F: only that phase appends samples here.

``olefile`` cannot create compound files, so this module carries a minimal
writer (CFB v3, 512-byte sectors, one FAT sector). Every stream is padded to at
least the 4096-byte mini-stream cutoff so it lives in regular sectors and no
mini FAT is needed; a property set may legitimately be followed by unused bytes.
Property sets follow [MS-OLEPS]: typed values padded to 4 bytes, strings in the
codepage of the set (PID 1), UTF-16LE when that codepage is 1200.

The builders below are also used directly by ``tests/test_ole_handler.py``.

Register with `@sample("name", ...)`; see `tests/factories/__init__.py`.
"""
from __future__ import annotations

import struct
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import registrar

SAMPLES: list = []
sample = registrar(SAMPLES, __name__)

# ──────────────────────── property sets ([MS-OLEPS]) ─────────────────────────

VT_I2, VT_I4, VT_R8, VT_BOOL, VT_VARIANT = 2, 3, 5, 11, 12
VT_UI4, VT_LPSTR, VT_LPWSTR, VT_FILETIME, VT_BLOB, VT_CF = 19, 30, 31, 64, 65, 71
VT_VECTOR = 0x1000

FMTID_SUMMARY = uuid.UUID("F29F85E0-4FF9-1068-AB91-08002B27B3D9")
FMTID_DOC_SUMMARY = uuid.UUID("D5CDD502-2E9C-101B-9397-08002B2CF9AE")
FMTID_USER_DEFINED = uuid.UUID("D5CDD505-2E9C-101B-9397-08002B2CF9AE")

SUMMARY_STREAM = "\x05SummaryInformation"
DOC_SUMMARY_STREAM = "\x05DocumentSummaryInformation"

_FILETIME_EPOCH = datetime(1601, 1, 1, tzinfo=timezone.utc)


def _pad4(data: bytes) -> bytes:
    return data + b"\0" * (-len(data) % 4)


def filetime(dt: datetime) -> int:
    """FILETIME ticks (100 ns since 1601-01-01 UTC) of an aware datetime."""
    delta = dt - _FILETIME_EPOCH
    return (delta.days * 86_400 + delta.seconds) * 10**7 + delta.microseconds * 10


def duration_ticks(seconds: float) -> int:
    return round(seconds * 10**7)


def lpstr_bytes(text: str, codepage: int) -> bytes:
    """CodePageString payload: Size (bytes, NUL included) + characters, unpadded."""
    if codepage == 1200:
        raw = text.encode("utf-16-le") + b"\0\0"
    else:
        codec = "utf-8" if codepage == 65001 else f"cp{codepage}"
        raw = text.encode(codec) + b"\0"
    return struct.pack("<I", len(raw)) + raw


def typed(vt: int, value) -> bytes:
    """One TypedPropertyValue (type, padding, value padded to 4 bytes).

    ``value`` by type: VT_I2/VT_I4/VT_UI4 int, VT_R8 float, VT_BOOL bool,
    VT_LPSTR ``(text, codepage)`` or raw ``bytes`` (Size + characters),
    VT_LPWSTR str, VT_FILETIME ticks, VT_CF/VT_BLOB bytes,
    VT_VECTOR|x a list of values of x, VT_VECTOR|VT_VARIANT a list of ``(vt, value)``.
    """
    head = struct.pack("<HH", vt, 0)
    if vt & VT_VECTOR:
        base = vt & ~VT_VECTOR
        body = struct.pack("<I", len(value))
        for item in value:
            if base == VT_VARIANT:
                body += typed(*item)
            else:
                body += typed(base, item)[4:]  # element without the type header
        return head + _pad4(body)
    if vt == VT_I2:
        body = struct.pack("<h", value)
    elif vt == VT_I4:
        body = struct.pack("<i", value)
    elif vt == VT_UI4:
        body = struct.pack("<I", value)
    elif vt == VT_R8:
        body = struct.pack("<d", value)
    elif vt == VT_BOOL:
        body = struct.pack("<H", 0xFFFF if value else 0)
    elif vt == VT_LPSTR:
        body = value if isinstance(value, bytes) else lpstr_bytes(*value)
    elif vt == VT_LPWSTR:
        chars = value.encode("utf-16-le") + b"\0\0"
        body = struct.pack("<I", len(chars) // 2) + chars
    elif vt == VT_FILETIME:
        body = struct.pack("<Q", value)
    elif vt in (VT_CF, VT_BLOB):
        body = struct.pack("<I", len(value)) + value
    else:
        raise ValueError(f"unsupported VT {vt}")
    return head + _pad4(body)


def dictionary(names: dict[int, str], codepage: int) -> bytes:
    """Dictionary property (PID 0) of a user-defined section."""
    body = struct.pack("<I", len(names))
    for pid, name in names.items():
        if codepage == 1200:
            chars = name.encode("utf-16-le") + b"\0\0"
            body += struct.pack("<II", pid, len(chars) // 2) + _pad4(chars)
        else:
            chars = name.encode("utf-8" if codepage == 65001 else f"cp{codepage}") + b"\0"
            body += struct.pack("<II", pid, len(chars)) + chars
    return _pad4(body)


def section(props: dict[int, bytes]) -> bytes:
    """A property set section from ``{pid: encoded value}`` (``typed``/``dictionary``)."""
    count = len(props)
    offset = 8 + 8 * count
    table = b""
    values = b""
    for pid, encoded in props.items():
        table += struct.pack("<II", pid, offset + len(values))
        values += encoded
    body = struct.pack("<I", count) + table + values
    return struct.pack("<I", 4 + len(body)) + body


def property_set(*sections: tuple[uuid.UUID, bytes]) -> bytes:
    """Property set stream: header + one (FMTID, section) per entry."""
    header = struct.pack("<HHI", 0xFFFE, 0, 0x00020006) + bytes(16) + struct.pack("<I", len(sections))
    offset = len(header) + 20 * len(sections)
    table = b""
    for fmtid, data in sections:
        table += fmtid.bytes_le + struct.pack("<I", offset)
        offset += len(data)
    return header + table + b"".join(data for _, data in sections)


# ──────────────────────── compound file writer ───────────────────────────────

_SECTOR = 512
_FREESECT, _ENDOFCHAIN, _FATSECT, _NOSTREAM = 0xFFFFFFFF, 0xFFFFFFFE, 0xFFFFFFFD, 0xFFFFFFFF


def _dir_entry(name: str, kind: int, *, child=_NOSTREAM, right=_NOSTREAM,
               start=_ENDOFCHAIN, size=0) -> bytes:
    encoded = name.encode("utf-16-le") + b"\0\0"
    assert len(encoded) <= 64, name
    return (encoded.ljust(64, b"\0") + struct.pack("<HBB", len(encoded), kind, 1)
            + struct.pack("<III", _NOSTREAM, right, child) + bytes(16) + struct.pack("<I", 0)
            + bytes(16) + struct.pack("<IQ", start, size))


def compound_file(streams: dict[str, bytes]) -> bytes:
    """Minimal valid CFB v3 holding ``streams`` in the root storage."""
    names = sorted(streams, key=lambda n: (len(n), n.upper()))  # CFB sibling order
    datas = [streams[n].ljust(4096, b"\0") for n in names]       # >= mini-stream cutoff
    dir_sectors = -(-(len(names) + 1) // 4)
    fat = [_FATSECT] + [_ENDOFCHAIN if i == dir_sectors - 1 else 2 + i for i in range(dir_sectors)]
    starts = []
    for data in datas:
        count = -(-len(data) // _SECTOR)
        first = len(fat)
        starts.append(first)
        fat += [first + i + 1 for i in range(count - 1)] + [_ENDOFCHAIN]
    assert len(fat) <= _SECTOR // 4, "sample too large for one FAT sector"
    fat += [_FREESECT] * (_SECTOR // 4 - len(fat))

    entries = [_dir_entry("Root Entry", 5, child=1 if names else _NOSTREAM)]
    for i, (name, data) in enumerate(zip(names, datas)):
        entries.append(_dir_entry(name, 2, right=i + 2 if i + 1 < len(names) else _NOSTREAM,
                                  start=starts[i], size=len(data)))
    while len(entries) % 4:
        entries.append(bytes(64) + struct.pack("<HBB", 0, 0, 0)
                       + struct.pack("<III", _NOSTREAM, _NOSTREAM, _NOSTREAM) + bytes(48))
    header = (bytes.fromhex("D0CF11E0A1B11AE1") + bytes(16)
              + struct.pack("<HHHHH", 0x3E, 3, 0xFFFE, 9, 6) + bytes(6)
              + struct.pack("<IIIIIIIII", 0, 1, 1, 0, 4096, _ENDOFCHAIN, 0, _ENDOFCHAIN, 0)
              + struct.pack("<I", 0) + struct.pack("<108I", *([_FREESECT] * 108)))
    assert len(header) == _SECTOR
    body = struct.pack(f"<{_SECTOR // 4}I", *fat) + b"".join(entries)
    body += b"".join(d.ljust(-(-len(d) // _SECTOR) * _SECTOR, b"\0") for d in datas)
    return header + body


# ──────────────────────── reference content ──────────────────────────────────

CREATED = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
MODIFIED = CREATED + timedelta(days=3, hours=2)
EDIT_SECONDS = 5400.5
# CF_DIB clipboard data: format tag -1 (Windows clipboard format) + CF_DIB (8) + pixels.
THUMBNAIL_CF = struct.pack("<iI", -1, 8) + bytes(range(40))
CUSTOM_NAMES = {2: "Progetto", 3: "Approvato", 4: "Revisione", 5: "Scadenza", 6: "Nota"}


def summary_section(codepage: int, *, title: str = "Relazione è",
                    author: str = "José Müller") -> bytes:
    s = lambda text: typed(VT_LPSTR, (text, codepage))  # noqa: E731
    return section({
        1: typed(VT_I2, codepage - 65536 if codepage > 32767 else codepage),
        2: s(title), 3: s("Oggetto"), 4: s(author), 5: s("uno; due"),
        6: s("Riga 1\r\nRiga 2"), 7: s("Normal.dot"), 8: s("Ultimo"), 9: s("7"),
        10: typed(VT_FILETIME, duration_ticks(EDIT_SECONDS)),
        11: typed(VT_FILETIME, 0),  # never printed: absent
        12: typed(VT_FILETIME, filetime(CREATED)),
        13: typed(VT_FILETIME, filetime(MODIFIED)),
        14: typed(VT_I4, 3), 15: typed(VT_I4, 250), 16: typed(VT_I4, 1400),
        17: typed(VT_CF, THUMBNAIL_CF),
        18: s("Microsoft Office Word"), 19: typed(VT_I4, 0),
    })


def doc_summary_section(codepage: int) -> bytes:
    s = lambda text: typed(VT_LPSTR, (text, codepage))  # noqa: E731
    return section({
        1: typed(VT_I2, codepage - 65536 if codepage > 32767 else codepage),
        2: s("Categoria"), 4: typed(VT_I4, 24576), 5: typed(VT_I4, 12),
        6: typed(VT_I4, 4), 11: typed(VT_BOOL, False),
        12: typed(VT_VECTOR | VT_VARIANT, [(VT_LPSTR, ("Titolo", codepage)), (VT_I4, 1),
                                           (VT_LPSTR, ("Fogli", codepage)), (VT_I4, 2)]),
        13: typed(VT_VECTOR | VT_LPSTR, [("Documento", codepage), ("Foglio è", codepage),
                                         ("Riepilogo", codepage)]),
        14: s("Direttore Società"), 15: s("Società €uro S.p.A."),
        16: typed(VT_BOOL, True), 17: typed(VT_I4, 1650), 23: typed(VT_I4, 0x000C0000),
    })


def user_defined_section(codepage: int) -> bytes:
    s = lambda text: typed(VT_LPSTR, (text, codepage))  # noqa: E731
    return section({
        0: dictionary(CUSTOM_NAMES, codepage),
        1: typed(VT_I2, codepage - 65536 if codepage > 32767 else codepage),
        2: s("Città nuova"), 3: typed(VT_BOOL, True), 4: typed(VT_I4, -5),
        5: typed(VT_FILETIME, filetime(MODIFIED)), 6: typed(VT_LPWSTR, "日本 €"),
    })


def build_office_file(path: Path, codepage: int, *, doc_summary: bool = True,
                      title: str = "Relazione è", author: str = "José Müller") -> Path:
    streams = {SUMMARY_STREAM: property_set(
        (FMTID_SUMMARY, summary_section(codepage, title=title, author=author)))}
    if doc_summary:
        streams[DOC_SUMMARY_STREAM] = property_set(
            (FMTID_DOC_SUMMARY, doc_summary_section(codepage)),
            (FMTID_USER_DEFINED, user_defined_section(codepage)))
    streams["WordDocument"] = b"\xec\xa5" + bytes(510)
    path.write_bytes(compound_file(streams))
    return path


@sample("ole_doc_cp1252")
def ole_doc_cp1252(tmp_path: Path) -> Path:
    return build_office_file(tmp_path / "legacy.doc", 1252)


@sample("ole_xls_utf8")
def ole_xls_utf8(tmp_path: Path) -> Path:
    return build_office_file(tmp_path / "legacy.xls", 65001, title="日本 € è")


@sample("ole_ppt_utf16")
def ole_ppt_utf16(tmp_path: Path) -> Path:
    return build_office_file(tmp_path / "legacy.ppt", 1200, title="日本 € è")


@sample("ole_doc_summary_only")
def ole_doc_summary_only(tmp_path: Path) -> Path:
    return build_office_file(tmp_path / "summary_only.doc", 1252, doc_summary=False)
