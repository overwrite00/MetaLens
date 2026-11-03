"""Structure-preserving TIFF/EXIF codec with typed values (plan F2A).

Unlike ``piexif.load``/``dump``, every IFD entry is kept with its ORIGINAL type,
count and value bytes, unknown tags included, so a rewrite only changes the
entries that were edited (offsets are recomputed). Supported layout (the one of
EXIF in JPEG): IFD0 -> Exif IFD (-> Interop IFD), GPS IFD, IFD1 + JPEG thumbnail.
Anything else that would need pointer relocation (strip/tile offsets, SubIFDs,
IFD chains longer than IFD1, unknown TIFF types, malformed thumbnail pointers)
is still READ, but the block is flagged ``rewrite_problems`` and must not be
rewritten. ``parse`` raises nothing but ``ExifFormatError``.

MakerNote: vendor MakerNotes often hold offsets that are absolute from the TIFF
header (Canon and others) and point inside the MakerNote itself. ``parse`` pins
the MakerNote to the offset it was read from (``ExifBlock.pins``) and
``serialize`` lays every other IFD, value and the thumbnail out around it, so
the MakerNote keeps both its bytes and its absolute position. Offsets relative to
the MakerNote (Nikon type 3, Olympus, Fujifilm) survive any position anyway.
Vendor data stored OUTSIDE the MakerNote's own byte range is not tracked.

Keys (stable, unique): ``exif:<Name>`` for a tag in its home IFD (Exif IFD for
Exif-table names, IFD0 otherwise), ``exif:<IFD0|Exif|Interop|IFD1>:<Name>`` for a
tag found elsewhere, ``gps:<Name>`` (``GPS`` prefix removed) for the GPS IFD.
``<Name>`` is the piexif table name or ``tag_<decimal id>``.
"""
from __future__ import annotations

import math
import re
import struct
from dataclasses import dataclass, field
from typing import Any

import piexif

from core import values
from core.errors import ErrorCode, MetadataValueError

# 129 = UTF-8 text (Exif 3.0): read as text, kept byte for byte on a rewrite.
TYPE_SIZES = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8, 13: 4, 129: 1}
_INT_FORMATS = {1: "B", 3: "H", 4: "L", 6: "b", 8: "h", 9: "l", 13: "L"}
_INT_RANGES = {1: (0, 255), 3: (0, 65535), 4: (0, 2**32 - 1), 6: (-128, 127),
               8: (-32768, 32767), 9: (-2**31, 2**31 - 1), 13: (0, 2**32 - 1)}

IFD_NAMES = ("0th", "Exif", "GPS", "Interop", "1st")
_IFD_LABEL = {"0th": "IFD0", "Exif": "Exif", "Interop": "Interop", "1st": "IFD1"}
_LABEL_IFD = {v: k for k, v in _IFD_LABEL.items()}
_TABLE = {"0th": "Image", "1st": "Image", "Exif": "Exif", "GPS": "GPS", "Interop": "Interop"}

EXIF_POINTER, GPS_POINTER, INTEROP_POINTER = 0x8769, 0x8825, 0xA005
THUMB_OFFSET, THUMB_LENGTH = 0x0201, 0x0202
POINTER_TAGS = frozenset({EXIF_POINTER, GPS_POINTER, INTEROP_POINTER, THUMB_OFFSET, THUMB_LENGTH})
# Entries whose value is an offset this codec does not relocate.
UNSUPPORTED_POINTERS = frozenset({0x0111, 0x0144, 0x014A})
# Never shown: pointers/offsets/byte counts (their values change on every rewrite).
HIDDEN_TAGS = frozenset({EXIF_POINTER, GPS_POINTER, INTEROP_POINTER, THUMB_OFFSET, THUMB_LENGTH,
                         0x0111, 0x0117, 0x0144, 0x0145, 0x014A})
MAKERNOTE = ("Exif", 0x927C)

