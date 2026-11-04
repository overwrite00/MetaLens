"""Editing and adding QuickTime / MP4 structure fields (the ``qt:`` namespace).

Dates are written over the old ones (fixed width). Text atoms (``udta`` ``(c)xyz``, ``(c)mak``...) and text
values of the ``mdta`` keys are rebuilt inside ``moov`` and ``moov`` is resized. Nothing else is touched, and a
change that would move the media data is refused: the size of ``moov`` may change only when nothing that is addressed by an
offset follows it or when the freed bytes can be turned into a ``free`` atom (a shorter value).
"""
from __future__ import annotations

import re
import struct
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from core.isobmff import Box, Reader

_EPOCH_1904 = datetime(1904, 1, 1, tzinfo=timezone.utc)
UDTA_ADDABLE = {"©xyz": "Location", "©mak": "Make", "©mod": "Model", "©swr": "Software",
                "©cmt": "Comment"}
MDTA_ADDABLE = {"com.apple.quicktime.location.ISO6709": "Location", "com.apple.quicktime.make": "Make",
                "com.apple.quicktime.model": "Model", "com.apple.quicktime.software": "Software",
                "com.apple.quicktime.creationdate": "Creation date"}
DATE_KEYS = ("qt:CreateDate", "qt:ModifyDate")


class LayoutError(ValueError):
    """The file cannot take this change without moving its media data."""


def seconds_from_iso(value: str) -> int:
    """Seconds since 1904 (UTC) of a canonical date-time with an offset."""
    stamp = datetime.fromisoformat(value)
    seconds = int((stamp - _EPOCH_1904).total_seconds())
    if not 0 < seconds < 1 << 32:
        raise LayoutError("QuickTime dates range from 1904 to 2040 in this file")
    return seconds


def _splice(blob: bytearray, start: int, end: int, new: bytes, container: Box) -> bytearray:
    """Replace ``blob[start:end]`` (inside ``container``) and add the size change to ``container`` and to every
    atom that contains it."""
    delta = len(new) - (end - start)
    chain = []
    rd = Reader(bytes(blob))
    level = rd.children()
    while True:
        hit = next((b for b in level if b.start <= container.start and container.end <= b.end), None)
        if hit is None:
            break
        if hit.data_start - hit.start != 8:
            raise LayoutError("an atom with a 64-bit size cannot be resized")
        chain.append(hit)
        if hit.start == container.start and hit.end == container.end:
            break
        level = rd.children(hit.data_start + (4 if hit.type == b"meta" and _is_full_meta(rd, hit) else 0), hit.end)
    if not chain or (chain[-1].start, chain[-1].end) != (container.start, container.end):
        raise LayoutError("the atom to change was not found")
    out = bytearray(blob[:start]) + new + blob[end:]
    for box in chain:
        out[box.start:box.start + 4] = struct.pack(">I", box.size + delta)
    return out


def _is_full_meta(rd: Reader, meta: Box) -> bool:
    return bytes(rd.buf[meta.data_start:meta.data_start + 4]) == bytes(4)


def _moov(path: Path) -> tuple[Box, bool, int]:
    from core.isobmff import open_reader
    with open_reader(path) as rd:
        boxes = rd.children()
        moov = next((b for b in boxes if b.type == b"moov"), None)
        if moov is None:
            raise LayoutError("the file has no movie atom")
        if moov.data_start - moov.start != 8:
            raise LayoutError("the movie atom has a 64-bit size")
        if any(b.type in (b"moof", b"sidx") for b in boxes):
            raise LayoutError("fragmented files are not supported")
        after = boxes[boxes.index(moov) + 1:]
        # nothing after ``moov`` is addressed by an offset when it is empty or only holds atoms found by scanning
        return moov, all(b.type in (b"uuid", b"free", b"skip") for b in after), len(rd.buf)


def _write_back(path: Path, moov: Box, last: bool, new: bytes) -> None:
    old = moov.size
    if len(new) == old:
        pass
    elif last:
        with open(path, "rb") as stream:                  # what follows is only found by scanning: it can move
            stream.seek(moov.end)
            tail = stream.read()
        new = new + tail
        with open(path, "r+b") as stream:
            stream.seek(moov.start)
            stream.write(new)
            stream.truncate(moov.start + len(new))
        return
    elif len(new) < old and old - len(new) >= 8:
        new = new + struct.pack(">I", old - len(new)) + b"free" + bytes(old - len(new) - 8)
    elif len(new) > old:
        _grow_before_media(path, moov, new)
        return
    else:
        raise LayoutError("a value 1 to 7 bytes shorter cannot be absorbed: change it by more or by nothing")
    with open(path, "r+b") as stream:
        stream.seek(moov.start)
        stream.write(new)


