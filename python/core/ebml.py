"""Bounded reader of Matroska / WebM (EBML) files: header values, tags, attachments, tracks.

Elements are read where they are; a removed element is turned into a ``Void`` element of the same total size
(its payload zeroed), a text is rewritten over the old one when it fits and the rest is voided: nothing moves,
the clusters (the audio and video) are never touched. Files whose segment or clusters have an unknown size
(live recordings) are read-only.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

EBML, SEGMENT, SEEKHEAD, INFO, TRACKS, CLUSTER, CUES, TAGS, ATTACHMENTS, CHAPTERS, VOID = (
    0x1A45DFA3, 0x18538067, 0x114D9B74, 0x1549A966, 0x1654AE6B, 0x1F43B675, 0x1C53BB6B, 0x1254C367, 0x1941A469,
    0x1043A770, 0xEC)
TITLE, MUXING_APP, WRITING_APP, DATE_UTC, DURATION, TIMECODE_SCALE = 0x7BA9, 0x4D80, 0x5741, 0x4461, 0x4489, 0x2AD7B1
TAG, SIMPLE_TAG, TAG_NAME, TAG_STRING, TAG_LANGUAGE = 0x7373, 0x67C8, 0x45A3, 0x4487, 0x447A
ATTACHED_FILE, FILE_NAME, FILE_MIME, FILE_DATA = 0x61A7, 0x466E, 0x4660, 0x465C
TRACK_ENTRY, TRACK_NUMBER, TRACK_TYPE, CODEC_ID, LANGUAGE, NAME = 0xAE, 0xD7, 0x83, 0x86, 0x22B59C, 0x536E
MAX_ELEMENTS = 200_000
TEXT_INFO = {TITLE: "Title", MUXING_APP: "MuxingApp", WRITING_APP: "WritingApp"}


@dataclass(frozen=True)
class Element:
    id: int
    start: int
    data_start: int
    end: int
    unknown_size: bool = False

    @property
    def size(self) -> int:
        return self.end - self.start


class EbmlError(ValueError):
    pass


def _vint(buf, pos: int, keep_marker: bool) -> tuple[int | None, int]:
    """(value, length) of the variable-length integer at ``pos``; ``None`` for the "unknown size" value."""
    if pos >= len(buf):
        raise EbmlError("truncated")
    first = buf[pos]
    length = 1
    while length <= 8 and not first & (0x80 >> (length - 1)):
        length += 1
    if length > 8 or pos + length > len(buf):
        raise EbmlError("bad variable-length integer")
    value = first if keep_marker else first & (0xFF >> length)
    for b in buf[pos + 1:pos + length]:
        value = (value << 8) | b
    if not keep_marker and value == (1 << (7 * length)) - 1:
        return None, length
    return value, length


def children(buf, start: int, end: int) -> list[Element]:
    """The elements laid out in ``buf[start:end]`` (an unreadable tail is dropped)."""
    out: list[Element] = []
    pos = start
    while pos < end and len(out) < MAX_ELEMENTS:
        try:
            ident, id_len = _vint(buf, pos, True)
            size, size_len = _vint(buf, pos + id_len, False)
        except EbmlError:
            break
        data_start = pos + id_len + size_len
        unknown = size is None
        stop = end if unknown else data_start + size
        if stop > end:
            break
        out.append(Element(ident, pos, data_start, stop, unknown))
        pos = stop
    return out


def _find(buf, parent_range: tuple[int, int], ident: int) -> list[Element]:
    return [e for e in children(buf, *parent_range) if e.id == ident]


def _uint(buf, e: Element) -> int:
    return int.from_bytes(buf[e.data_start:e.end], "big")


def _text(buf, e: Element) -> str:
    return bytes(buf[e.data_start:e.end]).rstrip(b"\x00").decode("utf-8", "replace")


def _float(buf, e: Element) -> float | None:
    import struct
    raw = bytes(buf[e.data_start:e.end])
    if len(raw) == 4:
        return struct.unpack(">f", raw)[0]
    if len(raw) == 8:
        return struct.unpack(">d", raw)[0]
    return None


class Segment:
    """The top-level structure of a file: the Segment and the elements the metadata lives in."""

    def __init__(self, buf) -> None:
        self.buf = buf
        top = children(buf, 0, len(buf))
        if not top or top[0].id != EBML:
            raise EbmlError("not a Matroska file")
        seg = next((e for e in top if e.id == SEGMENT), None)
        if seg is None:
            raise EbmlError("no Segment")
        self.segment = seg
        self.items = children(buf, seg.data_start, seg.end)
        self.live = seg.unknown_size or any(e.unknown_size for e in self.items)
        header = children(buf, top[0].data_start, top[0].end)
        doc = next((e for e in header if e.id == 0x4282), None)
        self.doc_type = _text(buf, doc) if doc else "matroska"

    def first(self, ident: int) -> Element | None:
        return next((e for e in self.items if e.id == ident), None)

    def all(self, ident: int) -> list[Element]:
        return [e for e in self.items if e.id == ident]


def void_patch(buf, element: Element):
    """``(offset, original, replacement)`` turning ``element`` into a Void element of the same total size."""
    total = element.size
    for size_len in range(1, 9):
        payload = total - 1 - size_len
        if payload >= 0 and payload < (1 << (7 * size_len)) - 1:
            marker = 1 << (7 * size_len)
            header = bytes([VOID]) + (marker | payload).to_bytes(size_len, "big")
            return element.start, bytes(buf[element.start:element.end]), header + bytes(payload)
    raise EbmlError("the element is too small to be voided")


def info_values(buf, seg: Segment) -> dict[int, Element]:
    info = seg.first(INFO)
    return {e.id: e for e in children(buf, info.data_start, info.end)} if info else {}


def simple_tags(buf, seg: Segment) -> list[tuple[str, str, Element]]:
    """(name, string, element) of every SimpleTag inside the Tags elements."""
    out = []
    for tags in seg.all(TAGS):
        for tag in _find(buf, (tags.data_start, tags.end), TAG):
            for simple in _find(buf, (tag.data_start, tag.end), SIMPLE_TAG):
                kids = {e.id: e for e in children(buf, simple.data_start, simple.end)}
                if TAG_NAME in kids and TAG_STRING in kids:
                    out.append((_text(buf, kids[TAG_NAME]), _text(buf, kids[TAG_STRING]), simple))
    return out


def attachments(buf, seg: Segment) -> list[tuple[str, str, bytes, Element]]:
    out = []
    for block in seg.all(ATTACHMENTS):
        for attached in _find(buf, (block.data_start, block.end), ATTACHED_FILE):
            kids = {e.id: e for e in children(buf, attached.data_start, attached.end)}
            name = _text(buf, kids[FILE_NAME]) if FILE_NAME in kids else ""
            mime = _text(buf, kids[FILE_MIME]) if FILE_MIME in kids else ""
            data = bytes(buf[kids[FILE_DATA].data_start:kids[FILE_DATA].end]) if FILE_DATA in kids else b""
            out.append((name, mime, data, attached))
    return out


def tracks(buf, seg: Segment) -> list[dict]:
    out = []
    block = seg.first(TRACKS)
    for entry in (_find(buf, (block.data_start, block.end), TRACK_ENTRY) if block else []):
        kids = {e.id: e for e in children(buf, entry.data_start, entry.end)}
        out.append({"number": _uint(buf, kids[TRACK_NUMBER]) if TRACK_NUMBER in kids else len(out) + 1,
                    "type": _uint(buf, kids[TRACK_TYPE]) if TRACK_TYPE in kids else 0,
                    "codec": _text(buf, kids[CODEC_ID]) if CODEC_ID in kids else "",
                    "language": _text(buf, kids[LANGUAGE]) if LANGUAGE in kids else "",
                    "name": _text(buf, kids[NAME]) if NAME in kids else ""})
    return out


def media_digest(path: Path) -> str | None:
    """SHA-256 of the clusters (the audio and video data)."""
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        try:
            import mmap
            buf = mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ)
        except (ValueError, OSError):
            return None
        try:
            for e in Segment(buf).all(CLUSTER):
                digest.update(bytes(buf[e.start:e.end]))
            return digest.hexdigest()
        except EbmlError:
            return None
        finally:
            buf.close()
