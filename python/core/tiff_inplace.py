"""Removal of metadata entries from TIFF-based files (TIFF, DNG and TIFF-based RAW) without moving anything.

An entry is taken out of its IFD by shifting the following entries up inside the same table (the table keeps
its place and size: the freed bytes at its end are zeroed) and the value it pointed to, when stored outside the
entry, is zeroed. No offset of the file changes, so strips, tiles, sub-IFDs, thumbnails and the MakerNote
(whose inner offsets are absolute) stay valid, and the removed data does not remain in the file.
``snapshot`` / ``insert`` bring an entry back (Undo). Only the metadata tags of ``removable`` are ever touched.
"""
from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass

from core.exif_codec import RAW_MAGICS, TYPE_SIZES

MAX_IFDS = 64
EXIF_POINTER, GPS_POINTER, INTEROP_POINTER, SUB_IFDS = 0x8769, 0x8825, 0xA005, 0x014A

# Entries that describe the picture or are needed to read the file: never removed.
_KEEP_EXIF = frozenset({0x9000, 0x9101, 0xA000, 0xA001, 0xA002, 0xA003, 0xA005})
_REMOVABLE_0TH = frozenset({0x010E, 0x010F, 0x0110, 0x0131, 0x0132, 0x013B, 0x013C, 0x8298,
                            0x9C9B, 0x9C9C, 0x9C9D, 0x9C9E, 0x9C9F, 0xC614, 0xC62F, 0xC634, 0x8773})


@dataclass(frozen=True)
class Entry:
    ifd: str
    tag: int
    type: int
    count: int
    entry_at: int           # offset of the 12-byte entry
    data_at: int | None     # offset of the value when stored outside the entry
    size: int               # size of the value in bytes


@dataclass(frozen=True)
class Ifd:
    name: str
    offset: int
    entries: tuple[Entry, ...]
    next_at: int | None     # offset of the next-IFD pointer (None: the writer left it out)


class TiffError(ValueError):
    pass


def removable(ifd: str, tag: int) -> bool:
    """Whether the entry is metadata that can be removed (the picture's structure never is)."""
    if ifd == "0th":
        return tag in _REMOVABLE_0TH
    if ifd == "Exif":
        return tag not in _KEEP_EXIF
    return ifd == "GPS"


def _byte_order(data) -> str:
    head = bytes(data[:4])
    if head == b"II*\x00":
        return "<"
    if head == b"MM\x00*":
        return ">"
    if head in RAW_MAGICS:
        return RAW_MAGICS[head]
    raise TiffError("not a TIFF structure")


def parse(data) -> tuple[str, dict[str, Ifd]]:
    """Byte order and the IFDs reachable from the header (``0th``, ``Exif``, ``GPS``, ``Interop``, ``1st``)."""
    order = _byte_order(data)
    size = len(data)
    ifds: dict[str, Ifd] = {}
    seen: set[int] = set()

    def read(name: str, offset: int) -> Ifd | None:
        if offset in seen or offset < 8 or offset + 2 > size or len(seen) >= MAX_IFDS:
            return None
        seen.add(offset)
        count = struct.unpack_from(order + "H", data, offset)[0]
        end = offset + 2 + 12 * count
        if end > size:
            return None
        entries = []
        for i in range(count):
            at = offset + 2 + 12 * i
            tag, typ, n = struct.unpack_from(order + "HHI", data, at)
            width = TYPE_SIZES.get(typ)
            if width is None:
                entries.append(Entry(name, tag, typ, n, at, None, 0))
                continue
            length = width * n
            entries.append(Entry(name, tag, typ, n, at, struct.unpack_from(order + "I", data, at + 8)[0] if length > 4 else None,
                                 length))
        # Some writers leave out the pointer of the last IFD: its value data then starts right after the table.
        has_pointer = end + 4 <= size and not any(
            e.data_at is not None and e.data_at < end + 4 and end < e.data_at + e.size for e in entries)
        ifd = Ifd(name, offset, tuple(entries), end if has_pointer else None)
        ifds[name] = ifd
        return ifd

    first = read("0th", struct.unpack_from(order + "I", data, 4)[0])
    if first is None:
        raise TiffError("the first IFD cannot be read")
    for entry in first.entries:
        if entry.tag in (EXIF_POINTER, GPS_POINTER) and entry.type in (4, 13) and entry.count == 1:
            target = struct.unpack_from(order + "I", data, entry.entry_at + 8)[0]
            sub = read("Exif" if entry.tag == EXIF_POINTER else "GPS", target)
            if sub is not None and sub.name == "Exif":
                for inner in sub.entries:
                    if inner.tag == INTEROP_POINTER and inner.type in (4, 13) and inner.count == 1:
                        read("Interop", struct.unpack_from(order + "I", data, inner.entry_at + 8)[0])
    following = struct.unpack_from(order + "I", data, first.next_at)[0] if first.next_at is not None else 0
    if following:
        read("1st", following)
    # A pointer overlapped by the value data of another IFD is not a pointer either.
    spans = [(e.data_at, e.data_at + e.size) for i in ifds.values() for e in i.entries if e.data_at is not None]
    for name, ifd in list(ifds.items()):
        if ifd.next_at is not None and any(a < ifd.next_at + 4 and ifd.next_at < b for a, b in spans):
            ifds[name] = Ifd(name, ifd.offset, ifd.entries, None)
    return order, ifds