DATETIME_TAGS = frozenset({("0th", 0x0132), ("Exif", 0x9003), ("Exif", 0x9004)})
XP_TAGS = frozenset(("0th", t) for t in range(0x9C9B, 0x9CA0))
CHARSET_TEXT_TAGS = frozenset({("Exif", 0x9286), ("GPS", 0x001B), ("GPS", 0x001C)})
VERSION_TAGS = frozenset({("Exif", 0x9000), ("Exif", 0xA000), ("Interop", 0x0002)})
# Image-structure tags: shown, not editable (changing them would misdescribe the data).
STRUCTURE_TAGS = frozenset(("0th", t) for t in (
    0x00FE, 0x0100, 0x0101, 0x0102, 0x0103, 0x0106, 0x0115, 0x011C, 0x0153, 0x0212))

CHOICES: dict[tuple[str, int], list] = {
    ("0th", 0x0112): [1, 2, 3, 4, 5, 6, 7, 8],          # Orientation
    ("0th", 0x0128): [1, 2, 3],                          # ResolutionUnit
    ("0th", 0x0213): [1, 2],                             # YCbCrPositioning
    ("Exif", 0x8822): [0, 1, 2, 3, 4, 5, 6, 7, 8],       # ExposureProgram
    ("Exif", 0x9207): [0, 1, 2, 3, 4, 5, 6, 255],        # MeteringMode
    ("Exif", 0xA001): [1, 65535],                        # ColorSpace
    ("Exif", 0xA210): [1, 2, 3, 4, 5],                   # FocalPlaneResolutionUnit
    ("Exif", 0xA217): [1, 2, 3, 4, 5, 7, 8],             # SensingMethod
    ("Exif", 0xA401): [0, 1],                            # CustomRendered
    ("Exif", 0xA402): [0, 1, 2],                         # ExposureMode
    ("Exif", 0xA403): [0, 1],                            # WhiteBalance
    ("Exif", 0xA406): [0, 1, 2, 3],                      # SceneCaptureType
    ("Exif", 0xA407): [0, 1, 2, 3, 4],                   # GainControl
    ("Exif", 0xA408): [0, 1, 2],                         # Contrast
    ("Exif", 0xA409): [0, 1, 2],                         # Saturation
    ("Exif", 0xA40A): [0, 1, 2],                         # Sharpness
    ("Exif", 0xA40C): [0, 1, 2, 3],                      # SubjectDistanceRange
    ("GPS", 0x0001): ["N", "S"], ("GPS", 0x0003): ["E", "W"], ("GPS", 0x0005): [0, 1],
    ("GPS", 0x0009): ["A", "V"], ("GPS", 0x000A): ["2", "3"],
    ("GPS", 0x000C): ["K", "M", "N"], ("GPS", 0x000E): ["T", "M"], ("GPS", 0x0010): ["T", "M"],
    ("GPS", 0x0013): ["N", "S"], ("GPS", 0x0015): ["E", "W"], ("GPS", 0x0017): ["T", "M"],
    ("GPS", 0x0019): ["K", "M", "N"], ("GPS", 0x001E): [0, 1],
}

LABELS = {
    "exif:ImageDescription": "Description", "exif:Make": "Camera Make", "exif:Model": "Camera Model",
    "exif:DateTimeOriginal": "Date Taken", "exif:DateTimeDigitized": "Date Digitized",
    "exif:DateTime": "Date Modified", "exif:FocalLengthIn35mmFilm": "Focal Length (35mm equiv)",
    "exif:PixelXDimension": "Pixel Width", "exif:PixelYDimension": "Pixel Height",
    "exif:ShutterSpeedValue": "Shutter Speed", "exif:ApertureValue": "Aperture",
    "exif:BrightnessValue": "Brightness", "exif:ExposureBiasValue": "Exposure Bias",
    "exif:MaxApertureValue": "Max Aperture", "exif:Thumbnail": "Thumbnail",
    "gps:ImgDirection": "GPS Direction", "gps:ImgDirectionRef": "GPS Direction Ref",
    "gps:TimeStamp": "GPS Timestamp", "gps:DateStamp": "GPS Date",
}

# Fields that can be added to a JPEG without Exif entry for them: (ifd, tag).
ADDABLE = (
    ("0th", 0x010E), ("0th", 0x010F), ("0th", 0x0110), ("0th", 0x0131), ("0th", 0x013B),
    ("0th", 0x8298), ("0th", 0x0132), ("Exif", 0x9003), ("Exif", 0x9004), ("0th", 0x0112),
    ("Exif", 0x9286), *sorted(XP_TAGS),
)