def _read_moov(path: Path, moov: Box) -> bytearray:
    with open(path, "rb") as stream:
        stream.seek(moov.start)
        return bytearray(stream.read(moov.size))


def _text_atom(name: str, text: str) -> bytes:
    raw = text.encode("utf-8")
    if len(raw) > 0xFFFF:
        raise LayoutError("the text is too long")
    return struct.pack(">I", 12 + len(raw)) + name.encode("latin-1") + struct.pack(">HH", len(raw), 0x15C7) + raw


def set_udta_text(path: Path, name: str, text: str) -> None:
    moov, last, _ = _moov(path)
    blob = _read_moov(path, moov)
    rd = Reader(bytes(blob))
    root = rd.children()[0]
    atom = _text_atom(name, text)
    udta = rd.find(root, b"udta")
    if udta is None:
        new = _splice(blob, root.end, root.end, struct.pack(">I", 8 + len(atom)) + b"udta" + atom, root)
    else:
        old = next((b for b in rd.children(udta.data_start, udta.end) if b.type.decode("latin-1") == name), None)
        if old is not None:
            new = _splice(blob, old.start, old.end, atom, udta)
        else:
            new = _splice(blob, udta.end, udta.end, atom, udta)
    _write_back(path, moov, last, bytes(new))


def _mdta_item(index: int, text: str) -> bytes:
    raw = text.encode("utf-8")
    data = struct.pack(">I", 16 + len(raw)) + b"data" + struct.pack(">II", 1, 0) + raw
    return struct.pack(">I", 8 + len(data)) + struct.pack(">I", index) + data


def set_mdta_text(path: Path, name: str, text: str) -> None:
    moov, last, _ = _moov(path)
    blob = _read_moov(path, moov)
    rd = Reader(bytes(blob))
    root = rd.children()[0]
    meta = rd.find(root, b"meta")
    if meta is None:
        # an ISO full-box ``meta`` with an ``mdta`` handler, the key and its item
        handler = struct.pack(">I", 33) + b"hdlr" + bytes(4) + bytes(4) + b"mdta" + bytes(12) + b"\x00"
        keys = struct.pack(">I", 24 + len(name)) + b"keys" + bytes(4) + struct.pack(">I", 1) \
            + struct.pack(">I", 8 + len(name)) + b"mdta" + name.encode("utf-8")
        ilst = struct.pack(">I", 8 + len(_mdta_item(1, text))) + b"ilst" + _mdta_item(1, text)
        body = bytes(4) + handler + keys + ilst
        new = _splice(blob, root.end, root.end, struct.pack(">I", 8 + len(body)) + b"meta" + body, root)
        _write_back(path, moov, last, bytes(new))
        return
    start = meta.data_start + (4 if _is_full_meta(rd, meta) else 0)
    keys = ilst = None
    for child in rd.children(start, meta.end):
        if child.type == b"keys":
            keys = child
        elif child.type == b"ilst":
            ilst = child
    if keys is None or ilst is None:
        raise LayoutError("the metadata atom has no key table")
    count = struct.unpack_from(">I", blob, keys.data_start + 4)[0]
    names, pos = [], keys.data_start + 8
    for _ in range(count):
        size = struct.unpack_from(">I", blob, pos)[0]
        names.append(bytes(blob[pos + 8:pos + size]).decode("utf-8", "replace"))
        pos += size
    if name in names:
        index = names.index(name) + 1
        item = next((c for c in rd.children(ilst.data_start, ilst.end)
                     if struct.unpack(">I", c.type)[0] == index), None)
        new = _splice(blob, item.start, item.end, _mdta_item(index, text), ilst) if item is not None \
            else _splice(blob, ilst.end, ilst.end, _mdta_item(index, text), ilst)
    else:
        entry = struct.pack(">I", 8 + len(name.encode("utf-8"))) + b"mdta" + name.encode("utf-8")
        blob = _splice(blob, ilst.end, ilst.end, _mdta_item(count + 1, text), ilst)     # later offsets first
        blob[keys.data_start + 4:keys.data_start + 8] = struct.pack(">I", count + 1)
        new = _splice(blob, keys.end, keys.end, entry, keys)
    _write_back(path, moov, last, bytes(new))


