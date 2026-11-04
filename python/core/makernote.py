"""A few well-known fields of the vendor MakerNote (Canon, Nikon, Sony), read only.

The MakerNote itself is kept byte for byte when a file is rewritten; this module only lets its serial numbers,
owner name and similar identifying values be seen (and, by removing the MakerNote, be dropped)."""
from __future__ import annotations

import struct
from dataclasses import dataclass

_SIZES = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 7: 1, 9: 4, 10: 8}
_MAX_ENTRIES = 512


@dataclass(frozen=True)
class MakerValue:
    vendor: str
    name: str
    label: str
    value: str | int


# (tag) -> (name, label, kind) with kind "str" or "int" (first number of the value)
_CANON = {0x0006: ("ImageType", "Image type", "str"), 0x0007: ("FirmwareVersion", "Firmware version", "str"),
          0x0009: ("OwnerName", "Owner name", "str"), 0x000C: ("SerialNumber", "Serial number", "int"),
          0x0095: ("LensModel", "Lens model", "str"), 0x0096: ("InternalSerialNumber", "Internal serial number", "str")}
_NIKON = {0x0004: ("Quality", "Quality", "str"), 0x0007: ("FocusMode", "Focus mode", "str"),
          0x001D: ("SerialNumber", "Serial number", "str"), 0x00A0: ("CameraSerialNumber", "Camera serial number", "str"),
          0x00A7: ("ShutterCount", "Shutter count", "int")}
_SONY = {0x0102: ("Quality", "Quality", "int"), 0xB020: ("CreativeStyle", "Creative style", "str")}


def _entries(note: bytes, ifd_at: int, order: str, shift: int | None):
    """Entries of the IFD at ``ifd_at`` of ``note``; a value offset ``v`` is at ``v + shift`` in ``note`` (None: not known)."""
    try:
        count = struct.unpack_from(order + "H", note, ifd_at)[0]
    except struct.error:
        return
    for i in range(min(count, _MAX_ENTRIES)):
        at = ifd_at + 2 + 12 * i
        try:
            tag, typ, n = struct.unpack_from(order + "HHL", note, at)
        except struct.error:
            return
        size = _SIZES.get(typ)
        if size is None or n > 1 << 20:
            continue
        total = size * n
        if total <= 4:
            data = note[at + 8:at + 8 + total]
        else:
            if at + 12 > len(note):
                return
            off = struct.unpack_from(order + "L", note, at + 8)[0] + shift if shift is not None else -1
            if off < 0:
                continue
            data = note[off:off + total]
        if len(data) == total:
            yield tag, typ, n, data


def _value(order: str, typ: int, data: bytes, kind: str):
    if kind == "str":
        if typ not in (2, 1, 7):
            return None
        text = data.split(b"\x00", 1)[0].decode("ascii", "replace").strip()
        return text or None
    code = {1: "B", 3: "H", 4: "L"}.get(typ)
    if code is None:
        return None
    return struct.unpack_from(order + code, data, 0)[0]


def read(make: str, note: bytes, order: str, note_offset: int | None) -> list[MakerValue]:
    """Known fields of a MakerNote. ``order`` is the byte order of the EXIF block and ``note_offset`` the offset of
    the note from the start of the TIFF header (None if not known)."""
    make = (make or "").strip().lower()
    out: list[MakerValue] = []

    def collect(vendor, table, ifd_at, byte_order, shift):
        for tag, typ, _n, data in _entries(note, ifd_at, byte_order, shift):
            if tag in table:
                name, label, kind = table[tag]
                value = _value(byte_order, typ, data, kind)
                if value is not None:
                    out.append(MakerValue(vendor, name, label, value))

    if note.startswith(b"Nikon\x00") and len(note) > 18 and note[10:12] in (b"II", b"MM"):
        order2 = "<" if note[10:12] == b"II" else ">"
        first = struct.unpack_from(order2 + "L", note, 14)[0]
        collect("nikon", _NIKON, 10 + first, order2, 10)
    elif note.startswith(b"SONY DSC \x00\x00\x00"):
        collect("sony", _SONY, 12, order, -note_offset if note_offset is not None else None)
    elif make.startswith("canon"):
        collect("canon", _CANON, 0, order, -note_offset if note_offset is not None else None)
    return out