_BINARY_COUNT_LIMIT = 64
_EXIF_DATE_RE = re.compile(rb"([0-9]{4}):([0-9]{2}):([0-9]{2}) ([0-9]{2}):([0-9]{2}):([0-9]{2})")


class ExifFormatError(ValueError):
    """The TIFF/EXIF structure is malformed."""


@dataclass
class Entry:
    tag: int
    type: int
    count: int
    data: bytes     # exact value bytes, in the block's byte order


@dataclass
class ExifBlock:
    byteorder: str                                   # "<" or ">"
    ifds: dict[str, dict[int, Entry]] = field(default_factory=lambda: {n: {} for n in IFD_NAMES})
    thumbnail: bytes | None = None
    rewrite_problems: list[str] = field(default_factory=list)
    # (ifd, tag) -> absolute offset its out-of-line value must keep (the MakerNote).
    pins: dict[tuple[str, int], int] = field(default_factory=dict)

    @classmethod
    def empty(cls) -> "ExifBlock":
        return cls(">")

    def entry(self, ifd: str, tag: int) -> Entry | None:
        return self.ifds[ifd].get(tag)


# ──────────────────────── names / keys ───────────────────────────────────────

def _table_names(table: str) -> dict[str, int]:
    return {spec["name"]: tag for tag, spec in piexif.TAGS[table].items()}


_NAMES = {t: _table_names(t) for t in ("Image", "Exif", "GPS", "Interop")}
_EXIF_TABLE_NAMES = frozenset(_NAMES["Exif"])


def _tag_name(ifd: str, tag: int) -> str:
    spec = piexif.TAGS[_TABLE[ifd]].get(tag)
    if spec is None:
        return f"tag_{tag}"
    name = spec["name"]
    if ifd == "GPS" and name.startswith("GPS"):
        name = name[3:]
    return name


def _home_ifd(name: str) -> str:
    return "Exif" if name in _EXIF_TABLE_NAMES else "0th"


def key_for(ifd: str, tag: int) -> str:
    name = _tag_name(ifd, tag)
    if ifd == "GPS":
        return f"gps:{name}"
    if ifd == _home_ifd(name):
        return f"exif:{name}"
    return f"exif:{_IFD_LABEL[ifd]}:{name}"


def _tag_from_name(ifd: str, name: str) -> int | None:
    if name.startswith("tag_") and name[4:].isdigit():
        tag = int(name[4:])
        if tag > 0xFFFF or piexif.TAGS[_TABLE[ifd]].get(tag) is not None:
            return None  # a known tag has a name; tag_<n> is reserved for unknown ones
        return tag
    lookup = ("GPS" + name) if ifd == "GPS" else name
    return _NAMES[_TABLE[ifd]].get(lookup)


def parse_key(key: str) -> tuple[str, int] | None:
    """``key`` -> (ifd, tag), or None when it is not an EXIF/GPS key of this codec."""
    if key.startswith("gps:"):
        ifd, name = "GPS", key[4:]
    elif key.startswith("exif:"):
        rest = key[5:]
        label, sep, name = rest.partition(":")
        if sep:
            if label not in _LABEL_IFD:
                return None
            ifd = _LABEL_IFD[label]
            if ifd == _home_ifd(name) and not name.startswith("tag_"):
                return None  # unqualified form is the canonical one
        else:
            name, ifd = rest, _home_ifd(rest)
    else:
        return None
    tag = _tag_from_name(ifd, name)
    if tag is None or key_for(ifd, tag) != key:
        return None
    return ifd, tag


def label_for(key: str, ifd: str, tag: int) -> str:
    if key in LABELS:
        return LABELS[key]
    name = _tag_name(ifd, tag)
    words = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", name).replace("_", " ")
    prefix = "GPS " if ifd == "GPS" else ("" if key.count(":") == 1 else f"{_IFD_LABEL[ifd]} ")
    return prefix + words


def spec_type(ifd: str, tag: int) -> int | None:
    spec = piexif.TAGS[_TABLE[ifd]].get(tag)
    return None if spec is None else spec["type"]


# ──────────────────────── parse ──────────────────────────────────────────────

# Magic of TIFF-based raw files whose IFDs are laid out like TIFF (Olympus ORF, Panasonic RW2/RWL).
RAW_MAGICS = {b"IIRO": "<", b"IIRS": "<", b"MMOR": ">", b"IIU\x00": "<"}