def _ranges(ifds: dict[str, Ifd]) -> list[tuple[int, int, str, int]]:
    return [(e.data_at, e.data_at + e.size, e.ifd, e.tag) for ifd in ifds.values() for e in ifd.entries
            if e.data_at is not None]


def remove(data: bytearray, ifd_name: str, tags: set[int], force: bool = False) -> int:
    """Remove ``tags`` from one IFD in place; the number of entries removed."""
    order, ifds = parse(data)
    ifd = ifds.get(ifd_name)
    if ifd is None:
        return 0
    doomed = [e for e in ifd.entries if e.tag in tags and (force or removable(ifd_name, e.tag))]
    if not doomed:
        return 0
    kept = [e for e in ifd.entries if e not in doomed]
    other_ranges = [(a, b) for a, b, name, tag in _ranges(ifds) if not (name == ifd_name and tag in tags)]
    # Own data of the removed entries, unless another entry points into the same bytes.
    for entry in doomed:
        if entry.data_at is not None and not any(a < entry.data_at + entry.size and entry.data_at < b
                                                 for a, b in other_ranges):
            data[entry.data_at:entry.data_at + entry.size] = bytes(entry.size)
    table = b"".join(bytes(data[e.entry_at:e.entry_at + 12]) for e in kept)
    next_pointer = bytes(data[ifd.next_at:ifd.next_at + 4]) if ifd.next_at is not None else b""
    start = ifd.offset
    old_end = start + 2 + 12 * len(ifd.entries) + len(next_pointer)
    data[start:old_end] = bytes(old_end - start)
    data[start:start + 2] = struct.pack(order + "H", len(kept))
    data[start + 2:start + 2 + len(table)] = table
    data[start + 2 + len(table):start + 2 + len(table) + len(next_pointer)] = next_pointer
    return len(doomed)


def snapshot(data, ifd_name: str, tag: int):
    """What is needed to bring an entry back: ``(ifd, entry bytes, data offset, data bytes, table has a
    next-IFD pointer)`` or ``None``."""
    _, ifds = parse(data)
    ifd = ifds.get(ifd_name)
    entry = next((e for e in ifd.entries if e.tag == tag), None) if ifd else None
    if entry is None:
        return None
    value = bytes(data[entry.data_at:entry.data_at + entry.size]) if entry.data_at is not None else b""
    return (ifd_name, bytes(data[entry.entry_at:entry.entry_at + 12]), entry.data_at, value, ifd.next_at is not None)


