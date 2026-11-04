"""Size-preserving removal of QuickTime / MP4 structure fields (the ``qt:`` namespace).

Nothing is ever moved: a removed atom is turned into a ``free`` atom of the same size with its payload
zeroed, a removed date is set to 0 (which means "not set"). The offsets of the media data therefore stay
valid and the media is never touched. ``plan`` computes the patches of the keys that can be removed,
``apply`` writes them; the original bytes are kept in the patch so that an Undo can write them back.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path

from core.isobmff import Box, Reader, open_reader


@dataclass(frozen=True)
class Patch:
    offset: int
    original: bytes
    replacement: bytes


def _zero(rd: Reader, offset: int, size: int) -> Patch:
    return Patch(offset, bytes(rd.buf[offset:offset + size]), bytes(size))


def _free(rd: Reader, box: Box) -> Patch | None:
    """The whole atom as a ``free`` atom of the same size (None when its header is not the plain 8 bytes)."""
    if box.data_start - box.start != 8:
        return None
    original = bytes(rd.buf[box.start:box.end])
    return Patch(box.start, original, struct.pack(">I", len(original)) + b"free" + bytes(len(original) - 8))


def _times(rd: Reader, box: Box) -> list[Patch]:
    """Creation and modification time of a ``mvhd`` / ``tkhd`` / ``mdhd`` atom."""
    base = box.data_start
    if base >= box.end:
        return []
    width = 8 if rd.buf[base] == 1 else 4
    if base + 4 + 2 * width > box.end:
        return []
    return [_zero(rd, base + 4, width), _zero(rd, base + 4 + width, width)]


def _mdta(rd: Reader, moov: Box, out: dict[str, list[Patch]]) -> None:
    meta = rd.find(moov, b"meta")
    if meta is None:
        return
    start = meta.data_start
    if bytes(rd.buf[start:start + 4]) == bytes(4):
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
    names: list[tuple[str, int, int]] = []              # name, offset of the name bytes, length
    pos = keys.data_start + 8
    for _ in range(min(count[0], 10_000) if count else 0):
        entry = rd.u("I4s", pos)
        if entry is None or entry[0] < 8 or pos + entry[0] > keys.end:
            break
        raw = bytes(rd.buf[pos + 8:pos + entry[0]])
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("latin-1")
        names.append((text, pos + 8, entry[0] - 8))
        pos += entry[0]
    seen: set[str] = set()
    for item in rd.children(ilst.data_start, ilst.end):
        index = struct.unpack(">I", item.type)[0]
        if not 1 <= index <= len(names):
            continue
        name, name_at, name_len = names[index - 1]
        if name in seen:
            continue
        seen.add(name)
        free = _free(rd, item)
        if free is not None:
            out[f"qt:mdta:{name}"] = [free, _zero(rd, name_at, name_len)]


def plan(path: Path) -> dict[str, list[Patch]]:
    """Patches that remove each removable ``qt:`` field of the file, by key."""
    out: dict[str, list[Patch]] = {}
    with open_reader(path) as rd:
        moov = rd.find(None, b"moov")
        if moov is None:
            return out
        mvhd = rd.find(moov, b"mvhd")
        if mvhd is not None:
            times = _times(rd, mvhd)
            if times:
                out["qt:CreateDate"], out["qt:ModifyDate"] = [times[0]], [times[1]]
        for trak in rd.find_all(moov, b"trak")[:64]:
            tkhd = rd.find(trak, b"tkhd")
            if tkhd is None:
                continue
            base = tkhd.data_start
            width = 8 if rd.buf[base] == 1 else 4
            track = rd.u("I", base + 4 + 2 * width)
            if track is None:
                continue
            patches = _times(rd, tkhd)
            mdhd = rd.find(trak, b"mdia", b"mdhd")
            if mdhd is not None:
                patches += _times(rd, mdhd)
            if patches:
                out[f"qt:track{track[0]}:CreateDate"] = patches
        _mdta(rd, moov, out)
        udta = rd.find(moov, b"udta")
        if udta is not None:
            for child in rd.children(udta.data_start, udta.end):
                if child.type[:1] == b"\xa9":
                    free = _free(rd, child)
                    if free is not None:
                        out[f"qt:udta:{child.type.decode('latin-1')}"] = [free]
    return out


def apply(path: Path, patches: list[Patch], restore: bool = False) -> None:
    """Write the patches (or their original bytes when ``restore``) into the file, in place."""
    with open(path, "r+b") as stream:
        for patch in patches:
            stream.seek(patch.offset)
            stream.write(patch.original if restore else patch.replacement)