def parse(tiff: bytes, *, with_ifd1: bool = True, raw: bool = False) -> ExifBlock:
    """Parse a TIFF structure (``Exif\\0\\0`` already stripped).

    ``raw=True`` also accepts the headers of ORF/RW2 raw files (read only: such a
    block is never rewritten).

    Raises ``ExifFormatError`` and nothing else, whatever the input bytes: a
    malformed block is a read error of the caller, never an unhandled exception.
    """
    view = tiff if isinstance(tiff, memoryview) else memoryview(tiff)
    try:
        return _parse(view, with_ifd1, raw)
    except ExifFormatError:
        raise
    except (struct.error, IndexError, KeyError, ValueError, TypeError, OverflowError) as exc:
        raise ExifFormatError(f"malformed EXIF ({type(exc).__name__}: {exc})") from exc
    finally:
        if view is not tiff:
            # A traceback still referencing the view must not keep the buffer (an mmap) exported.
            view.release()


def _pointer_value(entry: Entry, bo: str, what: str) -> int:
    """Value of an offset/length entry: exactly one SHORT or LONG."""
    if entry.type not in (3, 4) or entry.count != 1 or len(entry.data) != TYPE_SIZES[entry.type]:
        raise ExifFormatError(f"{what} must be one SHORT or LONG (type {entry.type}, count {entry.count})")
    return struct.unpack(bo + _INT_FORMATS[entry.type], entry.data)[0]


def _parse(data: memoryview, with_ifd1: bool, raw: bool = False) -> ExifBlock:
    size = len(data)
    if size < 8:
        raise ExifFormatError("EXIF block too short")
    head = bytes(data[:4])
    if head == b"II*\x00":
        bo = "<"
    elif head == b"MM\x00*":
        bo = ">"
    elif raw and head in RAW_MAGICS:
        bo = RAW_MAGICS[head]
    else:
        raise ExifFormatError("invalid TIFF header")
    block = ExifBlock(bo)
    visited: set[int] = set()

    def u16(off: int) -> int:
        return struct.unpack_from(bo + "H", data, off)[0]

    def u32(off: int) -> int:
        return struct.unpack_from(bo + "L", data, off)[0]

    def read_ifd(name: str, offset: int) -> int:
        if offset in visited:
            raise ExifFormatError(f"IFD loop at offset {offset}")
        visited.add(offset)
        if offset + 2 > size:
            raise ExifFormatError(f"{name} IFD offset {offset} outside the block")
        count = u16(offset)
        end = offset + 2 + 12 * count
        if end + 4 > size:
            if end <= size:  # some writers omit the next-IFD pointer of the last IFD
                next_offset = 0
            else:
                raise ExifFormatError(f"{name} IFD truncated")
        else:
            next_offset = u32(end)
        entries = block.ifds[name]
        for i in range(count):
            base = offset + 2 + 12 * i
            tag, typ, n = u16(base), u16(base + 2), u32(base + 4)
            if typ not in TYPE_SIZES:
                block.rewrite_problems.append(f"{name} tag {tag}: unknown TIFF type {typ}")
                value = bytes(data[base + 8:base + 12])
                entries.setdefault(tag, Entry(tag, typ, n, value))
                continue
            length = TYPE_SIZES[typ] * n
            start = None
            if length <= 4:
                value = bytes(data[base + 8:base + 8 + length])
            else:
                start = u32(base + 8)
                if start + length > size:
                    raise ExifFormatError(f"{name} tag {tag}: value outside the block")
                value = bytes(data[start:start + length])
            if tag in entries:
                block.rewrite_problems.append(f"{name} tag {tag} appears twice")
                continue
            entries[tag] = Entry(tag, typ, n, value)
            if (name, tag) == MAKERNOTE and start is not None and start >= 8:
                block.pins[MAKERNOTE] = start   # keep its absolute position on a rewrite
        return next_offset

    def pointer(name: str, tag: int) -> int | None:
        entry = block.ifds[name].get(tag)
        if entry is None:
            return None
        if entry.type not in (4, 13) or entry.count != 1:
            raise ExifFormatError(f"{name} pointer tag {tag} has an invalid type")
        return struct.unpack(bo + "L", entry.data)[0]

    next_ifd = read_ifd("0th", u32(4))
    for sub, tag in (("Exif", EXIF_POINTER), ("GPS", GPS_POINTER)):
        offset = pointer("0th", tag)
        if offset:
            read_ifd(sub, offset)
    offset = pointer("Exif", INTEROP_POINTER) if block.ifds["Exif"] else None
    if offset:
        read_ifd("Interop", offset)
    if with_ifd1 and next_ifd:
        if read_ifd("1st", next_ifd):
            block.rewrite_problems.append("IFD chain longer than IFD1")
        ifd1 = block.ifds["1st"]
        if THUMB_OFFSET in ifd1 and THUMB_LENGTH in ifd1:
            try:
                start = _pointer_value(ifd1[THUMB_OFFSET], bo, "JPEGInterchangeFormat")
                length = _pointer_value(ifd1[THUMB_LENGTH], bo, "JPEGInterchangeFormatLength")
            except ExifFormatError as exc:
                block.rewrite_problems.append(f"IFD1 thumbnail pointer: {exc}")
            else:
                if start + length <= size:
                    block.thumbnail = bytes(data[start:start + length])
                else:
                    block.rewrite_problems.append("thumbnail outside the EXIF block")
        elif THUMB_OFFSET in ifd1 or THUMB_LENGTH in ifd1:
            block.rewrite_problems.append("incomplete thumbnail pointers")
    for name, entries in block.ifds.items():
        for tag in entries:
            if tag in UNSUPPORTED_POINTERS:
                block.rewrite_problems.append(f"{name} tag {tag} holds offsets this codec cannot relocate")
            elif tag in (EXIF_POINTER, GPS_POINTER) and name != "0th" or \
                    tag == INTEROP_POINTER and name != "Exif" or \
                    tag in (THUMB_OFFSET, THUMB_LENGTH) and name != "1st":
                block.rewrite_problems.append(f"{name} tag {tag}: pointer in an unexpected IFD")
    return block