def insert(data: bytearray, saved) -> None:
    """Put a snapshot taken by ``snapshot`` back: the value first, then the entry at its place in the table."""
    ifd_name, raw, data_at, value, has_pointer = saved
    order, ifds = parse(data)
    ifd = ifds.get(ifd_name)
    if ifd is None:
        raise TiffError(f"the {ifd_name} table is gone")
    tag = struct.unpack_from(order + "H", raw, 0)[0]
    if any(e.tag == tag for e in ifd.entries):
        return
    table_end = ifd.offset + 2 + 12 * len(ifd.entries) + (4 if has_pointer else 0)
    free = bytes(data[table_end:table_end + 12])
    if len(free) < 12 or any(free):
        raise TiffError("the room left by the removed entry is no longer free")
    if data_at is not None:
        data[data_at:data_at + len(value)] = value
    entries = list(ifd.entries)
    position = next((i for i, e in enumerate(entries) if e.tag > tag), len(entries))
    table = b"".join(bytes(data[e.entry_at:e.entry_at + 12]) for e in entries)
    table = table[:12 * position] + raw + table[12 * position:]
    next_pointer = bytes(data[table_end - 4:table_end]) if has_pointer else b""
    start = ifd.offset
    data[start:start + 2] = struct.pack(order + "H", len(entries) + 1)
    data[start + 2:start + 2 + len(table)] = table
    data[start + 2 + len(table):start + 2 + len(table) + len(next_pointer)] = next_pointer


def media_digest(data) -> str | None:
    """SHA-256 of the strips / tiles / JPEG data the IFDs of the file point to (``None`` if unreadable)."""
    try:
        order, ifds = parse(data)
    except (TiffError, struct.error):
        return None
    digest = hashlib.sha256()
    ranges: list[tuple[int, int]] = []

    def values(entry: Entry) -> list[int]:
        width = TYPE_SIZES.get(entry.type)
        if width is None or entry.count > 1_000_000:
            return []
        at = entry.data_at if entry.data_at is not None else entry.entry_at + 8
        fmt = {1: "B", 3: "H", 4: "I", 8: "h", 9: "i"}.get(entry.type)
        return list(struct.unpack_from(order + fmt * entry.count, data, at)) if fmt else []

    for ifd in ifds.values():
        by_tag = {e.tag: e for e in ifd.entries}
        for offsets_tag, counts_tag in ((0x0111, 0x0117), (0x0144, 0x0145)):
            if offsets_tag in by_tag and counts_tag in by_tag:
                ranges += list(zip(values(by_tag[offsets_tag]), values(by_tag[counts_tag])))
        if 0x0201 in by_tag and 0x0202 in by_tag:
            ranges += list(zip(values(by_tag[0x0201]), values(by_tag[0x0202])))
    for start, length in sorted(set(ranges)):
        if start + length > len(data):
            return None
        digest.update(struct.pack(">QQ", start, length))
        digest.update(bytes(data[start:start + length]))
    return digest.hexdigest()


# ──────────────────────── editing: change in place, otherwise append ──────────────────────────

_POINTER_TAGS = {"Exif": (EXIF_POINTER, "0th"), "GPS": (GPS_POINTER, "0th"), "Interop": (INTEROP_POINTER, "Exif")}
_EDITABLE_0TH_EXTRA = frozenset({0x0112})          # Orientation


def editable(ifd: str, tag: int) -> bool:
    """Whether the entry may be changed or added (metadata only; never the picture structure)."""
    return removable(ifd, tag) or (ifd == "0th" and tag in _EDITABLE_0TH_EXTRA)


def _align(buf: bytearray) -> None:
    if len(buf) % 2:
        buf.append(0)


def _append(buf: bytearray, data: bytes) -> int:
    """Append ``data`` at an even offset; the offset it starts at."""
    _align(buf)
    offset = len(buf)
    if offset + len(data) >= 1 << 32:
        raise TiffError("the file would exceed 4 GiB")
    buf.extend(data)
    return offset


def _entry_bytes(order: str, tag: int, typ: int, count: int, value: bytes, offset: int | None) -> bytes:
    """A 12-byte entry: the value inline (left-justified) when it fits in four bytes, else its offset."""
    field = value.ljust(4, b"\x00") if offset is None else struct.pack(order + "I", offset)
    return struct.pack(order + "HHI", tag, typ, count) + field


def _zero_unshared(buf: bytearray, ifds: dict[str, Ifd], entry: Entry) -> None:
    if entry.data_at is None:
        return
    others = [(a, b) for a, b, name, tag in _ranges(ifds) if not (name == entry.ifd and tag == entry.tag)]
    if not any(a < entry.data_at + entry.size and entry.data_at < b for a, b in others):
        buf[entry.data_at:entry.data_at + entry.size] = bytes(entry.size)


