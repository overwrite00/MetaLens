"""Chunk containers of PNG and WebP files: parse into a list of chunks, change the metadata chunks, serialize.

Only the chunks that hold metadata are ever created, replaced or removed; every other chunk (and the bytes
after the last chunk) is written back exactly as read, so the picture data is untouched.
PNG: ``eXIf`` holds the EXIF TIFF block, an ``iTXt`` chunk with the keyword ``XML:com.adobe.xmp`` holds the XMP
packet, ``tEXt`` / ``zTXt`` / ``iTXt`` hold text. WebP (RIFF): ``EXIF`` and ``XMP `` chunks, announced by the flags
of a ``VP8X`` header chunk (created when the file has only a bare ``VP8 `` / ``VP8L`` image).
"""
from __future__ import annotations

import struct
import zlib

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
XMP_KEYWORD = b"XML:com.adobe.xmp"
TEXT_TYPES = (b"tEXt", b"zTXt", b"iTXt")
MAX_CHUNKS = 100_000


class ContainerError(ValueError):
    """The file is not a well-formed PNG / WebP container (its metadata is left untouched)."""


# ──────────────────────────────── PNG ────────────────────────────────────────

class PngFile:
    def __init__(self, chunks: list[list], tail: bytes) -> None:
        self.chunks = chunks           # [type, body]
        self.tail = tail               # bytes after IEND, kept as they are

    @classmethod
    def parse(cls, data: bytes) -> "PngFile":
        if data[:8] != PNG_SIGNATURE:
            raise ContainerError("not a PNG file")
        chunks: list[list] = []
        pos = 8
        while pos + 12 <= len(data) and len(chunks) < MAX_CHUNKS:
            length = int.from_bytes(data[pos:pos + 4], "big")
            kind = data[pos + 4:pos + 8]
            end = pos + 8 + length
            if end + 4 > len(data):
                raise ContainerError(f"{kind.decode('latin-1')} chunk is truncated")
            body = data[pos + 8:end]
            if zlib.crc32(kind + body) != int.from_bytes(data[end:end + 4], "big"):
                raise ContainerError(f"{kind.decode('latin-1')} chunk has a wrong checksum")
            chunks.append([kind, body])
            pos = end + 4
            if kind == b"IEND":
                return cls(chunks, data[pos:])
        raise ContainerError("no IEND chunk")

    def to_bytes(self) -> bytes:
        out = [PNG_SIGNATURE]
        for kind, body in self.chunks:
            out.append(struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body)))
        out.append(self.tail)
        return b"".join(out)

    # ── metadata chunks ──
    def index_of(self, kind: bytes) -> list[int]:
        return [i for i, (k, _) in enumerate(self.chunks) if k == kind]

    def xmp_indexes(self) -> list[int]:
        return [i for i, (k, body) in enumerate(self.chunks) if k == b"iTXt" and body.startswith(XMP_KEYWORD + b"\x00")]

    def xmp_packet(self, index: int) -> bytes | None:
        """Text of the XMP ``iTXt`` chunk at ``index`` (decompressed), ``None`` if it cannot be decoded."""
        body = self.chunks[index][1]
        rest = body[len(XMP_KEYWORD) + 1:]
        if len(rest) < 2:
            return None
        compressed = rest[0]
        _lang, _, rest = rest[2:].partition(b"\x00")
        _translated, _, text = rest.partition(b"\x00")
        if compressed:
            try:
                return zlib.decompress(text)
            except zlib.error:
                return None
        return text

    def _insert_position(self, hint: int | None) -> int:
        if hint is not None and 1 <= hint <= len(self.chunks):
            return min(hint, self._first_data())
        return self._first_data()

    def _first_data(self) -> int:
        for i, (kind, _) in enumerate(self.chunks):
            if kind in (b"IDAT", b"IEND"):
                return i
        return len(self.chunks)

    def set_exif(self, tiff: bytes | None, hint: int | None = None) -> None:
        self._replace(self.index_of(b"eXIf"), [b"eXIf", tiff] if tiff is not None else None, hint)

    def set_xmp(self, packet: bytes | None, hint: int | None = None) -> None:
        chunk = None
        if packet is not None:
            chunk = [b"iTXt", XMP_KEYWORD + b"\x00\x00\x00\x00\x00" + packet]
        self._replace(self.xmp_indexes(), chunk, hint)

    def _replace(self, indexes: list[int], chunk: list | None, hint: int | None) -> None:
        position = indexes[0] if indexes else self._insert_position(hint)
        for i in reversed(indexes):
            del self.chunks[i]
        if chunk is not None:
            self.chunks.insert(min(position, len(self.chunks)), chunk)

    # ── text chunks ──
    @staticmethod
    def text_keyword(chunk: list) -> tuple[str, str] | None:
        """(keyword, language) of a text chunk; ``None`` for other chunks and for the XMP carrier."""
        kind, body = chunk
        if kind not in TEXT_TYPES or b"\x00" not in body:
            return None
        keyword, _, rest = body.partition(b"\x00")
        if keyword == XMP_KEYWORD:
            return None
        language = ""
        if kind == b"iTXt" and len(rest) >= 2:
            language = rest[2:].partition(b"\x00")[0].decode("ascii", "replace").strip()
        return keyword.decode("latin-1"), language

    def text_indexes(self, keyword: str, language: str = "") -> list[int]:
        return [i for i, chunk in enumerate(self.chunks) if self.text_keyword(chunk) == (keyword, language)]

    def remove_text(self, keyword: str, language: str = "") -> list[tuple[int, list]]:
        """Remove every text chunk of that keyword; the removed ``(index, chunk)`` pairs, for Undo."""
        found = [(i, self.chunks[i]) for i in self.text_indexes(keyword, language)]
        for i, _ in reversed(found):
            del self.chunks[i]
        return found

    def put_text(self, items: list[tuple[int, list]]) -> None:
        """Put back chunks removed by ``remove_text`` at their old positions."""
        for index, chunk in sorted(items, key=lambda item: item[0]):
            self.chunks.insert(min(index, len(self.chunks)), [chunk[0], chunk[1]])