# ──────────────────────── serialize ──────────────────────────────────────────

class _Layout:
    """First-fit placement of word-aligned chunks after the 8-byte TIFF header,
    around ranges reserved at fixed offsets (pins)."""

    def __init__(self) -> None:
        self.free: list[list] = [[8, None]]          # [start, end) intervals; None = unbounded
        self.end = 8

    def reserve(self, start: int, size: int) -> bool:
        stop = start + size
        for i, (a, b) in enumerate(self.free):
            if a <= start and (b is None or stop <= b):
                pieces = ([[a, start]] if a < start else []) + ([[stop, b]] if b is None or stop < b else [])
                self.free[i:i + 1] = pieces
                self.end = max(self.end, stop)
                return True
        return False

    def place(self, size: int) -> int:
        for i, (a, b) in enumerate(self.free):
            start = a + (a & 1)
            if b is None or start + size <= b:
                stop = start + size
                self.free[i:i + 1] = [[stop, b]] if b is None or stop < b else []
                self.end = max(self.end, stop)
                return start
        raise AssertionError("unbounded free interval missing")  # pragma: no cover


def serialize(block: ExifBlock) -> bytes:
    """Rebuild the TIFF structure; entries keep their type/count/bytes.

    Pinned values (``block.pins``, i.e. the MakerNote) are written at their
    recorded absolute offset; the IFD tables, the other out-of-line values and the
    thumbnail are placed first-fit (word aligned) in the space around them, in the
    order IFD0, Exif, Interop, GPS, IFD1, thumbnail. Unused gaps are zero bytes.
    """
    if block.rewrite_problems:
        raise ExifFormatError("EXIF block cannot be rewritten: " + "; ".join(block.rewrite_problems))
    bo = block.byteorder
    ifds = {n: {t: e for t, e in block.ifds[n].items() if t not in POINTER_TAGS} for n in IFD_NAMES}
    has = {n: bool(ifds[n]) for n in IFD_NAMES}
    has["Exif"] = has["Exif"] or has["Interop"]
    has["1st"] = has["1st"] or block.thumbnail is not None
    placeholder = {}
    for name, tag, needed in (("0th", EXIF_POINTER, has["Exif"]), ("0th", GPS_POINTER, has["GPS"]),
                              ("Exif", INTEROP_POINTER, has["Interop"]),
                              ("1st", THUMB_OFFSET, block.thumbnail is not None),
                              ("1st", THUMB_LENGTH, block.thumbnail is not None)):
        if needed:
            placeholder[(name, tag)] = ifds[name][tag] = Entry(tag, 4, 1, b"\x00" * 4)

    order = [n for n in ("0th", "Exif", "Interop", "GPS", "1st") if n == "0th" or has[n]]
    layout = _Layout()
    value_at: dict[tuple[str, int], int] = {}
    for (name, tag), offset in sorted(block.pins.items()):
        entry = ifds.get(name, {}).get(tag)
        if entry is not None and len(entry.data) > 4 and offset >= 8 and layout.reserve(offset, len(entry.data)):
            value_at[(name, tag)] = offset
    table_at: dict[str, int] = {}
    for name in order:
        entries = ifds[name]
        table_at[name] = layout.place(2 + 12 * len(entries) + 4)
        for tag in sorted(entries):
            if len(entries[tag].data) > 4 and (name, tag) not in value_at:
                value_at[(name, tag)] = layout.place(len(entries[tag].data))
    thumb_at = layout.place(len(block.thumbnail)) if block.thumbnail is not None else 0
    pointers = {EXIF_POINTER: table_at.get("Exif"), GPS_POINTER: table_at.get("GPS"),
                INTEROP_POINTER: table_at.get("Interop"), THUMB_OFFSET: thumb_at,
                THUMB_LENGTH: len(block.thumbnail) if block.thumbnail is not None else 0}

    out = bytearray(layout.end)
    out[0:8] = (b"II*\x00" if bo == "<" else b"MM\x00*") + struct.pack(bo + "L", table_at["0th"])
    for name in order:
        entries = ifds[name]
        table = bytearray(struct.pack(bo + "H", len(entries)))
        for tag in sorted(entries):
            e = entries[tag]
            value = struct.pack(bo + "L", pointers[tag]) if placeholder.get((name, tag)) is e else e.data
            table += struct.pack(bo + "HHL", tag, e.type, e.count)
            if len(value) <= 4:
                table += value + b"\x00" * (4 - len(value))
            else:
                at = value_at[(name, tag)]
                table += struct.pack(bo + "L", at)
                out[at:at + len(value)] = value
        next_ifd = table_at["1st"] if name == "0th" and "1st" in table_at else 0
        table += struct.pack(bo + "L", next_ifd)
        out[table_at[name]:table_at[name] + len(table)] = table
    if block.thumbnail is not None:
        out[thumb_at:thumb_at + len(block.thumbnail)] = block.thumbnail
    return bytes(out)