def _point_to(buf: bytearray, order: str, ifds: dict[str, Ifd], name: str, offset: int) -> None:
    """Make the file reach IFD ``name`` at ``offset`` (header, parent entry or previous IFD's pointer)."""
    if name == "0th":
        buf[4:8] = struct.pack(order + "I", offset)
    elif name == "1st":
        first = ifds["0th"]
        if first.next_at is None:
            raise TiffError("the first IFD has no room for a next-IFD pointer")
        buf[first.next_at:first.next_at + 4] = struct.pack(order + "I", offset)
    else:
        tag, parent = _POINTER_TAGS[name]
        entry = next(e for e in ifds[parent].entries if e.tag == tag)
        buf[entry.entry_at + 8:entry.entry_at + 12] = struct.pack(order + "I", offset)


def ensure_ifd(buf: bytearray, name: str) -> None:
    """Create an empty ``Exif`` / ``GPS`` / ``Interop`` IFD at the end of the file when it is missing."""
    order, ifds = parse(buf)
    if name in ifds:
        return
    if name not in _POINTER_TAGS:
        raise TiffError(f"cannot create the {name} IFD")
    tag, parent = _POINTER_TAGS[name]
    if parent not in ifds:
        ensure_ifd(buf, parent)
    offset = _append(buf, struct.pack(order + "HI", 0, 0))
    set_entry(buf, parent, tag, 4, 1, struct.pack(order + "I", offset))


def set_entry(buf: bytearray, ifd_name: str, tag: int, typ: int, count: int, data: bytes) -> None:
    """Change or add one entry. The value is written over the old one when it fits, else it is appended at
    the end of the file and the entry points to it; a new entry makes the IFD table move to the end of the
    file (its out-of-line values and everything else stay where they are). The old bytes are zeroed."""
    width = TYPE_SIZES.get(typ)
    if width is None or width * count != len(data):
        raise TiffError("the value does not match its type")
    if ifd_name in _POINTER_TAGS and ifd_name not in parse(buf)[1]:
        ensure_ifd(buf, ifd_name)
    order, ifds = parse(buf)
    ifd = ifds.get(ifd_name)
    if ifd is None:
        raise TiffError(f"no {ifd_name} IFD")
    old = next((e for e in ifd.entries if e.tag == tag), None)
    if old is not None:
        fits = old.data_at is not None and len(data) > 4 and len(data) <= old.size
        if len(data) <= 4:
            offset = None
        elif fits:
            offset = old.data_at
            buf[offset:offset + len(data)] = data
            buf[offset + len(data):offset + old.size] = bytes(old.size - len(data))
        else:
            offset = _append(buf, data)
        if offset is not old.data_at:
            _zero_unshared(buf, ifds, old)
        buf[old.entry_at:old.entry_at + 12] = _entry_bytes(order, tag, typ, count, data, offset)
        return
    offset = None if len(data) <= 4 else _append(buf, data)
    entries = [bytes(buf[e.entry_at:e.entry_at + 12]) for e in ifd.entries]
    entries.append(_entry_bytes(order, tag, typ, count, data, offset))
    entries.sort(key=lambda raw: struct.unpack_from(order + "H", raw, 0)[0])
    following = bytes(buf[ifd.next_at:ifd.next_at + 4]) if ifd.next_at is not None else bytes(4)
    table = struct.pack(order + "H", len(entries)) + b"".join(entries) + following
    old_end = ifd.offset + 2 + 12 * len(ifd.entries) + (4 if ifd.next_at is not None else 0)
    new_at = _append(buf, table)
    buf[ifd.offset:old_end] = bytes(old_end - ifd.offset)
    _point_to(buf, order, ifds, ifd_name, new_at)


def put_entry(buf: bytearray, saved) -> None:
    """Undo of a change: write the entry (and its value) back as ``snapshot`` saw them. An entry that was
    moved by a later change is pointed back at its original place; the moved copy is zeroed."""
    ifd_name, raw, data_at, value, _has_pointer = saved
    order, ifds = parse(buf)
    ifd = ifds.get(ifd_name)
    tag = struct.unpack_from(order + "H", raw, 0)[0]
    current = next((e for e in ifd.entries if e.tag == tag), None) if ifd else None
    if current is None:
        insert(buf, saved)
        return
    if current.data_at is not None and current.data_at != data_at:
        _zero_unshared(buf, ifds, current)
    if data_at is not None:
        buf[data_at:data_at + len(value)] = value
    buf[current.entry_at:current.entry_at + 12] = raw
