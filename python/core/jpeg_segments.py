"""Byte-exact JPEG marker-segment reader/writer (header part only).

A JPEG file is parsed up to the first SOS (start of scan) marker. Everything from
SOS to the end of the file (entropy-coded data, later scans, EOI, trailers such
as MPF secondary images) is kept as one opaque ``tail`` and written back
unchanged. Every segment keeps its original bytes (``raw``, including any 0xFF
fill bytes in front of the marker), so a rebuild that replaces one segment
leaves every other byte of the file identical.

Handled: fill bytes (repeated 0xFF before a marker), markers without a length
field (SOI, EOI, RSTn, TEM), APPn segments, files without SOS (EOI first).

Extraneous bytes between segments (bytes that are not 0xFF, ``FF 00`` pairs)
are not conformant: the strict parser rejects them. ``parse(..., tolerant=True)``
skips them the way decoders do (libjpeg: "extraneous bytes before marker"),
keeps them in the ``raw`` of the following segment (or in the tail) so the file
still round-trips byte for byte, and lists them in ``JpegFile.extraneous``.
Callers use the tolerant mode only to READ such files.

Multi-Picture Format (CIPA DC-007, e.g. MPO stereo or preview images): the APP2
``MPF`` segment of the first image holds the offset of every further image
relative to its MP header and the size of the first image. ``to_bytes`` keeps
them right when header segments change size: new segments go in front of the
MPF segment (offsets unchanged), and any change of the distance between the MP
header and the image data, or of the first image's size, is applied to the MP
Entry fields. ``JpegFile.mpf_problem`` tells when an MPF index cannot be
updated with certainty (callers then treat the file as read-only).
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field

from core.errors import ErrorCode, MetadataValueError

SOI = 0xD8
EOI = 0xD9
SOS = 0xDA
APP0 = 0xE0
APP1 = 0xE1
APP2 = 0xE2
APP13 = 0xED
COM = 0xFE
TEM = 0x01

# Largest payload of one marker segment: 0xFFFF minus the 2 length bytes.
MAX_PAYLOAD = 0xFFFF - 2

EXIF_HEADER = b"Exif\x00\x00"
XMP_HEADER = b"http://ns.adobe.com/xap/1.0/\x00"
XMP_EXT_HEADER = b"http://ns.adobe.com/xmp/extension/\x00"
PHOTOSHOP_HEADER = b"Photoshop 3.0\x00"
MPF_HEADER = b"MPF\x00"

MP_NUMBER_OF_IMAGES = 0xB001
MP_ENTRY = 0xB002


class JpegFormatError(ValueError):
    """The file is not a JPEG whose header this module can parse safely."""


class MpfError(ValueError):
    """The Multi-Picture Format index cannot be parsed or updated with certainty."""


def _has_length(marker: int) -> bool:
    return not (marker in (SOI, EOI, TEM) or 0xD0 <= marker <= 0xD7)


@dataclass
class Segment:
    marker: int
    payload: bytes | None          # None for markers without a length field
    raw: bytes                     # exact original bytes (extraneous + fill bytes + marker + length + payload)
    offset: int | None = None      # position of ``raw`` in the parsed file; None for built segments

    @classmethod
    def build(cls, marker: int, payload: bytes, *, key: str | None = None) -> "Segment":
        if len(payload) > MAX_PAYLOAD:
            raise MetadataValueError(
                key, ErrorCode.TOO_LARGE,
                f"{key + ': ' if key else ''}segment payload of {len(payload)} bytes exceeds "
                f"the JPEG limit of {MAX_PAYLOAD} bytes")
        raw = bytes((0xFF, marker)) + (len(payload) + 2).to_bytes(2, "big") + payload
        return cls(marker, bytes(payload), raw)

    @property
    def header_size(self) -> int:
        """Bytes of ``raw`` before the payload (extraneous/fill bytes, marker, length)."""
        return len(self.raw) - len(self.payload or b"")

    def is_app(self, n: int, signature: bytes) -> bool:
        return self.marker == 0xE0 + n and self.payload is not None and self.payload.startswith(signature)

    @property
    def is_exif(self) -> bool:
        return self.is_app(1, EXIF_HEADER)

    @property
    def is_xmp(self) -> bool:
        return self.is_app(1, XMP_HEADER)

    @property
    def is_xmp_extension(self) -> bool:
        return self.is_app(1, XMP_EXT_HEADER)

    @property
    def is_photoshop(self) -> bool:
        return self.is_app(13, PHOTOSHOP_HEADER)

    @property
    def is_mpf(self) -> bool:
        return self.is_app(2, MPF_HEADER)

    @property
    def is_jfif(self) -> bool:
        return self.marker == APP0 and self.payload is not None and \
            self.payload[:5] in (b"JFIF\x00", b"JFXX\x00")


# ──────────────────────── Multi-Picture Format index ─────────────────────────

@dataclass
class MpIndex:
    """MP Index IFD of an APP2 ``MPF`` segment (CIPA DC-007).

    ``entries``: one ``[attribute, size, offset, dependent 1, dependent 2]`` per
    individual image. Offsets are relative to the MP header (the endian field
    right after ``MPF\\0``); the first image has offset 0 and its size runs from
    its SOI to its EOI.
    """
    byteorder: str
    entries_at: int                       # position of the MP Entry array in the payload
    entries: list[list[int]]

    def pack(self, payload: bytes) -> bytes:
        """``payload`` with the MP Entry array replaced by ``entries`` (same length)."""
        out = bytearray(payload)
        for i, entry in enumerate(self.entries):
            struct.pack_into(self.byteorder + "LLLHH", out, self.entries_at + 16 * i, *entry)
        return bytes(out)


def parse_mp_index(payload: bytes) -> MpIndex:
    """MP Index IFD of an ``MPF`` APP2 payload. Raises ``MpfError``."""
    if not payload.startswith(MPF_HEADER):
        raise MpfError("not an MPF segment")
    base = len(MPF_HEADER)
    head = payload[base:base + 4]
    if head == b"II*\x00":
        bo = "<"
    elif head == b"MM\x00*":
        bo = ">"
    else:
        raise MpfError("invalid MP endian field")
    if base + 8 > len(payload):
        raise MpfError("truncated MP header")
    ifd = struct.unpack_from(bo + "L", payload, base + 4)[0]
    if ifd < 8 or base + ifd + 2 > len(payload):
        raise MpfError("MP Index IFD outside the segment")
    count = struct.unpack_from(bo + "H", payload, base + ifd)[0]
    if base + ifd + 2 + 12 * count > len(payload):
        raise MpfError("MP Index IFD truncated")
    tags: dict[int, tuple[int, int, int]] = {}
    for i in range(count):
        tag, typ, n, value = struct.unpack_from(bo + "HHLL", payload, base + ifd + 2 + 12 * i)
        if tag in tags:
            raise MpfError(f"MP Index tag 0x{tag:04X} appears twice")
        tags[tag] = (typ, n, value)
    if MP_ENTRY not in tags:
        raise MpfError("no MP Entry (0xB002)")
    typ, n, value = tags[MP_ENTRY]
    if typ != 7 or n == 0 or n % 16:
        raise MpfError("MP Entry (0xB002) has an invalid type or size")
    images = n // 16
    if MP_NUMBER_OF_IMAGES in tags and tags[MP_NUMBER_OF_IMAGES] != (4, 1, images):
        raise MpfError("NumberOfImages (0xB001) does not match the MP Entry")
    at = base + value
    if value < 8 or at + n > len(payload):
        raise MpfError("MP Entry outside the segment")
    entries = [list(struct.unpack_from(bo + "LLLHH", payload, at + 16 * i)) for i in range(images)]
    return MpIndex(bo, at, entries)


# ──────────────────────── file ───────────────────────────────────────────────

@dataclass
class JpegFile:
    """SOI + header segments + opaque tail (``SOS...EOF`` or ``EOI...EOF``)."""
    soi: bytes
    segments: list[Segment] = field(default_factory=list)
    tail: bytes = b""
    tail_offset: int | None = None                       # position of the tail in the parsed file
    extraneous: list[tuple[int, int, int]] = field(default_factory=list)   # (offset, bytes, next marker)

    def to_bytes(self) -> bytes:
        raws = [s.raw for s in self.segments]
        positions, pos = [], len(self.soi)
        for raw in raws:
            positions.append(pos)
            pos += len(raw)
        moved = self._relocated_mpf(positions, pos)
        if moved is not None:
            index, raw = moved
            raws[index] = raw
        return b"".join([self.soi, *raws, self.tail])

    # ── queries ──
    def find(self, predicate) -> list[int]:
        return [i for i, s in enumerate(self.segments) if predicate(s)]

    def exif_indexes(self) -> list[int]:
        return self.find(lambda s: s.is_exif)

    def xmp_indexes(self) -> list[int]:
        return self.find(lambda s: s.is_xmp)

    def photoshop_indexes(self) -> list[int]:
        return self.find(lambda s: s.is_photoshop)

    def mpf_indexes(self) -> list[int]:
        return self.find(lambda s: s.is_mpf)

    # ── Multi-Picture Format ──
    def _mp_header_position(self, index: int, segment_position: int) -> int:
        return segment_position + self.segments[index].header_size + len(MPF_HEADER)

    def _mpf(self) -> tuple[int, MpIndex] | None:
        """(segment index, MP index) of the MPF segment of this (first) image, None when
        there is none. Raises ``MpfError`` when it cannot be updated with certainty."""
        found = self.mpf_indexes()
        if not found:
            return None
        if len(found) > 1:
            raise MpfError(f"{len(found)} MPF segments")
        index = found[0]
        seg = self.segments[index]
        if seg.offset is None or self.tail_offset is None:
            raise MpfError("position of the MPF segment unknown")
        mp = parse_mp_index(seg.payload)
        if mp.entries[0][2] != 0:
            raise MpfError("the first MP Entry does not describe this image (offset is not 0)")
        header = self._mp_header_position(index, seg.offset)
        for number, (_attr, size, offset, _d1, _d2) in enumerate(mp.entries[1:], 2):
            if offset == 0 and size == 0:
                continue                        # entry without image data
            start = header + offset
            if offset == 0 or start < self.tail_offset:
                raise MpfError(f"image {number} does not start after the header segments")
            at = start - self.tail_offset
            if self.tail[at:at + 2] != b"\xff\xd8":
                raise MpfError(f"the offset of image {number} does not point at a JPEG image")
        return index, mp

    def mpf_problem(self) -> str | None:
        """Why the MPF index cannot be kept consistent on a rewrite (None: it can, or no MPF)."""
        try:
            self._mpf()
        except MpfError as exc:
            return str(exc)
        return None

    def _relocated_mpf(self, positions: list[int], tail_position: int) -> tuple[int, bytes] | None:
        """New raw bytes of the MPF segment for the layout ``positions`` (None: unchanged)."""
        found = self.mpf_indexes()
        if not found or self.tail_offset is None or self.segments[found[0]].offset is None:
            return None                          # not a parsed layout: nothing to keep
        index = found[0]
        seg = self.segments[index]
        old_header = self._mp_header_position(index, seg.offset)
        new_header = self._mp_header_position(index, positions[index])
        shift = (tail_position - new_header) - (self.tail_offset - old_header)
        grow = tail_position - self.tail_offset
        if shift == 0 and grow == 0:
            return None
        try:
            located = self._mpf()
            if located is None:
                raise MpfError("MPF segment not found")
            _, mp = located
            for number, entry in enumerate(mp.entries):
                if number == 0:
                    if entry[1]:
                        entry[1] += grow        # size of this image, SOI to EOI
                elif entry[2]:
                    entry[2] += shift           # offset of a further image
                if not (0 <= entry[1] <= 0xFFFFFFFF and 0 <= entry[2] <= 0xFFFFFFFF):
                    raise MpfError("MP Entry value out of range after the rewrite")
        except MpfError as exc:
            raise MetadataValueError(
                None, ErrorCode.UNSUPPORTED,
                f"the Multi-Picture (MPF) index cannot be updated: {exc}") from exc
        for number, (_attr, _size, offset, _d1, _d2) in enumerate(mp.entries[1:], 2):
            at = new_header + offset - tail_position    # the image data is in the unchanged tail
            if offset and (at < 0 or self.tail[at:at + 2] != b"\xff\xd8"):
                raise MetadataValueError(None, ErrorCode.UNSUPPORTED,
                                         f"MPF image {number} would not be found after the rewrite")
        return index, seg.raw[:seg.header_size] + mp.pack(seg.payload)

    # ── edits ──
    def _leading_app_run(self) -> int:
        """Number of leading APPn/COM segments (where metadata segments may be inserted)."""
        count = 0
        for seg in self.segments:
            if not (0xE0 <= seg.marker <= 0xEF or seg.marker == COM):
                break
            count += 1
        return count

    def _default_insert_index(self, after) -> int:
        """Index after the last segment matching any of ``after`` among the leading
        APPn run; 0 (= right after SOI) when none matches. Never after the MPF
        segment: the offsets of the further images then stay unchanged."""
        index = 0
        for i, seg in enumerate(self.segments):
            if not (0xE0 <= seg.marker <= 0xEF):
                break
            if any(pred(seg) for pred in after):
                index = i + 1
        mpf = self.mpf_indexes()
        if mpf and index > mpf[0]:
            index = mpf[0]
        return index

    def replace_unique(self, predicate, payload: bytes | None, *, marker: int,
                       insert_after=(), key: str | None = None, index: int | None = None) -> int:
        """Replace in place the FIRST segment matching ``predicate`` and remove every
        other match; insert one when none exists (at ``index`` when it lies in the
        leading APPn run, else after ``insert_after``). ``payload=None`` removes them
        all. Returns the number of duplicate segments removed."""
        matches = self.find(predicate)
        new = None if payload is None else Segment.build(marker, payload, key=key)
        removed = max(len(matches) - 1, 0)
        if matches:
            first = matches[0]
            for i in reversed(matches[1:]):
                del self.segments[i]
            if new is None:
                del self.segments[first]
            else:
                self.segments[first] = new
        elif new is not None:
            if index is None or not 0 <= index <= self._leading_app_run():
                index = self._default_insert_index(insert_after)
            self.segments.insert(index, new)
        return removed

    def set_exif(self, tiff: bytes | None, *, key: str | None = None, index: int | None = None) -> int:
        payload = None if tiff is None else EXIF_HEADER + tiff
        return self.replace_unique(lambda s: s.is_exif, payload, marker=APP1,
                                   insert_after=(lambda s: s.is_jfif,), key=key, index=index)

    def set_xmp(self, packet: bytes | None, *, key: str | None = None, index: int | None = None) -> int:
        payload = None if packet is None else XMP_HEADER + packet
        return self.replace_unique(lambda s: s.is_xmp, payload, marker=APP1,
                                   insert_after=(lambda s: s.is_jfif, lambda s: s.is_exif),
                                   key=key, index=index)

    def set_photoshop(self, resources: bytes | None, *, key: str | None = None,
                      index: int | None = None) -> int:
        payload = None if resources is None else PHOTOSHOP_HEADER + resources
        return self.replace_unique(
            lambda s: s.is_photoshop, payload, marker=APP13,
            insert_after=(lambda s: 0xE0 <= s.marker <= 0xED,), key=key, index=index)


def parse(data: bytes, *, tolerant: bool = False) -> JpegFile:
    """Split ``data`` into SOI, header segments and tail. Raises ``JpegFormatError``.

    ``tolerant``: skip extraneous bytes between segments (kept byte for byte, listed
    in ``JpegFile.extraneous``) instead of rejecting the file. Use it to read only.
    """
    data = bytes(data)
    if data[:2] != b"\xff\xd8":
        raise JpegFormatError("not a JPEG file (missing SOI)")
    jpeg = JpegFile(soi=data[:2])
    pos, size = 2, len(data)
    while True:
        start = pos
        while True:
            if pos >= size:
                raise JpegFormatError("truncated JPEG: no SOS or EOI marker")
            if data[pos] != 0xFF:
                if not tolerant:
                    raise JpegFormatError(f"expected a marker at offset {pos}")
                pos += 1
                continue
            marker_at = pos
            while marker_at < size and data[marker_at] == 0xFF:   # 0xFF fill bytes
                marker_at += 1
            if marker_at >= size:
                raise JpegFormatError("truncated JPEG after fill bytes")
            if data[marker_at] != 0x00:
                break
            if not tolerant:
                raise JpegFormatError(f"stuffed byte outside entropy-coded data at offset {marker_at - 1}")
            pos = marker_at + 1                 # FF 00 outside a scan: skipped like decoders do
        marker = data[marker_at]
        if pos > start:
            jpeg.extraneous.append((start, pos - start, marker))
        pos = marker_at + 1
        if marker in (SOS, EOI):
            jpeg.tail = data[start:]
            jpeg.tail_offset = start
            return jpeg
        if marker == SOI:
            raise JpegFormatError(f"unexpected SOI at offset {pos - 2}")
        if not _has_length(marker):
            jpeg.segments.append(Segment(marker, None, data[start:pos], start))
            continue
        if pos + 2 > size:
            raise JpegFormatError("truncated segment length")
        length = int.from_bytes(data[pos:pos + 2], "big")
        if length < 2 or pos + length > size:
            raise JpegFormatError(f"invalid length {length} of marker 0x{marker:02X} at offset {pos - 2}")
        payload = data[pos + 2:pos + length]
        pos += length
        jpeg.segments.append(Segment(marker, payload, data[start:pos], start))


def is_jpeg(data: bytes) -> bool:
    return bytes(data[:3]) == b"\xff\xd8\xff"