def same_content(a: ExifBlock, b: ExifBlock) -> bool:
    """Same byte order, entries (tag/type/count/value bytes; offset pointers excluded)
    and thumbnail: what a reader can see is identical, whatever the layout."""
    def view(block: ExifBlock):
        return (block.byteorder, block.thumbnail,
                {n: {t: (e.type, e.count, e.data) for t, e in block.ifds[n].items() if t not in POINTER_TAGS}
                 for n in IFD_NAMES})
    return view(a) == view(b)


# ──────────────────────── typed values ───────────────────────────────────────

def _int_values(entry: Entry, bo: str) -> tuple[int, ...]:
    fmt = _INT_FORMATS[entry.type]
    return struct.unpack(bo + fmt * entry.count, entry.data)


def _rationals(entry: Entry, bo: str) -> list[str]:
    fmt = "LL" if entry.type == 5 else "ll"
    nums = struct.unpack(bo + fmt * entry.count, entry.data)
    return [f"{nums[2 * i]}/{nums[2 * i + 1]}" for i in range(entry.count)]


def _utf16(bo: str) -> str:
    return "utf-16-le" if bo == "<" else "utf-16-be"


@dataclass
class Decoded:
    field_type: str
    value: Any
    multi: bool = False
    length: int | None = None
    choices: list = field(default_factory=list)
    editable: bool = True
    binary: bytes | None = None          # payload of a binary field
    kind: str = "plain"                  # how to re-encode: ascii/datetime/xp/charset/int/rational


