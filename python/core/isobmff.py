"""Bounded reader for ISO base media boxes (MP4, MOV, M4A, and later HEIC/AVIF).

Only reads: ``Reader`` walks the box tree of a byte buffer (an ``mmap`` of the
file or plain ``bytes``) without copying it. Every walk is bounded (depth,
boxes per level, total boxes) so a hostile or damaged file cannot make the
reader loop or allocate without limit; a box that does not fit its parent is
ignored (never raised), because metadata reading must not fail on the media.
"""
from __future__ import annotations

import mmap
import struct
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

MAX_BOXES_PER_LEVEL = 100_000
MAX_TOTAL_BOXES = 500_000
MAX_DEPTH = 12


@dataclass(frozen=True)
class Box:
    type: bytes
    start: int          # offset of the box header
    data_start: int     # first byte after the header (and after the usertype of a ``uuid`` box)
    end: int            # offset just after the box
    usertype: bytes | None = None

    @property
    def size(self) -> int:
        return self.end - self.start


class Reader:
    """Navigation over the boxes of ``buf``."""

    def __init__(self, buf) -> None:
        self.buf = buf
        self._budget = MAX_TOTAL_BOXES

    def children(self, start: int = 0, end: int | None = None) -> list[Box]:
        """The boxes laid out one after another in ``buf[start:end]`` (malformed tail dropped)."""
        end = len(self.buf) if end is None else min(end, len(self.buf))
        boxes: list[Box] = []
        pos = start
        while pos + 8 <= end and len(boxes) < MAX_BOXES_PER_LEVEL and self._budget > 0:
            size, kind = struct.unpack_from(">I4s", self.buf, pos)
            header = 8
            if size == 1:
                if pos + 16 > end:
                    break
                size = struct.unpack_from(">Q", self.buf, pos + 8)[0]
                header = 16
            elif size == 0:
                size = end - pos
            usertype = None
            if kind == b"uuid":
                if pos + header + 16 > end:
                    break
                usertype = bytes(self.buf[pos + header:pos + header + 16])
                header += 16
            if size < header or pos + size > end:
                break
            boxes.append(Box(kind, pos, pos + header, pos + size, usertype))
            self._budget -= 1
            pos += size
        return boxes

    def find(self, parent: Box | None, *path: bytes) -> Box | None:
        """First box reached by following ``path`` (types) from ``parent`` (``None`` = file root)."""
        boxes = self.children() if parent is None else self.children(parent.data_start, parent.end)
        for kind in path:
            hit = next((b for b in boxes if b.type == kind), None)
            if hit is None:
                return None
            parent = hit
            boxes = self.children(hit.data_start, hit.end)
        return parent

    def find_all(self, parent: Box | None, kind: bytes) -> list[Box]:
        boxes = self.children() if parent is None else self.children(parent.data_start, parent.end)
        return [b for b in boxes if b.type == kind]

    def payload(self, box: Box) -> bytes:
        return bytes(self.buf[box.data_start:box.end])

    def u(self, fmt: str, pos: int):
        """Unpack ``fmt`` (big-endian) at ``pos``; ``None`` when it does not fit the buffer."""
        try:
            return struct.unpack_from(">" + fmt, self.buf, pos)
        except struct.error:
            return None


@contextmanager
def open_reader(path: Path) -> Iterator[Reader]:
    """A ``Reader`` over a read-only mapping of ``path`` (empty files give an empty reader)."""
    with open(path, "rb") as stream:
        try:
            mapped = mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ)
        except ValueError:      # empty file
            yield Reader(b"")
            return
        try:
            yield Reader(mapped)
        finally:
            mapped.close()