# ──────────────────────────────── WebP ───────────────────────────────────────

FLAG_ICC, FLAG_ALPHA, FLAG_EXIF, FLAG_XMP, FLAG_ANIM = 0x20, 0x10, 0x08, 0x04, 0x02


class WebpFile:
    def __init__(self, chunks: list[list]) -> None:
        self.chunks = chunks           # [fourcc, body]

    @classmethod
    def parse(cls, data: bytes) -> "WebpFile":
        if data[:4] != b"RIFF" or data[8:12] != b"WEBP":
            raise ContainerError("not a WebP file")
        size = int.from_bytes(data[4:8], "little")
        if size + 8 > len(data) or size < 4:
            raise ContainerError("the RIFF size does not fit the file")
        end = size + 8
        chunks: list[list] = []
        pos = 12
        while pos + 8 <= end and len(chunks) < MAX_CHUNKS:
            fourcc = data[pos:pos + 4]
            length = int.from_bytes(data[pos + 4:pos + 8], "little")
            if pos + 8 + length > end:
                raise ContainerError(f"{fourcc.decode('latin-1')} chunk is truncated")
            chunks.append([fourcc, data[pos + 8:pos + 8 + length]])
            pos += 8 + length + (length & 1)
        if not chunks:
            raise ContainerError("no chunks")
        return cls(chunks)

    def to_bytes(self) -> bytes:
        body = b"WEBP"
        for fourcc, payload in self.chunks:
            body += fourcc + struct.pack("<I", len(payload)) + payload + (b"\x00" if len(payload) & 1 else b"")
        return b"RIFF" + struct.pack("<I", len(body)) + body

    def find(self, fourcc: bytes) -> list[int]:
        return [i for i, (k, _) in enumerate(self.chunks) if k == fourcc]

    # ── canvas / VP8X ──
    def canvas(self) -> tuple[int, int, bool] | None:
        """(width, height, has alpha) of the picture from a VP8X header or the bare bitstream."""
        for fourcc, body in self.chunks:
            if fourcc == b"VP8X" and len(body) >= 10:
                return (int.from_bytes(body[4:7], "little") + 1, int.from_bytes(body[7:10], "little") + 1,
                        bool(body[0] & FLAG_ALPHA))
            if fourcc == b"VP8 " and len(body) >= 10 and body[3:6] == b"\x9d\x01\x2a":
                return struct.unpack_from("<H", body, 6)[0] & 0x3FFF, struct.unpack_from("<H", body, 8)[0] & 0x3FFF, False
            if fourcc == b"VP8L" and len(body) >= 5 and body[0] == 0x2F:
                bits = int.from_bytes(body[1:5], "little")
                return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1, bool((bits >> 28) & 1)
        return None

    def _ensure_vp8x(self) -> None:
        if self.find(b"VP8X"):
            return
        canvas = self.canvas()
        if canvas is None:
            raise ContainerError("the picture size cannot be determined")
        width, height, alpha = canvas
        flags = FLAG_ALPHA if (alpha or self.find(b"ALPH")) else 0
        header = bytes([flags]) + bytes(3) + (width - 1).to_bytes(3, "little") + (height - 1).to_bytes(3, "little")
        self.chunks.insert(0, [b"VP8X", header])

    def _set_flag(self, flag: int, on: bool) -> None:
        for chunk in self.chunks:
            if chunk[0] == b"VP8X" and len(chunk[1]) >= 10:
                value = chunk[1][0] | flag if on else chunk[1][0] & ~flag & 0xFF
                chunk[1] = bytes([value]) + chunk[1][1:]

    def set_chunk(self, fourcc: bytes, payload: bytes | None, flag: int) -> None:
        indexes = self.find(fourcc)
        if payload is not None:
            self._ensure_vp8x()
            indexes = self.find(fourcc)
        position = indexes[0] if indexes else len(self.chunks)
        for i in reversed(indexes):
            del self.chunks[i]
        if payload is not None:
            self.chunks.insert(min(position, len(self.chunks)), [fourcc, payload])
        self._set_flag(flag, payload is not None)

    def set_exif(self, tiff: bytes | None) -> None:
        self.set_chunk(b"EXIF", tiff, FLAG_EXIF)

    def set_xmp(self, packet: bytes | None) -> None:
        self.set_chunk(b"XMP ", packet, FLAG_XMP)