def _scalar_or_list(items: list, field_type: str, choices: list | None, editable: bool,
                    kind: str) -> Decoded:
    if len(items) == 1:
        if choices and items[0] in choices:
            return Decoded("enum", items[0], choices=list(choices), editable=editable, kind=kind)
        return Decoded(field_type, items[0], editable=editable, kind=kind)
    return Decoded(field_type, list(items), multi=True, length=len(items), editable=editable, kind=kind)


def decode(ifd: str, tag: int, entry: Entry, bo: str) -> Decoded:
    """Typed, lossless-on-reencode view of one entry."""
    where = (ifd, tag)
    editable = ifd in ("0th", "Exif", "GPS") and where not in STRUCTURE_TAGS
    t, n = entry.type, entry.count
    binary = Decoded("binary", None, editable=False, binary=entry.data, kind="binary")
    if t not in TYPE_SIZES or n == 0:
        return binary
    if where in XP_TAGS and t == 1:
        if len(entry.data) % 2:
            return binary
        try:
            text = entry.data.decode("utf-16-le")
        except UnicodeDecodeError:
            return binary
        return Decoded("str", text.rstrip("\x00"), editable=editable, kind="xp")
    if t == 129:
        return Decoded("str", values.decode_text(entry.data.rstrip(b"\x00"), ("utf-8",)), editable=False, kind="utf8")
    if t == 2:
        raw = entry.data.rstrip(b"\x00")
        if where in DATETIME_TAGS:
            m = _EXIF_DATE_RE.fullmatch(raw)
            if m:
                iso = "{}-{}-{}T{}:{}:{}".format(*(g.decode() for g in m.groups()))
                try:
                    values.parse_localdatetime(iso)
                    return Decoded("localdatetime", iso, editable=editable, kind="datetime")
                except MetadataValueError:
                    pass
            # Unparsable date ("    :  :  ", "0000:00:00 ..."): shown, not editable.
            return Decoded("str", values.decode_text(raw), editable=False, kind="ascii")
        text = values.decode_text(raw, ("utf-8",))
        choices = CHOICES.get(where)
        if choices and text in choices:
            return Decoded("enum", text, choices=list(choices), editable=editable, kind="ascii")
        return Decoded("str", text, editable=editable, kind="ascii")
    if t == 7:
        if where in CHARSET_TEXT_TAGS:
            text = _decode_charset_text(entry.data, bo)
            if text is None:
                return binary
            return Decoded("str", text, editable=editable, kind="charset")
        if where in VERSION_TAGS:
            if entry.data.isascii() and entry.data.decode("ascii").isprintable():
                return Decoded("str", entry.data.decode("ascii"), editable=False, kind="version")
        return binary
    if n > _BINARY_COUNT_LIMIT:
        return binary
    if t in _INT_FORMATS:
        items = list(_int_values(entry, bo))
        return _scalar_or_list(items, "int", CHOICES.get(where), editable and t != 13, "int")
    if t in (5, 10):
        return _scalar_or_list(_rationals(entry, bo), "rational", None, editable, "rational")
    # FLOAT / DOUBLE: shown, never editable (float32 cannot keep arbitrary input).
    fmt = "f" if t == 11 else "d"
    items = [float(x) for x in struct.unpack(bo + fmt * n, entry.data)]
    if not all(math.isfinite(x) for x in items):
        return binary
    dec = _scalar_or_list(items, "float", None, False, "float")
    return dec


_CHARSET_ASCII = b"ASCII\x00\x00\x00"
_CHARSET_UNICODE = b"UNICODE\x00"
_CHARSET_UNDEFINED = b"\x00" * 8


def _decode_charset_text(data: bytes, bo: str) -> str | None:
    if len(data) < 8:
        return None
    head, body = data[:8], data[8:]
    if head == _CHARSET_ASCII or head == _CHARSET_UNDEFINED:
        return values.decode_text(body.rstrip(b"\x00"), ("utf-8",))
    if head == _CHARSET_UNICODE:
        if len(body) % 2:
            return None
        encoding = _utf16(bo)
        if body[:2] in (b"\xff\xfe", b"\xfe\xff"):
            encoding = "utf-16"
        try:
            return body.decode(encoding).rstrip("\x00")
        except UnicodeDecodeError:
            return None
    return None  # JIS and unknown character codes: binary