def set_date(path: Path, key: str, seconds: int) -> None:
    """Write the creation / modification time of the movie header or of a track (tkhd and mdhd)."""
    from core.isobmff import open_reader
    with open_reader(path) as rd:
        moov = rd.find(None, b"moov")
        if moov is None:
            raise LayoutError("the file has no movie atom")
        targets: list[tuple[Box, int]] = []
        if key in DATE_KEYS:
            mvhd = rd.find(moov, b"mvhd")
            if mvhd is not None:
                targets.append((mvhd, 0 if key == "qt:CreateDate" else 1))
        else:
            track = int(key.split(":")[1][len("track"):])
            for trak in rd.find_all(moov, b"trak"):
                tkhd = rd.find(trak, b"tkhd")
                width = 8 if tkhd is not None and rd.buf[tkhd.data_start] == 1 else 4
                if tkhd is not None and rd.u("I", tkhd.data_start + 4 + 2 * width) == (track,):
                    targets += [(tkhd, 0), (tkhd, 1)]
                    mdhd = rd.find(trak, b"mdia", b"mdhd")
                    if mdhd is not None:
                        targets += [(mdhd, 0), (mdhd, 1)]
        patches = []
        for box, which in targets:
            width = 8 if rd.buf[box.data_start] == 1 else 4
            if seconds >= 1 << (8 * width):
                raise LayoutError("the date does not fit in this file")
            patches.append((box.data_start + 4 + which * width, seconds.to_bytes(width, "big")))
    if not patches:
        raise LayoutError("the header to change was not found")
    with open(path, "r+b") as stream:
        for offset, raw in patches:
            stream.seek(offset)
            stream.write(raw)


_ISO6709 = re.compile(r"[+-][0-9]{2}(\.[0-9]+)?[+-][0-9]{3}(\.[0-9]+)?([+-][0-9]+(\.[0-9]+)?)?/")


@dataclass(frozen=True)
class QtRaw:
    """Pre-image of one ``qt:`` key. Equality is the value only: the patches (offset, original, replacement)
    are a hint to put a removed atom back exactly where it was."""
    value: object
    patches: tuple = field(default=(), compare=False)


def text_of(key: str, value: str) -> str:
    """The checked text of a ``qt:udta:`` / ``qt:mdta:`` value."""
    if not isinstance(value, str) or "\x00" in value:
        raise LayoutError("the value must be text without NUL characters")
    name = key.split(":", 2)[2]
    if name in ("\u00a9xyz", "com.apple.quicktime.location.ISO6709") and not _ISO6709.fullmatch(value):
        raise LayoutError("a location looks like +45.4642+009.1900+120.500/")
    return value


def set_value(path: Path, key: str, value) -> None:
    """Write the value of a ``qt:`` key: a date, or the text of a ``udta`` / ``mdta`` entry."""
    if key in DATE_KEYS or (key.startswith("qt:track") and key.endswith(":CreateDate")):
        set_date(path, key, seconds_from_iso(value))
    elif key.startswith("qt:udta:"):
        set_udta_text(path, key.split(":", 2)[2], text_of(key, value))
    elif key.startswith("qt:mdta:"):
        set_mdta_text(path, key.split(":", 2)[2], text_of(key, value))
    else:
        raise LayoutError("this field cannot be written")


_CHUNK = 8 * 1024 * 1024


def _fix_offsets(blob: bytes, old_end: int, delta: int) -> bytes:
    """The chunk offset tables (``stco`` / ``co64``) of a movie atom, with every offset past ``old_end`` moved."""
    out = bytearray(blob)
    rd = Reader(bytes(blob))
    root = rd.children()[0]
    for trak in rd.find_all(root, b"trak"):
        stbl = rd.find(trak, b"mdia", b"minf", b"stbl")
        if stbl is None:
            continue
        for kind, width, code in ((b"stco", 4, ">I"), (b"co64", 8, ">Q")):
            for table in rd.find_all(stbl, kind):
                count = struct.unpack_from(">I", out, table.data_start + 4)[0]
                for i in range(count):
                    at = table.data_start + 8 + width * i
                    value = struct.unpack_from(code, out, at)[0]
                    if value >= old_end:
                        if kind == b"stco" and value + delta >= 1 << 32:
                            raise LayoutError("a 32-bit chunk offset would overflow: the file needs 64-bit offsets")
                        struct.pack_into(code, out, at, value + delta)
    return bytes(out)


def _grow_before_media(path: Path, moov: Box, new: bytes) -> None:
    """The movie atom grows while media follows it: the rest of the file moves up and the chunk offsets follow."""
    delta = len(new) - moov.size
    fixed = _fix_offsets(new, moov.end, delta)
    with open(path, "r+b") as stream:
        stream.seek(0, 2)
        size = stream.tell()
        pos = size
        while pos > moov.end:                                   # from the end, so nothing is overwritten
            start = max(moov.end, pos - _CHUNK)
            stream.seek(start)
            chunk = stream.read(pos - start)
            stream.seek(start + delta)
            stream.write(chunk)
            pos = start
        stream.seek(moov.start)
        stream.write(fixed)