def kind_for_new(ifd: str, tag: int) -> tuple[int, str] | None:
    """(TIFF type, encoding kind) used when ADDING the tag."""
    where = (ifd, tag)
    t = spec_type(ifd, tag)
    if t is None:
        return None
    if where in XP_TAGS:
        return 1, "xp"
    if where in DATETIME_TAGS:
        return 2, "datetime"
    if where in CHARSET_TEXT_TAGS:
        return 7, "charset"
    if t == 2:
        return 2, "ascii"
    if t in (1, 3, 4, 6, 8, 9):
        return t, "int"
    if t in (5, 10):
        return t, "rational"
    return None


def encode(key: str, tag: int, typ: int, kind: str, value: Any, bo: str,
           count: int | None = None) -> Entry:
    """Canonical ``value`` -> entry of type ``typ`` (range/size checked, errors on ``key``)."""
    def fail(code: str, message: str) -> MetadataValueError:
        return MetadataValueError(key, code, f"{key}: {message}")

    if kind == "ascii" or kind == "datetime":
        if kind == "datetime":
            dt = values.parse_localdatetime(value)
            if dt.microsecond:
                raise fail(ErrorCode.INVALID_VALUE, "EXIF date-times have no fractional seconds")
            text = dt.strftime("%Y:%m:%d %H:%M:%S")
            if len(text) != 19:
                raise fail(ErrorCode.OUT_OF_RANGE, "EXIF dates need a four-digit year")
        else:
            text = value
        if text.endswith("\x00"):
            raise fail(ErrorCode.INVALID_VALUE, "text must not end with a NUL character")
        data = _encode_utf8(key, text) + b"\x00"
        return Entry(tag, 2, len(data), data)
    if kind == "xp":
        if value.endswith("\x00"):
            raise fail(ErrorCode.INVALID_VALUE, "text must not end with a NUL character")
        data = _encode_utf16(key, value, "utf-16-le") + b"\x00\x00"
        return Entry(tag, 1, len(data), data)
    if kind == "charset":
        if value.isascii():
            data = _CHARSET_ASCII + value.encode("ascii")
        else:
            data = _CHARSET_UNICODE + _encode_utf16(key, value, _utf16(bo))
        return Entry(tag, 7, len(data), data)
    items = value if isinstance(value, list) else [value]
    if count is not None and len(items) != count:
        raise fail(ErrorCode.WRONG_LENGTH, f"{len(items)} items, exactly {count} required")
    if kind == "int":
        low, high = _INT_RANGES[typ]
        for item in items:
            if type(item) is not int or not low <= item <= high:
                raise fail(ErrorCode.OUT_OF_RANGE, f"{item!r} outside [{low}, {high}] for TIFF type {typ}")
        return Entry(tag, typ, len(items), struct.pack(bo + _INT_FORMATS[typ] * len(items), *items))
    if kind == "rational":
        pairs = []
        for item in items:
            try:
                pairs += values.parse_rational(item, signed=(typ == 10))
            except MetadataValueError as exc:
                raise exc.with_key(key) from exc
        fmt = "LL" if typ == 5 else "ll"
        return Entry(tag, typ, len(items), struct.pack(bo + fmt * len(items), *pairs))
    raise fail(ErrorCode.READ_ONLY, "this EXIF data type is read-only")


def _encode_utf8(key: str, text: str) -> bytes:
    try:
        return text.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise MetadataValueError(key, ErrorCode.INVALID_VALUE, f"{key}: text is not encodable") from exc


def _encode_utf16(key: str, text: str, encoding: str) -> bytes:
    try:
        return text.encode(encoding)
    except UnicodeEncodeError as exc:
        raise MetadataValueError(key, ErrorCode.INVALID_VALUE, f"{key}: text is not encodable") from exc


def swap_entry(entry: Entry, bo_from: str, bo_to: str) -> Entry:
    """Same entry in another byte order (snapshot restored into a block with another order)."""
    if bo_from == bo_to or entry.type in (1, 2, 6, 7, 129) or entry.type not in TYPE_SIZES:
        return entry
    fmt = {3: "H", 4: "L", 5: "LL", 8: "h", 9: "l", 10: "ll", 11: "f", 12: "d", 13: "L"}[entry.type]
    vals = struct.unpack(bo_from + fmt * entry.count, entry.data)
    return Entry(entry.tag, entry.type, entry.count, struct.pack(bo_to + fmt * entry.count, *vals))
