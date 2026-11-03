"""Images: Pillow facts + EXIF/XMP/IPTC (plan F2A, D4).

Writable: JPEG only. EXIF, XMP and IPTC are rewritten through
``core.jpeg_segments`` (only the touched APPn segment changes; every other
segment, the scan data and any trailer stay byte-identical) with the
structure-preserving codecs ``core.exif_codec``, ``core.xmp`` and ``core.iptc``.
TIFF/RAW/PNG/WebP/GIF/BMP are read-only.

* A save whose canonical values equal the ones read changes nothing (no-op:
  the file is not rewritten and its snapshot does not change, so no Undo token).
* The MakerNote keeps its absolute offset when EXIF is rewritten (Canon-style
  absolute vendor offsets keep resolving), see ``core.exif_codec``.
* Multi-Picture files (MPO): the MPF offsets of the further images are kept
  consistent by ``core.jpeg_segments``; when they cannot be, the format is
  read-only with ``read_only_reason``.
* JPEGs with extraneous bytes between segments are READ tolerantly (metadata
  shown, with a warning) and are read-only.
* Exact Undo: the snapshot of a key present in the file also carries the exact
  bytes of its whole segment (``_SegmentRaw``); ``restore`` writes those bytes
  back when, after restoring the keys, the segment holds the same content
  (layout, MakerNote position, XMP padding and syntax, IPTC charset marker
  and digest come back byte for byte).

Key namespaces: ``img:`` (Pillow, computed), ``exif:``/``gps:`` (see
``core.exif_codec``), ``xmp:<prefix>:<local>``, ``iptc:<Name>`` (mapped IIM
datasets) or ``iptc:<record>:<dataset>``, ``png:<keyword>``, ``gif:comment``.
"""
from __future__ import annotations

import copy
import math
import mmap
import re
import struct
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from core import exif_codec, icc, image_containers, iptc, jpeg_segments, sniff, tiff_inplace, values, xmp
from core.base_handler import ABSENT, BaseMetadataHandler, Snapshot
from core.errors import ErrorCode, MetadataValueError, MetadataValueErrors
from core.models import MetadataField, MetadataRecord

_READ_ONLY_EXTS = frozenset({".cr2", ".nef", ".arw", ".dng", ".orf", ".rw2", ".pef", ".srw",
                             ".rwl", ".nrw", ".srf", ".sr2", ".3fr", ".erf", ".mos", ".kdc", ".dcr", ".mef", ".iiq",
                             ".cr3", ".raf"})
JPEG_EXTS = frozenset({".jpg", ".jpeg"})


def _jpeg_name(path: Path) -> bool:
    """A JPEG by name: ``.jpg``/``.jpeg`` or a file without extension whose bytes are a JPEG."""
    suffix = path.suffix.lower()
    return suffix in JPEG_EXTS or (suffix == "" and sniff.detect(path) == "jpeg")

MAKERNOTE_WARNING = ("EXIF MakerNote present: it is kept byte for byte at its original offset when "
                     "EXIF is rewritten; vendor data stored outside the MakerNote itself is not tracked")

# ──────────────────────── XMP / IPTC schemas ─────────────────────────────────

_DC = "http://purl.org/dc/elements/1.1/"
_XMP = "http://ns.adobe.com/xap/1.0/"
_PS = "http://ns.adobe.com/photoshop/1.0/"
_RIGHTS = "http://ns.adobe.com/xap/1.0/rights/"
_IPTC_CORE = "http://iptc.org/std/Iptc4xmpCore/1.0/xmlns/"

# key -> (uri, local, form, kind, label); kind: str | text | date | rating
XMP_MAP: dict[str, tuple[str, str, str, str, str]] = {}
for _uri, _local, _form, _kind, _label in (
    (_DC, "title", "alt", "str", "Title"),
    (_DC, "description", "alt", "text", "Description"),
    (_DC, "rights", "alt", "str", "Rights"),
    (_DC, "creator", "seq", "str", "Creator"),
    (_DC, "subject", "bag", "str", "Subject / Keywords"),
    (_XMP, "CreateDate", "text", "date", "Create Date"),
    (_XMP, "ModifyDate", "text", "date", "Modify Date"),
    (_XMP, "MetadataDate", "text", "date", "Metadata Date"),
    (_XMP, "CreatorTool", "text", "str", "Creator Tool"),
    (_XMP, "Rating", "text", "rating", "Rating"),
    (_XMP, "Label", "text", "str", "Label"),
    (_PS, "Headline", "text", "str", "Headline"),
    (_PS, "City", "text", "str", "City"),
    (_PS, "State", "text", "str", "State / Province"),
    (_PS, "Country", "text", "str", "Country"),
    (_PS, "Credit", "text", "str", "Credit"),
    (_PS, "Source", "text", "str", "Source"),
    (_PS, "Instructions", "text", "text", "Instructions"),
    (_PS, "AuthorsPosition", "text", "str", "Author's Position"),
    (_PS, "CaptionWriter", "text", "str", "Caption Writer"),
    (_PS, "TransmissionReference", "text", "str", "Transmission Reference"),
    (_PS, "DateCreated", "text", "date", "Date Created"),
    (_RIGHTS, "WebStatement", "text", "str", "Rights Web Statement"),
    (_RIGHTS, "UsageTerms", "alt", "text", "Usage Terms"),
    (_IPTC_CORE, "Location", "text", "str", "Location"),
    (_IPTC_CORE, "CountryCode", "text", "str", "Country Code"),
):
    XMP_MAP[f"xmp:{xmp.KNOWN_PREFIXES[_uri]}:{_local}"] = (_uri, _local, _form, _kind, _label)
_XMP_BY_NAME = {(v[0], v[1]): k for k, v in XMP_MAP.items()}

_XMP_DATE = re.compile(
    r"[0-9]{4}(-[0-9]{2}(-[0-9]{2}(T[0-9]{2}:[0-9]{2}(:[0-9]{2}(\.[0-9]+)?)?"
    r"(Z|[+-][0-9]{2}:[0-9]{2})?)?)?)?", re.ASCII)
_IPTC_DATE = re.compile(r"[0-9]{8}", re.ASCII)
_IPTC_TIME = re.compile(r"[0-9]{6}([+-][0-9]{4})?", re.ASCII)

# key -> (record, dataset, repeatable, kind, label); kind: str | text | date | time
IPTC_MAP: dict[str, tuple[int, int, bool, str, str]] = {
    "iptc:ObjectName": (2, 5, False, "str", "Object Name"),
    "iptc:EditStatus": (2, 7, False, "str", "Edit Status"),
    "iptc:Category": (2, 15, False, "str", "Category"),
    "iptc:SupplementalCategories": (2, 20, True, "str", "Supplemental Categories"),
    "iptc:Keywords": (2, 25, True, "str", "Keywords"),
    "iptc:SpecialInstructions": (2, 40, False, "text", "Special Instructions"),
    "iptc:DateCreated": (2, 55, False, "date", "Date Created (YYYYMMDD)"),
    "iptc:TimeCreated": (2, 60, False, "time", "Time Created (HHMMSS±HHMM)"),
    "iptc:By-line": (2, 80, True, "str", "By-line (Author)"),
    "iptc:By-lineTitle": (2, 85, True, "str", "By-line Title"),
    "iptc:City": (2, 90, False, "str", "City"),
    "iptc:Sub-location": (2, 92, False, "str", "Sub-location"),
    "iptc:Province-State": (2, 95, False, "str", "Province / State"),
    "iptc:Country-PrimaryLocationCode": (2, 100, False, "str", "Country Code"),
    "iptc:Country-PrimaryLocationName": (2, 101, False, "str", "Country"),
    "iptc:OriginalTransmissionReference": (2, 103, False, "str", "Transmission Reference"),
    "iptc:Headline": (2, 105, False, "str", "Headline"),
    "iptc:Credit": (2, 110, False, "str", "Credit"),
    "iptc:Source": (2, 115, False, "str", "Source"),
    "iptc:CopyrightNotice": (2, 116, False, "str", "Copyright Notice"),
    "iptc:Contact": (2, 118, True, "str", "Contact"),
    "iptc:Caption-Abstract": (2, 120, False, "text", "Caption / Abstract"),
    "iptc:Writer-Editor": (2, 122, True, "str", "Writer / Editor"),
}
_IPTC_BY_NUMBER = {(v[0], v[1]): k for k, v in IPTC_MAP.items()}

# IPTC dataset -> XMP property holding the same information (Metadata Working Group
# guidelines). Only pairs with the same shape (single/single, list/list) and no
# re-composition of the value; dates and the single-valued twins of list datasets
# (By-line Title, Writer/Editor) are not mirrored.
IPTC_XMP_LINKS: dict[str, str] = {
    "iptc:ObjectName": "xmp:dc:title",
    "iptc:Caption-Abstract": "xmp:dc:description",
    "iptc:CopyrightNotice": "xmp:dc:rights",
    "iptc:By-line": "xmp:dc:creator",
    "iptc:Keywords": "xmp:dc:subject",
    "iptc:Headline": "xmp:photoshop:Headline",
    "iptc:City": "xmp:photoshop:City",
    "iptc:Province-State": "xmp:photoshop:State",
    "iptc:Country-PrimaryLocationName": "xmp:photoshop:Country",
    "iptc:Credit": "xmp:photoshop:Credit",
    "iptc:Source": "xmp:photoshop:Source",
    "iptc:SpecialInstructions": "xmp:photoshop:Instructions",
    "iptc:OriginalTransmissionReference": "xmp:photoshop:TransmissionReference",
    "iptc:Sub-location": "xmp:Iptc4xmpCore:Location",
    "iptc:Country-PrimaryLocationCode": "xmp:Iptc4xmpCore:CountryCode",
}
_LINK_PARTNER = {**IPTC_XMP_LINKS, **{x: i for i, x in IPTC_XMP_LINKS.items()}}
_IPTC_RECORD_VERSION = (2, 0)
_IPTC_HIDDEN = frozenset({(1, 0), iptc.CHARSET})
_IPTC_KEY = re.compile(r"iptc:([0-9]{1,3}):([0-9]{1,3})")


# ──────────────────────── JPEG state ─────────────────────────────────────────

@dataclass(frozen=True)
class _SegmentRaw:
    """Raw pre-image of one key + the exact bytes of the segment it was taken from
    (EXIF TIFF block, XMP packet or Photoshop resources) and the segment's index.

    Equality, i.e. what the Undo journal compares to detect changed keys and
    conflicts, is the key's own ``raw`` only. ``segment``/``index`` ride along so
    that ``restore`` can put the segment back byte for byte when, once the keys
    are restored, nothing else in it differs from the original.
    """
    raw: Any
    segment: bytes | None = field(default=None, compare=False, repr=False)
    index: int | None = field(default=None, compare=False)


class _PositionedRaw(tuple):
    """Raw pre-image of an XMP property or IPTC dataset whose equality ignores *positions*.

    The tuple keeps the positions (IPTC index within the record; XMP description / attribute /
    element position) because ``restore`` puts the value back where it was. They are not part of
    the value: the Undo journal compares snapshots to detect a conflicting change of the touched
    keys, and a program that inserted or removed a *neighbour* must not turn that into a conflict.
    ``content`` is what is compared.
    """

    def __new__(cls, items, content):
        self = super().__new__(cls, items)
        self.content = content
        return self

    def __getnewargs__(self):          # copy / deepcopy / pickle
        return tuple(self), self.content

    def __eq__(self, other):
        if isinstance(other, _PositionedRaw):
            return self.content == other.content
        return tuple.__eq__(self, other)

    def __ne__(self, other):
        result = self.__eq__(other)
        return result if result is NotImplemented else not result

    def __hash__(self):
        return hash(self.content)


def _unwrap(raw: Any) -> Any:
    return raw.raw if isinstance(raw, _SegmentRaw) else raw


def _namespace(key: str) -> str:
    if key.startswith(("exif:", "gps:")):
        return "exif"
    if key.startswith("xmp:"):
        return "xmp"
    if key.startswith("iptc:"):
        return "iptc"
    if key.startswith("png:"):
        return "png"
    raise MetadataValueError(key, ErrorCode.UNKNOWN_FIELD)


_SEGMENT_HEADER = {"exif": jpeg_segments.EXIF_HEADER, "xmp": jpeg_segments.XMP_HEADER,
                   "iptc": jpeg_segments.PHOTOSHOP_HEADER}


@dataclass
class _JpegState:
    """Parsed metadata blocks of one JPEG and whether each may be rewritten."""
    jpeg: jpeg_segments.JpegFile
    exif: exif_codec.ExifBlock | None = None
    exif_writable: bool = True
    xmp: xmp.XmpPacket | None = None
    xmp_writable: bool = True
    resources: list[iptc.Resource] | None = None
    resources_trailer: bytes = b""
    iim: list[iptc.Dataset] | None = None
    iim_trailer: bytes = b""
    iptc_writable: bool = True
    iptc_utf8: bool = True                   # decode record 2 as UTF-8 (then Latin-1)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    touched: set[str] = field(default_factory=set)
    exact: dict[str, bytes] = field(default_factory=dict)     # namespace -> payload written verbatim
    hints: dict[str, int] = field(default_factory=dict)       # namespace -> index to re-insert at

    @classmethod
    def load(cls, data: bytes, *, tolerant: bool = False) -> "_JpegState":
        state = cls(jpeg_segments.parse(data, tolerant=tolerant))
        state._load_exif()
        state._load_xmp()
        state._load_iptc()
        return state

    def read_only_reason(self) -> str | None:
        """Why the metadata of this JPEG must not be rewritten (None: it may be)."""
        if self.jpeg.extraneous:
            offset, _count, marker = self.jpeg.extraneous[0]
            total = sum(n for _, n, _ in self.jpeg.extraneous)
            return (f"The JPEG has {total} extraneous byte(s) between its segments (first at offset "
                    f"{offset}, before marker 0x{marker:02X}): metadata is shown but read-only")
        problem = self.jpeg.mpf_problem()
        if problem:
            return (f"The Multi-Picture (MPF) index of this JPEG cannot be kept consistent ({problem}): "
                    "metadata is read-only")
        return None

    def segment(self, ns: str) -> tuple[bytes | None, int | None]:
        """(payload after the signature, index) of the first segment of ``ns``."""
        find = {"exif": self.jpeg.exif_indexes, "xmp": self.jpeg.xmp_indexes,
                "iptc": self.jpeg.photoshop_indexes}[ns]
        found = find()
        if not found:
            return None, None
        return self.jpeg.segments[found[0]].payload[len(_SEGMENT_HEADER[ns]):], found[0]

    def matches(self, ns: str, payload: bytes) -> bool:
        """The (restored) content of ``ns`` equals the content of ``payload``: writing
        ``payload`` verbatim then changes nothing a reader can see."""
        try:
            if ns == "exif":
                return self.exif is not None and exif_codec.same_content(self.exif, exif_codec.parse(payload))
            if ns == "xmp":
                return self.xmp is not None and self.xmp.same_content(xmp.XmpPacket(payload))
            resources, trailer = iptc.parse_resources(payload)
        except (exif_codec.ExifFormatError, xmp.XmpFormatError, iptc.IptcFormatError):
            return False
        managed = (iptc.IPTC_RESOURCE, iptc.IPTC_DIGEST_RESOURCE)

        def others(rs):
            return [r.raw for r in rs if not (r.signature == b"8BIM" and r.rid in managed)]
        original = next((r for r in resources if r.signature == b"8BIM" and r.rid == iptc.IPTC_RESOURCE), None)
        try:
            original_iim = iptc.parse_iim(original.data)[0] if original is not None else []
        except iptc.IptcFormatError:
            return False

        def text(datasets):     # the CodedCharacterSet marker is managed by the writer
            return [d for d in datasets if (d.record, d.number) != iptc.CHARSET]
        marker = iptc.charset_marker(original_iim)
        return (others(self.resources or []) == others(resources) and self.resources_trailer == trailer
                and text(self.iim or []) == text(original_iim)
                and marker in (None, iptc.charset_marker(self.iim or [])))

    def _load_exif(self) -> None:
        idx = self.jpeg.exif_indexes()
        if not idx:
            return
        if len(idx) > 1:
            self.warnings.append(
                f"{len(idx)} EXIF segments: only the first is used; the duplicates are removed "
                "on the next EXIF save")
        payload = self.jpeg.segments[idx[0]].payload[len(jpeg_segments.EXIF_HEADER):]
        try:
            self.exif = exif_codec.parse(payload)
        except exif_codec.ExifFormatError as exc:
            self.errors.append(f"EXIF read error: {exc}")
            self.exif_writable = False
            return
        if self.exif.rewrite_problems:
            self.exif_writable = False
            self.warnings.append("EXIF is read-only: " + "; ".join(self.exif.rewrite_problems))
        if exif_codec.MAKERNOTE[1] in self.exif.ifds["Exif"]:
            self.warnings.append(MAKERNOTE_WARNING)

    def _load_xmp(self) -> None:
        idx = self.jpeg.xmp_indexes()
        if self.jpeg.find(lambda s: s.is_xmp_extension):
            self.xmp_writable = False
            self.warnings.append("Extended XMP (larger than one JPEG segment) is read-only")
        if not idx:
            return
        if len(idx) > 1:
            self.xmp_writable = False
            self.warnings.append(f"{len(idx)} XMP segments: XMP is read-only")
        payload = self.jpeg.segments[idx[0]].payload[len(jpeg_segments.XMP_HEADER):]
        try:
            self.xmp = xmp.XmpPacket(payload)
        except xmp.XmpFormatError as exc:
            self.xmp_writable = False
            self.warnings.append(f"XMP not readable, left untouched: {exc}")

    def _load_iptc(self) -> None:
        idx = self.jpeg.photoshop_indexes()
        if not idx:
            return
        if len(idx) > 1:
            self.iptc_writable = False
            self.warnings.append(f"{len(idx)} Photoshop APP13 segments: IPTC is read-only")
        payload = self.jpeg.segments[idx[0]].payload[len(jpeg_segments.PHOTOSHOP_HEADER):]
        try:
            self.resources, self.resources_trailer = iptc.parse_resources(payload)
            res = self._iptc_resource()
            if res is not None:
                self.iim, self.iim_trailer = iptc.parse_iim(res.data)
        except iptc.IptcFormatError as exc:
            self.iptc_writable = False
            self.resources = None
            self.iim = None
            self.warnings.append(f"IPTC not readable, left untouched: {exc}")
            return
        if self.iim is None:
            return
        marker = iptc.charset_marker(self.iim)
        if marker == iptc.UTF8_MARKER:
            return
        record2 = [d.data for d in self.iim if d.record == 2 and (d.record, d.number) != _IPTC_RECORD_VERSION]
        all_utf8 = all(_is_utf8(d) for d in record2)
        if marker is None and all_utf8:
            return
        self.iptc_utf8 = marker is None
        self.iptc_writable = False
        self.warnings.append("IPTC uses a legacy character set: IPTC is read-only")

    def _iptc_resource(self) -> iptc.Resource | None:
        for r in self.resources or []:
            if r.signature == b"8BIM" and r.rid == iptc.IPTC_RESOURCE:
                return r
        return None

    # ── save ──
    def to_bytes(self, size_keys: dict[str, str | None]) -> bytes:
        if "exif" in self.touched:
            tiff = self.exact.get("exif")
            if tiff is None and self.exif is not None and (any(self.exif.ifds.values()) or self.exif.thumbnail):
                tiff = exif_codec.serialize(self.exif)
            self.jpeg.set_exif(tiff, key=size_keys.get("exif"), index=self.hints.get("exif"))
        if "xmp" in self.touched and self.xmp is not None:
            packet = self.exact.get("xmp")
            if packet is None and not self.xmp.is_blank():   # nothing left in a new packet: drop it
                packet = self.xmp.to_bytes()
            self.jpeg.set_xmp(packet, key=size_keys.get("xmp"), index=self.hints.get("xmp"))
        if "iptc" in self.touched and "iptc" in self.exact:
            self.jpeg.set_photoshop(self.exact["iptc"], index=self.hints.get("iptc"))
        elif "iptc" in self.touched:
            # MWG: a digest equal to the IIM's MD5 tells readers the XMP is in sync and preferred
            # over the IIM. An IIM edit that does not also write the XMP must leave the old digest
            # alone (the mismatch makes the IIM win); a file without XMP has nothing to prefer.
            # The XMP also has to agree with the IIM on every mapped pair, else it is not in sync.
            sync_digest = (not self.jpeg.xmp_indexes()
                           or ("xmp" in self.touched and _linked_fields_agree(self)))
            resources = list(self.resources or [])
            datasets = self.iim or []
            if all((d.record, d.number) == iptc.CHARSET for d in datasets):
                datasets = []        # a CodedCharacterSet marker alone describes no data
            data = iptc.serialize_iim(datasets)
            if datasets:
                body = data + self.iim_trailer if self._iptc_resource() is not None else data
                existing = self._iptc_resource()
                if existing is not None:
                    resources[resources.index(existing)] = existing.with_data(body)
                else:
                    resources.append(iptc.Resource.build(iptc.IPTC_RESOURCE, body))
                if sync_digest:
                    iptc.update_digest(resources, body)
            else:
                existing = self._iptc_resource()
                if existing is not None:
                    resources.remove(existing)
                iptc.update_digest(resources, None)
            if resources or self.resources_trailer:
                payload = iptc.serialize_resources(resources, self.resources_trailer)
                self.jpeg.set_photoshop(payload, key=size_keys.get("iptc"), index=self.hints.get("iptc"))
            else:
                self.jpeg.set_photoshop(None)
            self.resources = resources
        return self.jpeg.to_bytes()


class _PngState(_JpegState):
    """Metadata blocks of a PNG: EXIF in ``eXIf``, XMP in an ``iTXt`` chunk; no IPTC.

    Reuses the editing model of ``_JpegState`` (``exif`` / ``xmp`` blocks, ``touched``, ``exact``): only the
    container I/O differs. Text chunks are removed through ``png`` (never edited)."""
    png: image_containers.PngFile

    @classmethod
    def load(cls, data: bytes, *, tolerant: bool = False) -> "_PngState":
        try:
            png = image_containers.PngFile.parse(data)
        except image_containers.ContainerError as exc:
            raise jpeg_segments.JpegFormatError(f"PNG structure error: {exc}") from exc
        state = cls(jpeg_segments.JpegFile(b""))
        state.png = png
        state.iptc_writable = False
        state._load_exif()
        state._load_xmp()
        return state

    def read_only_reason(self) -> str | None:
        return None

    def segment(self, ns: str) -> tuple[bytes | None, int | None]:
        if ns == "exif":
            found = self.png.index_of(b"eXIf")
            return (self.png.chunks[found[0]][1], found[0]) if found else (None, None)
        if ns == "xmp":
            found = self.png.xmp_indexes()
            return (self.png.xmp_packet(found[0]), found[0]) if found else (None, None)
        return None, None

    def _load_exif(self) -> None:
        found = self.png.index_of(b"eXIf")
        if not found:
            return
        if len(found) > 1:
            self.warnings.append(f"{len(found)} EXIF chunks: only the first is used; the duplicates are "
                                 "removed on the next EXIF save")
        try:
            self.exif = exif_codec.parse(_strip_exif_header(self.png.chunks[found[0]][1]))
        except exif_codec.ExifFormatError as exc:
            self.errors.append(f"EXIF read error: {exc}")
            self.exif_writable = False
            return
        if self.exif.rewrite_problems:
            self.exif_writable = False
            self.warnings.append("EXIF is read-only: " + "; ".join(self.exif.rewrite_problems))

    def _load_xmp(self) -> None:
        found = self.png.xmp_indexes()
        if not found:
            return
        if len(found) > 1:
            self.xmp_writable = False
            self.warnings.append(f"{len(found)} XMP chunks: XMP is read-only")
        packet = self.png.xmp_packet(found[0])
        try:
            if packet is None:
                raise xmp.XmpFormatError("the chunk cannot be decoded")
            self.xmp = xmp.XmpPacket(packet)
        except xmp.XmpFormatError as exc:
            self.xmp_writable = False
            self.warnings.append(f"XMP not readable, left untouched: {exc}")

    def _load_iptc(self) -> None:
        return

    def to_bytes(self, size_keys: dict[str, str | None]) -> bytes:
        if "exif" in self.touched:
            tiff = self.exact.get("exif")
            if tiff is None and self.exif is not None and (any(self.exif.ifds.values()) or self.exif.thumbnail):
                tiff = exif_codec.serialize(self.exif)
            self.png.set_exif(tiff, self.hints.get("exif"))
        if "xmp" in self.touched and self.xmp is not None:
            packet = self.exact.get("xmp")
            if packet is None and not self.xmp.is_blank():
                packet = self.xmp.to_bytes()
            self.png.set_xmp(packet, self.hints.get("xmp"))
        return self.png.to_bytes()


class _WebpState(_JpegState):
    """Metadata blocks of a WebP: ``EXIF`` and ``XMP `` chunks; no IPTC."""
    webp: image_containers.WebpFile

    @classmethod
    def load(cls, data: bytes, *, tolerant: bool = False) -> "_WebpState":
        try:
            webp = image_containers.WebpFile.parse(data)
        except image_containers.ContainerError as exc:
            raise jpeg_segments.JpegFormatError(f"WebP structure error: {exc}") from exc
        state = cls(jpeg_segments.JpegFile(b""))
        state.webp = webp
        state.iptc_writable = False
        state._load_exif()
        state._load_xmp()
        return state

    def read_only_reason(self) -> str | None:
        if self.webp.find(b"ANIM") or self.webp.find(b"ANMF"):
            return "Animated WebP files are read-only"
        return None

    def segment(self, ns: str) -> tuple[bytes | None, int | None]:
        found = self.webp.find({"exif": b"EXIF", "xmp": b"XMP "}.get(ns, b""))
        return (self.webp.chunks[found[0]][1], found[0]) if found else (None, None)

    def _load_exif(self) -> None:
        found = self.webp.find(b"EXIF")
        if not found:
            return
        if len(found) > 1:
            self.warnings.append(f"{len(found)} EXIF chunks: only the first is used")
        try:
            self.exif = exif_codec.parse(_strip_exif_header(self.webp.chunks[found[0]][1]))
        except exif_codec.ExifFormatError as exc:
            self.errors.append(f"EXIF read error: {exc}")
            self.exif_writable = False
            return
        if self.exif.rewrite_problems:
            self.exif_writable = False
            self.warnings.append("EXIF is read-only: " + "; ".join(self.exif.rewrite_problems))

    def _load_xmp(self) -> None:
        found = self.webp.find(b"XMP ")
        if not found:
            return
        if len(found) > 1:
            self.xmp_writable = False
            self.warnings.append(f"{len(found)} XMP chunks: XMP is read-only")
        try:
            self.xmp = xmp.XmpPacket(self.webp.chunks[found[0]][1])
        except xmp.XmpFormatError as exc:
            self.xmp_writable = False
            self.warnings.append(f"XMP not readable, left untouched: {exc}")

    def _load_iptc(self) -> None:
        return

    def to_bytes(self, size_keys: dict[str, str | None]) -> bytes:
        try:
            if "exif" in self.touched:
                tiff = self.exact.get("exif")
                if tiff is None and self.exif is not None and (any(self.exif.ifds.values()) or self.exif.thumbnail):
                    tiff = exif_codec.serialize(self.exif)
                self.webp.set_exif(tiff)
            if "xmp" in self.touched and self.xmp is not None:
                packet = self.exact.get("xmp")
                if packet is None and not self.xmp.is_blank():
                    packet = self.xmp.to_bytes()
                self.webp.set_xmp(packet)
        except image_containers.ContainerError as exc:
            raise MetadataValueError(None, ErrorCode.UNSUPPORTED, f"WebP cannot take this metadata: {exc}") from exc
        return self.webp.to_bytes()


_XMP_UUID = bytes.fromhex("be7acfcb97a942e89c71999491e3afac")


def _mp4_xmp_location(data: bytes) -> tuple[int, int] | None:
    """(start, end) of the XMP packet of an MP4-family file: a ``uuid`` atom at the root or an ``XMP_`` atom in ``udta``."""
    from core.isobmff import Reader
    rd = Reader(data)
    for box in rd.children():
        if box.type == b"uuid" and box.usertype == _XMP_UUID:
            return box.data_start, box.end
    moov = rd.find(None, b"moov")
    udta = rd.find(moov, b"udta") if moov is not None else None
    if udta is not None:
        for box in rd.children(udta.data_start, udta.end):
            if box.type == b"XMP_":
                return box.data_start, box.end
    return None


class _Mp4State(_JpegState):
    """The XMP packet of an MP4-family file. Changed in place: the packet never grows, the atoms around it
    stay where they are, so no media offset changes."""
    data: bytes
    location: tuple[int, int] | None

    @classmethod
    def load(cls, data: bytes, *, tolerant: bool = False) -> "_Mp4State":
        state = cls(jpeg_segments.JpegFile(b""))
        state.data = data
        state.iptc_writable = False
        state.exif_writable = False             # EXIF is not stored in these files
        state.location = _mp4_xmp_location(data)
        state._load_xmp()
        if state.location is None:
            state.xmp_writable = True            # no packet yet: a first property is stored in a new atom
        return state

    def read_only_reason(self) -> str | None:
        return None

    def segment(self, ns: str) -> tuple[bytes | None, int | None]:
        if ns == "xmp" and self.location is not None:
            return self.data[self.location[0]:self.location[1]], 0
        return None, None

    def _load_exif(self) -> None:
        return

    def _load_iptc(self) -> None:
        return

    def _load_xmp(self) -> None:
        if self.location is None:
            self.xmp_writable = False
            return
        try:
            self.xmp = xmp.XmpPacket(self.data[self.location[0]:self.location[1]])
        except xmp.XmpFormatError as exc:
            self.xmp_writable = False
            self.warnings.append(f"XMP not readable, left untouched: {exc}")

    def to_bytes(self, size_keys: dict[str, str | None]) -> bytes:
        if "xmp" in self.touched and self.xmp is not None and self.location is None and not self.xmp.is_blank():
            return self._appended(self.xmp.to_bytes(), clear=None)
        if "xmp" not in self.touched or self.xmp is None or self.location is None:
            return self.data
        start, end = self.location
        packet = self.exact.get("xmp")
        if packet is None:
            packet = (xmp.XmpPacket.new() if self.xmp.is_blank() else self.xmp).to_bytes()
        if len(packet) > end - start:
            # does not fit: a new atom at the end of the file, the old one is cleared (nothing moves)
            return self._appended(packet, clear=(start, end))
        return self.data[:start] + packet + b" " * (end - start - len(packet)) + self.data[end:]

    def _appended(self, packet: bytes, clear: tuple[int, int] | None) -> bytes:
        out = bytearray(self.data)
        if clear is not None:
            start, end = clear
            # the old atom becomes a ``free`` atom (so that it is not found again) and its content is cleared
            header = start - 24 if bytes(out[start - 20:start - 16]) == b"uuid" else start - 8
            out[header + 4:header + 8] = b"free"
            out[header + 8:end] = bytes(end - header - 8)
        out += struct.pack(">I", 24 + len(packet)) + b"uuid" + _XMP_UUID + packet
        return bytes(out)


_TIFF_EXTS = frozenset({".tif", ".tiff"}) | _READ_ONLY_EXTS


class _TiffState(_JpegState):
    """A TIFF / DNG / TIFF-based RAW: entries are removed in place and the XMP packet (tag 700) is reduced
    in place, so nothing in the file moves. Nothing is added or edited."""
    buf: bytearray

    @classmethod
    def load(cls, data: bytes, *, tolerant: bool = False) -> "_TiffState":
        state = cls(jpeg_segments.JpegFile(b""))
        state.buf = bytearray(data)
        state.iptc_writable = False
        try:
            tiff_inplace.parse(state.buf)
        except (tiff_inplace.TiffError, struct.error) as exc:
            raise jpeg_segments.JpegFormatError(f"TIFF structure error: {exc}") from exc
        state._load_exif()
        state._load_xmp()
        state._original = {name: dict(entries) for name, entries in state.exif.ifds.items()} if state.exif else {}
        return state

    def read_only_reason(self) -> str | None:
        return None

    def _location(self) -> tuple[int, int] | None:
        _, ifds = tiff_inplace.parse(self.buf)
        entry = next((e for e in ifds["0th"].entries if e.tag == 700), None)
        if entry is None or entry.data_at is None or entry.type not in (1, 7):
            return None
        return entry.data_at, entry.data_at + entry.size

    def segment(self, ns: str) -> tuple[bytes | None, int | None]:
        if ns == "xmp":
            location = self._location()
            if location is not None:
                return bytes(self.buf[location[0]:location[1]]), 0
        return None, None

    def _load_exif(self) -> None:
        try:
            self.exif = exif_codec.parse(bytes(self.buf), with_ifd1=False, raw=True)
        except exif_codec.ExifFormatError as exc:
            self.errors.append(f"EXIF read error: {exc}")
            self.exif_writable = False

    def _load_xmp(self) -> None:
        location = self._location()
        if location is None:
            self.xmp_writable = not any(e.tag == 700 for e in tiff_inplace.parse(self.buf)[1]["0th"].entries)
            return
        try:
            self.xmp = xmp.XmpPacket(bytes(self.buf[location[0]:location[1]]))
        except xmp.XmpFormatError as exc:
            self.xmp_writable = False
            self.warnings.append(f"XMP not readable, left untouched: {exc}")

    def _load_iptc(self) -> None:
        return

    def _apply_exif_changes(self) -> None:
        """Write the entries that differ from the file: over the old value when it fits, else appended."""
        if self.exif is None:
            return
        try:
            for name in ("0th", "Exif", "GPS", "Interop"):
                for tag, entry in sorted(self.exif.ifds[name].items()):
                    if self._original.get(name, {}).get(tag) != entry:
                        if not tiff_inplace.editable(name, tag):
                            raise MetadataValueError(exif_codec.key_for(name, tag), ErrorCode.READ_ONLY)
                        tiff_inplace.set_entry(self.buf, name, tag, entry.type, entry.count, entry.data)
        except tiff_inplace.TiffError as exc:
            raise MetadataValueError(None, ErrorCode.UNSUPPORTED, f"the EXIF change cannot be stored: {exc}") from exc

    def _apply_xmp(self) -> None:
        packet = self.exact.get("xmp")
        if packet is None and self.xmp.is_blank():
            tiff_inplace.remove(self.buf, "0th", {700}, force=True)          # nothing left: drop the packet
            return
        if packet is None:
            packet = self.xmp.to_bytes()
        location = self._location()
        try:
            if location is not None and len(packet) <= location[1] - location[0]:
                start, end = location
                self.buf[start:end] = packet + b" " * (end - start - len(packet))
            else:
                tiff_inplace.set_entry(self.buf, "0th", 700, 1, len(packet), packet)
        except tiff_inplace.TiffError as exc:
            raise MetadataValueError(None, ErrorCode.UNSUPPORTED, f"the XMP change cannot be stored: {exc}") from exc

    def to_bytes(self, size_keys: dict[str, str | None]) -> bytes:
        if "exif" in self.touched:
            self._apply_exif_changes()
        if "xmp" in self.touched and self.xmp is not None:
            self._apply_xmp()
        return bytes(self.buf)


class _HeifState(_TiffState):
    """HEIC / HEIF / AVIF: the TIFF part of the Exif item and the XMP item are changed where they are; an item
    that outgrows its place is written again at the end of the file as a new ``mdat`` atom and its entry in the
    item location table is pointed there (the old bytes are zeroed). Image items and every other offset stay."""
    data: bytearray
    layout: dict

    @classmethod
    def load(cls, data: bytes, *, tolerant: bool = False) -> "_HeifState":
        from core.handlers.heif_handler import metadata_layout
        layout = metadata_layout(data)
        if not layout:
            raise jpeg_segments.JpegFormatError("HEIF structure error: no Exif or XMP item stored in one piece")
        state = cls(jpeg_segments.JpegFile(b""))
        state.data = bytearray(data)
        state.layout = layout
        exif = layout.get("exif")
        state.buf = bytearray(data[exif["range"][0]:exif["range"][1]]) if exif else bytearray()
        state.iptc_writable = False
        if exif:
            state._load_exif()
            state._original = {name: dict(entries) for name, entries in state.exif.ifds.items()} if state.exif else {}
        else:
            state._original = {}
        state._load_xmp()
        return state

    def _xmp_range(self) -> tuple[int, int] | None:
        item = self.layout.get("xmp")
        return item["range"] if item else None

    def _location(self) -> tuple[int, int] | None:
        return self._xmp_range()

    def segment(self, ns: str) -> tuple[bytes | None, int | None]:
        if ns == "xmp" and self._xmp_range() is not None:
            start, end = self._xmp_range()
            return bytes(self.data[start:end]), 0
        return None, None

    def _load_xmp(self) -> None:
        located = self._xmp_range()
        if located is None:
            self.xmp_writable = False             # no XMP item: this file has nowhere to put XMP properties
            return
        try:
            self.xmp = xmp.XmpPacket(bytes(self.data[located[0]:located[1]]))
        except xmp.XmpFormatError as exc:
            self.xmp_writable = False
            self.warnings.append(f"XMP not readable, left untouched: {exc}")

    def _move_item(self, name: str, payload: bytes) -> None:
        """Write ``payload`` as the new content of the item ``name`` at the end of the file."""
        entry = self.layout[name]
        offset_at, offset_size, length_at, length_size, base = entry["loc"]
        start = len(self.data) + 8
        if start - base >= 1 << (8 * offset_size) or len(payload) >= 1 << (8 * length_size):
            raise MetadataValueError(None, ErrorCode.TOO_LARGE, "the item location table cannot address the new item")
        old_start, old_end = entry["item"]
        self.data.extend(struct.pack(">I", 8 + len(payload)) + b"mdat" + payload)
        self.data[old_start:old_end] = bytes(old_end - old_start)
        self.data[offset_at:offset_at + offset_size] = (start - base).to_bytes(offset_size, "big")
        self.data[length_at:length_at + length_size] = len(payload).to_bytes(length_size, "big")
        entry["item"] = (start, start + len(payload))
        entry["range"] = (start + (4 if name == "exif" else 0), start + len(payload))

    def to_bytes(self, size_keys: dict[str, str | None]) -> bytes:
        if "exif" in self.touched:
            self._apply_exif_changes()
        exif = self.layout.get("exif")
        if exif is not None and (bytes(self.buf) != bytes(self.data[exif["range"][0]:exif["range"][1]])):
            start, end = exif["range"]
            if len(self.buf) <= end - start:
                self.data[start:end] = bytes(self.buf).ljust(end - start, b"\x00")
            else:
                self._move_item("exif", struct.pack(">I", 0) + bytes(self.buf))
        if "xmp" in self.touched and self.xmp is not None:
            located = self._xmp_range()
            packet = self.exact.get("xmp")
            if packet is None:
                packet = (xmp.XmpPacket.new() if self.xmp.is_blank() else self.xmp).to_bytes()
            if located is None:
                raise MetadataValueError(None, ErrorCode.UNSUPPORTED,
                                         "this file has no XMP item: XMP cannot be added to it")
            if len(packet) <= located[1] - located[0]:
                self.data[located[0]:located[1]] = packet + b" " * (located[1] - located[0] - len(packet))
            else:
                self._move_item("xmp", packet)
        return bytes(self.data)


IPTC_BLOCK_KEY = "iptc:@block"


def _tiff_iptc_block(buf, writable: bool) -> list[MetadataField]:
    """The IPTC-NAA record of a TIFF as one removable field (its datasets are not edited one by one)."""
    try:
        _, ifds = tiff_inplace.parse(buf)
        entry = next((e for e in ifds["0th"].entries if e.tag == 33723), None)
    except (tiff_inplace.TiffError, struct.error):
        return []
    if entry is None or entry.data_at is None:
        return []
    data = bytes(buf[entry.data_at:entry.data_at + entry.size])
    field = MetadataField.binary_field(IPTC_BLOCK_KEY, "IPTC record (all its fields)", data, mime="application/x-iptc",
                                       deletable=writable, source="iptc")
    field.computed = True
    return [field]


def _tiff_deletable(fields: list[MetadataField], writable: bool) -> None:
    """Mark which EXIF/GPS entries of a TIFF-based file can be removed; nothing can be edited in place."""
    for f in fields:
        if f.key.startswith(("exif:", "gps:")):
            where = exif_codec.parse_key(f.key)
            f.deletable = bool(writable and where and tiff_inplace.removable(*where))
            f.editable = bool(writable and where and f.editable and f.field_type != "binary"
                              and tiff_inplace.editable(*where))
            if where in (("0th", 700), ("0th", 33723)):
                f.computed = True        # the raw XMP / IPTC bytes change with their interpreted fields
            continue
        elif f.key == IPTC_BLOCK_KEY:
            f.deletable = bool(writable)
        elif f.key.startswith(("xmp:", "iptc:")):
            f.deletable = bool(writable and f.key.startswith("xmp:") and f.deletable)
            f.editable = bool(writable and f.key.startswith("xmp:") and f.editable)
        else:
            f.editable = False


def _container_kind(path: Path) -> str | None:
    """``jpeg`` / ``png`` / ``webp`` when the file may be edited as such: its bytes say so and its
    extension agrees (or it has none)."""
    suffix = path.suffix.lower()
    kind = sniff.detect(path)
    if kind == "tiff" or (kind is None and suffix in _READ_ONLY_EXTS and _read_head(path)[:4] in exif_codec.RAW_MAGICS):
        return "tiff" if suffix in _TIFF_EXTS else None
    if kind not in ("jpeg", "png", "webp"):
        return None
    allowed = {"jpeg": JPEG_EXTS, "png": {".png"}, "webp": {".webp"}}[kind]
    return kind if suffix in allowed or suffix == "" else None


def _state_for(data: bytes) -> "_JpegState":
    """Metadata state of the container the bytes are (JPEG, PNG or WebP)."""
    if data[:8] == _PNG_SIGNATURE:
        return _PngState.load(data)
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return _WebpState.load(data)
    if data[4:8] == b"ftyp":
        from core.handlers.heif_handler import is_heif_brand
        return _HeifState.load(data) if is_heif_brand(data) else _Mp4State.load(data)
    if data[:4] in (b"II*\x00", b"MM\x00*") or data[:4] in exif_codec.RAW_MAGICS:
        return _TiffState.load(data)
    return _JpegState.load(data)


def _png_text_key(chunk: list) -> str | None:
    """Field key (``png:<keyword>`` or ``png:<keyword>@<language>``) of a text chunk."""
    found = image_containers.PngFile.text_keyword(chunk)
    if found is None:
        return None
    keyword, language = found
    return f"png:{keyword}@{language}" if language else f"png:{keyword}"


def _png_text_fields(png: "image_containers.PngFile", writable: bool) -> list[MetadataField]:
    """PNG text chunks: shown, and removable (never edited in place)."""
    texts, _ = _png_metadata(png.to_bytes(), [], None)
    return [MetadataField(f"png:{k}", k, v, editable=False, deletable=writable,
                          field_type="text" if "\n" in v else "str", source="png")
            for k, v in texts]


_CANON_UUID = bytes.fromhex("85c0b687820f11e08111f4ce462b6a48")


def _cr3_fields(path: Path, warnings: list[str]) -> list[MetadataField]:
    """EXIF / GPS of a Canon CR3: the tables CMT1 (main), CMT2 (Exif) and CMT4 (GPS) of the Canon ``uuid`` atom."""
    from core.isobmff import open_reader
    block = exif_codec.ExifBlock.empty()
    found = False
    with open_reader(path) as rd:
        moov = rd.find(None, b"moov")
        canon = next((b for b in (rd.children(moov.data_start, moov.end) if moov else [])
                      if b.type == b"uuid" and b.usertype == _CANON_UUID), None)
        if canon is None:
            warnings.append("No Canon metadata atom found")
            return []
        for atom in rd.children(canon.data_start, canon.end):
            target = {b"CMT1": "0th", b"CMT2": "Exif", b"CMT4": "GPS"}.get(atom.type)
            if target is None:
                continue
            try:
                part = exif_codec.parse(rd.payload(atom), with_ifd1=False)
            except exif_codec.ExifFormatError as exc:
                warnings.append(f"{atom.type.decode()} not readable: {exc}")
                continue
            block.byteorder = part.byteorder if not found else block.byteorder
            block.ifds[target] = part.ifds["0th"]
            found = True
    return _exif_fields(block, False) if found else []


def _raf_fields(path: Path, warnings: list[str]) -> list[MetadataField]:
    """EXIF / XMP / IPTC of the JPEG preview stored in a Fujifilm RAF file."""
    with open(path, "rb") as stream:
        head = stream.read(0x5C)
        if len(head) < 0x5C:
            return []
        offset, length = struct.unpack(">II", head[0x54:0x5C])
        stream.seek(offset)
        jpeg = stream.read(min(length, 64 * 1024 * 1024))
    if not jpeg_segments.is_jpeg(jpeg[:4]):
        warnings.append("The preview picture of this RAF could not be found")
        return []
    try:
        state = _JpegState.load(jpeg, tolerant=True)
    except jpeg_segments.JpegFormatError as exc:
        warnings.append(f"RAF preview not readable: {exc}")
        return []
    warnings.extend(state.warnings)
    return ImageHandler._state_fields(state, False)


def _linked_fields_agree(state: _JpegState) -> bool:
    """Every IPTC dataset that has an XMP twin in the file holds the same value as the twin."""
    fields = {f.key: f for f in ImageHandler._state_fields(state, False)}
    return all(_same(fields[iptc_key].value, fields[xmp_key].value)
               for iptc_key, xmp_key in IPTC_XMP_LINKS.items()
               if iptc_key in fields and xmp_key in fields)


def _is_utf8(data: bytes) -> bool:
    try:
        data.decode("utf-8")
        return True
    except UnicodeDecodeError:
        return False


def _clean(text: str) -> bool:
    return all(c.isprintable() or c in "\t\n\r" for c in text)


# ──────────────────────── field builders ─────────────────────────────────────

def _exif_fields(block: exif_codec.ExifBlock, writable: bool) -> list[MetadataField]:
    fields: list[MetadataField] = []
    for ifd in ("0th", "Exif", "GPS", "Interop", "1st"):
        for tag in sorted(block.ifds[ifd]):
            if tag in exif_codec.HIDDEN_TAGS:
                continue
            fields.append(_exif_field(block, ifd, tag, writable))
    if block.thumbnail is not None:
        fields.append(MetadataField.binary_field(
            "exif:Thumbnail", "Thumbnail", block.thumbnail, mime="image/jpeg",
            deletable=False, source="exif"))
    return fields


def _exif_field(block, ifd: str, tag: int, writable: bool) -> MetadataField:
    entry = block.ifds[ifd][tag]
    key = exif_codec.key_for(ifd, tag)
    label = exif_codec.label_for(key, ifd, tag)
    source = "gps" if ifd == "GPS" else "exif"
    dec = exif_codec.decode(ifd, tag, entry, block.byteorder)
    deletable = writable and ifd in ("0th", "Exif", "GPS") and (ifd, tag) not in exif_codec.STRUCTURE_TAGS
    if dec.field_type == "binary":
        mime = None
        return MetadataField.binary_field(key, label, dec.binary, mime=mime, deletable=deletable,
                                          source=source, raw_value=entry.data)
    return MetadataField(key, label, dec.value, raw_value=entry.data,
                         editable=writable and dec.editable, deletable=deletable,
                         field_type=dec.field_type, source=source, choices=dec.choices,
                         multi=dec.multi, length=dec.length)


def _dpi_field(x: float, y: float) -> list[MetadataField]:
    if not (math.isfinite(x) and math.isfinite(y)):
        return []
    return [MetadataField("img:dpi", "DPI", [float(x), float(y)], editable=False, deletable=False,
                          field_type="float", source="image", multi=True, length=2)]


def _jpeg_dpi(state: "_JpegState") -> list[MetadataField]:
    """DPI of a JPEG: JFIF density (units 1 = inch, 2 = cm), else EXIF X/YResolution
    with ResolutionUnit (2 = inch, the default; 3 = cm). Nothing when neither says."""
    for seg in state.jpeg.segments:
        if seg.is_jfif and seg.payload[:5] == b"JFIF\x00" and len(seg.payload) >= 12:
            units = seg.payload[7]
            x, y = (int.from_bytes(seg.payload[8:10], "big"), int.from_bytes(seg.payload[10:12], "big"))
            if units in (1, 2) and x and y:
                factor = 2.54 if units == 2 else 1.0
                return _dpi_field(x * factor, y * factor)
            break
    block = state.exif
    if block is None:
        return []
    ifd0 = block.ifds["0th"]
    xr, yr = ifd0.get(0x011A), ifd0.get(0x011B)
    if xr is None or yr is None or xr.type != 5 or yr.type != 5 or xr.count != 1 or yr.count != 1:
        return []
    (xn, xd), (yn, yd) = (struct.unpack(block.byteorder + "LL", e.data) for e in (xr, yr))
    if not xd or not yd:
        return []
    unit = ifd0.get(0x0128)
    unit = struct.unpack(block.byteorder + "H", unit.data)[0] if unit is not None and unit.type == 3         and unit.count == 1 else 2
    factor = {2: 1.0, 3: 2.54}.get(unit)
    if factor is None:
        return []
    return _dpi_field(xn / xd * factor, yn / yd * factor)


def _xmp_key_map(packet: xmp.XmpPacket) -> dict[str, tuple[str, str]]:
    """Key -> (uri, local) for every property of the packet (mapped keys use the table)."""
    out: dict[str, tuple[str, str]] = {}
    for prop in packet.properties():
        key = _XMP_BY_NAME.get((prop.uri, prop.local))
        if key is None:
            prefix = xmp.KNOWN_PREFIXES.get(prop.uri) or prop.prefix or "ns"
            key = f"xmp:{prefix}:{prop.local}"
        out.setdefault(key, (prop.uri, prop.local))
    return out


XMP_HIDDEN_KEY = "xmp:@hidden"


def _xmp_fields(packet: xmp.XmpPacket, writable: bool) -> list[MetadataField]:
    fields: list[MetadataField] = []
    hidden: list[xmp.Prop] = []
    seen: set[tuple[str, str]] = set()
    for prop in packet.properties():
        name = (prop.uri, prop.local)
        if name in seen:
            continue
        seen.add(name)
        key = _XMP_BY_NAME.get(name)
        if key is not None:
            f = _mapped_xmp_field(packet, prop, key, writable)
        else:
            f = _unmapped_xmp_field(packet, prop, writable)
        if f is not None:
            fields.append(f)
        else:
            hidden.append(prop)
    if hidden:
        # Structured data (edit history, document ids, derived-from...) that has no field of its own:
        # listed so that it is not a blind spot, and removable as a whole.
        names = [f"{xmp.KNOWN_PREFIXES.get(p.uri) or p.prefix or 'ns'}:{p.local}" for p in hidden]
        fields.append(MetadataField(XMP_HIDDEN_KEY, "XMP data not shown", names, raw_value=names, editable=False,
                                    deletable=writable, field_type="str", source="xmp", multi=True, computed=True))
    return fields


def _xmp_hidden(packet: xmp.XmpPacket) -> list[tuple[str, str]]:
    """(uri, local) of the properties of the packet that have no field of their own."""
    return [(p.uri, p.local) for p in packet.properties()
            if ((_mapped_xmp_field(packet, p, _XMP_BY_NAME[(p.uri, p.local)], True)
                 if (p.uri, p.local) in _XMP_BY_NAME else _unmapped_xmp_field(packet, p, True)) is None)]


def _mapped_xmp_field(packet, prop, key, writable) -> MetadataField | None:
    uri, local, form, kind, label = XMP_MAP[key]
    value = packet.read_value(prop, form)
    if value is None:
        return None  # unexpected shape: preserved, not shown
    multi = form in ("bag", "seq")
    field_type = "text" if kind == "text" else "str"
    editable = writable
    if kind == "rating":
        if re.fullmatch(r"-?[0-9]+", value or "") and -1 <= int(value) <= 5 and str(int(value)) == value:
            value, field_type = int(value), "int"
        else:
            editable = False
    elif kind == "date":
        field_type, value, ok = _xmp_date_view(value)
        editable = editable and ok
    if multi and not value:
        return None
    return MetadataField(key, f"XMP {label}", value, editable=editable, deletable=writable,
                         field_type=field_type, source="xmp", multi=multi,
                         has_alternatives=form == "alt" and packet.has_alternatives(prop))


def _xmp_date_view(text: str) -> tuple[str, str, bool]:
    """(field_type, value, editable) of an XMP date: full date-times are typed
    ``datetime`` (with offset) or ``localdatetime``; partial dates stay ``str``."""
    if not _XMP_DATE.fullmatch(text):
        return "str", text, False
    if "T" not in text:
        return "str", text, True
    try:
        return "datetime", values.format_datetime(values.parse_datetime(text)), True
    except MetadataValueError:
        pass
    try:
        return "localdatetime", values.format_datetime(values.parse_localdatetime(text)), True
    except MetadataValueError:
        return "str", text, False


def _xmp_empty_array(packet: xmp.XmpPacket, uri: str, local: str, form: str) -> bool:
    """Every occurrence of the property is an empty ``rdf:Bag``/``rdf:Seq`` of ``form``."""
    occurrences = packet.find(uri, local)
    return form in ("bag", "seq") and bool(occurrences) and \
        all(packet.read_value(p, form) == [] for p in occurrences)


def _unmapped_xmp_field(packet, prop, writable) -> MetadataField | None:
    prefix = xmp.KNOWN_PREFIXES.get(prop.uri) or prop.prefix or "ns"
    key = f"xmp:{prefix}:{prop.local}"
    for form in ("text", "bag", "seq", "alt"):
        value = packet.read_value(prop, form)
        if value is None or value == []:
            continue
        multi = isinstance(value, list)
        return MetadataField(key, f"XMP {prefix}:{prop.local}", value, editable=False,
                             deletable=writable, field_type="str", source="xmp", multi=multi)
    return None


def _iptc_text(data: bytes, utf8: bool) -> str:
    return values.decode_text(data, ("utf-8",) if utf8 else ())


def _iptc_fields(state: _JpegState, writable: bool) -> list[MetadataField]:
    fields: list[MetadataField] = []
    groups: dict[tuple[int, int], list[bytes]] = {}
    for d in state.iim or []:
        groups.setdefault((d.record, d.number), []).append(d.data)
    for (record, number), payloads in groups.items():
        if (record, number) in _IPTC_HIDDEN:
            continue
        if (record, number) == _IPTC_RECORD_VERSION:
            if len(payloads) == 1 and len(payloads[0]) == 2:
                fields.append(MetadataField("iptc:ApplicationRecordVersion", "IPTC Record Version",
                                            int.from_bytes(payloads[0], "big"), editable=False,
                                            deletable=False, field_type="int", source="iptc"))
            continue
        key = _IPTC_BY_NUMBER.get((record, number))
        texts = [_iptc_text(p, state.iptc_utf8) for p in payloads]
        if key is None:
            key = f"iptc:{record}:{number}"
            label = f"IPTC {record}:{number}"
            if not all(_clean(t) for t in texts):
                fields.append(MetadataField.binary_field(
                    key, label, payloads if len(payloads) > 1 else payloads[0],
                    deletable=writable and record == 2, source="iptc"))
                continue
            editable, multi = False, len(texts) > 1
        else:
            _, _, repeatable, kind, label = IPTC_MAP[key]
            label = f"IPTC {label}"
            multi = repeatable or len(texts) > 1
            editable = writable and (repeatable or len(texts) == 1)
            if kind == "date" and not all(_IPTC_DATE.fullmatch(t) for t in texts) or \
                    kind == "time" and not all(_IPTC_TIME.fullmatch(t) for t in texts):
                editable = False
        value = texts if multi else texts[0]
        kind = IPTC_MAP[key][3] if key in IPTC_MAP else "str"
        fields.append(MetadataField(key, label, value, raw_value=payloads if multi else payloads[0],
                                    editable=editable, deletable=writable and record == 2,
                                    field_type="text" if kind == "text" else "str",
                                    source="iptc", multi=multi))
    return fields


# ──────────────────────── other formats (read-only) ──────────────────────────

def _png_chunks(data: bytes):
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        return
    pos = 8
    while pos + 12 <= len(data):
        length = int.from_bytes(data[pos:pos + 4], "big")
        ctype = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + length]
        if len(body) != length:
            return
        yield ctype, body
        if ctype == b"IEND":
            return
        pos += 12 + length


_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_MAX_TEXT = 1 << 20


def _inflate(data: bytes) -> bytes | None:
    try:
        d = zlib.decompressobj()
        out = d.decompress(data, _MAX_TEXT)
        return None if d.unconsumed_tail else out
    except zlib.error:
        return None


_PNG_COLOR_TYPES = {0: "Grayscale", 2: "RGB", 3: "Palette", 4: "Grayscale + alpha", 6: "RGB + alpha"}
_PNG_XMP_KEYWORD = "XML:com.adobe.xmp"


@dataclass
class _PngExtras:
    """What a PNG holds besides its text chunks and EXIF."""
    xmp: bytes | None = None
    icc: bytes | None = None
    header: tuple[int, int, int] | None = None     # bit depth, colour type, interlace method
    modified: tuple[int, ...] | None = None        # tIME: year, month, day, hour, minute, second


def _png_metadata(data: bytes, warnings: list[str],
                  extras: "_PngExtras | None" = None) -> tuple[list[tuple[str, str]], bytes | None]:
    """Text chunks (``keyword`` or ``keyword@language``) and the EXIF chunk; the XMP packet,
    the header and the modification time go to ``extras``."""
    texts: list[tuple[str, str]] = []
    exif = None
    extras = extras if extras is not None else _PngExtras()
    for ctype, body in _png_chunks(data):
        if ctype == b"IHDR" and len(body) == 13 and extras.header is None:
            extras.header = (body[8], body[9], body[12])
        elif ctype == b"iCCP" and extras.icc is None and b"\x00" in body:
            _name, _, rest = body.partition(b"\x00")
            extras.icc = _inflate(rest[1:])
        elif ctype == b"tIME" and len(body) == 7 and extras.modified is None:
            extras.modified = (int.from_bytes(body[0:2], "big"), *body[2:7])
        elif ctype == b"tEXt" and b"\x00" in body:
            kw, _, text = body.partition(b"\x00")
            texts.append((kw.decode("latin-1"), text.decode("latin-1")))
        elif ctype == b"zTXt" and b"\x00" in body:
            kw, _, rest = body.partition(b"\x00")
            text = _inflate(rest[1:])
            if text is None:
                warnings.append(f"PNG zTXt {kw.decode('latin-1')!r} not decoded")
                continue
            texts.append((kw.decode("latin-1"), text.decode("latin-1")))
        elif ctype == b"iTXt" and b"\x00" in body:
            kw, _, rest = body.partition(b"\x00")
            if len(rest) < 2:
                continue
            compressed, rest = rest[0], rest[2:]
            lang, _, rest = rest.partition(b"\x00")
            _tkw, _, text = rest.partition(b"\x00")
            if compressed:
                text = _inflate(text)
                if text is None:
                    warnings.append(f"PNG iTXt {kw.decode('latin-1')!r} not decoded")
                    continue
            name = kw.decode("latin-1")
            if name == _PNG_XMP_KEYWORD:
                if extras.xmp is None:
                    extras.xmp = text
                continue
            tag = lang.decode("ascii", "replace").strip()
            texts.append((f"{name}@{tag}" if tag else name, values.decode_text(text, ("utf-8",))))
        elif ctype == b"eXIf" and exif is None:
            exif = body
    return texts, exif


_JFIF_UNITS = {0: "None (aspect ratio)", 1: "Inches", 2: "Centimeters"}
_MPF_TYPES = {0x000000: "Undefined", 0x010001: "Large thumbnail (VGA)", 0x010002: "Large thumbnail (full HD)",
              0x020001: "Multi-frame panorama", 0x020002: "Multi-frame disparity",
              0x020003: "Multi-frame multi-angle", 0x030000: "Baseline MP primary image"}


def _jpeg_structure_fields(state: "_JpegState") -> list[MetadataField]:
    """Read-only JFIF header, comments, MPF index and ICC profile of a JPEG."""
    fields: list[MetadataField] = []

    def add(key, label, value, field_type="str", source="jpeg", **extra):
        fields.append(MetadataField(key, label, value, raw_value=value, editable=False, deletable=False,
                                    field_type=field_type, source=source, **extra))

    segments = state.jpeg.segments
    for seg in segments:
        if seg.is_jfif and seg.payload[:5] == b"JFIF\x00" and len(seg.payload) >= 12:
            p = seg.payload
            add("jfif:Version", "JFIF Version", f"{p[5]}.{p[6]:02d}", source="jfif")
            add("jfif:ResolutionUnit", "JFIF Resolution Unit", _JFIF_UNITS.get(p[7], str(p[7])), source="jfif")
            add("jfif:XResolution", "JFIF X Resolution", int.from_bytes(p[8:10], "big"), "int", source="jfif")
            add("jfif:YResolution", "JFIF Y Resolution", int.from_bytes(p[10:12], "big"), "int", source="jfif")
            break
    comments = [values.decode_text(seg.payload, ("utf-8",)).rstrip("\x00")
                for seg in segments if seg.marker == jpeg_segments.COM and seg.payload is not None]
    if len(comments) == 1:
        add("jpeg:Comment", "JPEG Comment", comments[0], "text" if "\n" in comments[0] else "str")
    elif comments:
        add("jpeg:Comment", "JPEG Comment", comments, "str", multi=True)
    for seg in segments:
        if seg.is_mpf:
            try:
                index = jpeg_segments.parse_mp_index(seg.payload)
            except jpeg_segments.MpfError:
                break
            add("mpf:NumberOfImages", "MPF Number of Images", len(index.entries), "int", source="mpf")
            for number, entry in enumerate(index.entries, start=1):
                kind = entry[0] & 0xFFFFFF
                add(f"mpf:image{number}:Type", f"MPF Image {number} Type", _MPF_TYPES.get(kind, f"0x{kind:06X}"),
                    source="mpf")
                add(f"mpf:image{number}:Size", f"MPF Image {number} Size (bytes)", entry[1], "int",
                    source="mpf", computed=True)
            break
    profile = icc.assemble([s.payload for s in segments if s.is_app(2, b"ICC_PROFILE\x00") and s.payload])
    if profile is not None:
        fields += icc.fields(profile)
    return fields


def _png_structure_fields(extras: "_PngExtras") -> list[MetadataField]:
    """Read-only header and last-modification fields of a PNG."""
    fields: list[MetadataField] = []
    if extras.header is not None:
        depth, color, interlace = extras.header
        fields += [
            MetadataField("img:bit_depth", "Bit Depth", depth, editable=False, deletable=False,
                          field_type="int", source="image"),
            MetadataField("img:color_type", "PNG Color Type", _PNG_COLOR_TYPES.get(color, str(color)),
                          editable=False, deletable=False, source="image"),
            MetadataField("img:interlace", "Interlaced", bool(interlace), editable=False, deletable=False,
                          field_type="bool", source="image"),
        ]
    if extras.modified is not None:
        year, month, day, hour, minute, second = extras.modified
        try:
            stamp = values.format_datetime(datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc))
        except ValueError:
            stamp = None
        if stamp:
            fields.append(MetadataField("pngchunk:tIME", "PNG Last Modification (UTC)", stamp, editable=False,
                                        deletable=False, field_type="datetime", source="png"))
    return fields


def _riff_chunks(data: bytes):
    if data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        return
    pos = 12
    while pos + 8 <= len(data):
        fourcc = data[pos:pos + 4]
        length = int.from_bytes(data[pos + 4:pos + 8], "little")
        body = data[pos + 8:pos + 8 + length]
        if len(body) != length:
            return
        yield fourcc, body
        pos += 8 + length + (length % 2)


def _strip_exif_header(data: bytes) -> bytes:
    return data[6:] if data.startswith(jpeg_segments.EXIF_HEADER) else data


# ──────────────────────── handler ────────────────────────────────────────────

class ImageHandler(BaseMetadataHandler):
    NAME = "image"
    EXTENSIONS = frozenset({
        ".jpg", ".jpeg", ".tif", ".tiff",
        ".png", ".bmp", ".gif", ".webp", ".ico",
        ".cr2", ".nef", ".arw", ".dng", ".orf", ".rw2", ".pef", ".srw",
        ".rwl", ".nrw", ".srf", ".sr2", ".3fr", ".erf", ".mos", ".kdc", ".dcr", ".mef", ".iiq",
        ".cr3", ".raf",
    })
    PRIORITY = 10

    # ── read ──
    def read(self, path: Path) -> MetadataRecord:
        fields: list[MetadataField] = []
        errors: list[str] = []
        warnings: list[str] = []
        pillow_errors: list[str] = []
        fields += self._pillow_fields(path, pillow_errors)
        suffix = path.suffix.lower()
        writable = False
        reason = None
        try:
            head = _read_head(path)
        except OSError as exc:
            errors.append(f"read error: {exc}")
            head = b""
        raw_file = head[:4] in exif_codec.RAW_MAGICS or (suffix in _READ_ONLY_EXTS and bool(pillow_errors))
        if raw_file and pillow_errors:
            # The image library does not know this raw format: its properties are simply not shown.
            warnings.append("Image properties not read: format not supported by the image library")
        else:
            errors += pillow_errors
        try:
            if jpeg_segments.is_jpeg(head):
                writable = _jpeg_name(path)
                more, reason = self._jpeg_fields(path, writable, errors, warnings)
                fields += more
            elif head[:4] in (b"II*\x00", b"MM\x00*") or head[:4] in exif_codec.RAW_MAGICS:
                writable = _container_kind(path) == "tiff"
                fields += self._tiff_fields(path, errors, writable)
                if writable and suffix in _READ_ONLY_EXTS:
                    warnings.append("Raw file: metadata changes are experimental (nothing existing is moved); "
                                    "check the result with your raw software")
            elif head[:8] == b"\x89PNG\r\n\x1a\n":
                data = path.read_bytes()
                extras = _PngExtras()
                texts, exif = _png_metadata(data, warnings, extras)
                fields += _png_structure_fields(extras)
                if extras.icc is not None:
                    fields += icc.fields(extras.icc)
                state = None
                if _container_kind(path) == "png":
                    try:
                        state = _PngState.load(data)
                    except jpeg_segments.JpegFormatError as exc:
                        warnings.append(f"{exc}: metadata is shown but read-only")
                if state is not None:
                    errors += state.errors
                    warnings += state.warnings
                    writable = True
                    fields += self._state_fields(state, writable)
                else:
                    fields += [MetadataField(f"png:{k}", k, v, editable=False, deletable=False,
                                             field_type="text" if "\n" in v else "str", source="png")
                               for k, v in texts]
                    if exif is not None:
                        fields += self._exif_bytes_fields(_strip_exif_header(exif), errors)
                    if extras.xmp is not None:
                        try:
                            fields += _xmp_fields(xmp.XmpPacket(extras.xmp), False)
                        except xmp.XmpFormatError as exc:
                            warnings.append(f"XMP not readable: {exc}")
            elif head[:4] == b"RIFF" and head[8:12] == b"WEBP":
                data = path.read_bytes()
                for fourcc, body in _riff_chunks(data):
                    if fourcc == b"ICCP":
                        fields += icc.fields(body)
                state = None
                if _container_kind(path) == "webp":
                    try:
                        state = _WebpState.load(data)
                    except jpeg_segments.JpegFormatError as exc:
                        warnings.append(f"{exc}: metadata is shown but read-only")
                if state is not None:
                    errors += state.errors
                    warnings += state.warnings
                    reason = state.read_only_reason()
                    if reason:
                        warnings.append(reason)
                    writable = True
                    fields += self._state_fields(state, writable and not reason)
                else:
                    for fourcc, body in _riff_chunks(data):
                        if fourcc == b"EXIF":
                            fields += self._exif_bytes_fields(_strip_exif_header(body), errors)
                        elif fourcc == b"XMP ":
                            try:
                                fields += _xmp_fields(xmp.XmpPacket(body), False)
                            except xmp.XmpFormatError as exc:
                                warnings.append(f"XMP not readable: {exc}")
            elif head[4:12] == b"ftypcrx " or (suffix == ".cr3" and head[4:8] == b"ftyp"):
                fields += _cr3_fields(path, warnings)
            elif head[:15] == b"FUJIFILMCCD-RAW":
                fields += _raf_fields(path, warnings)
            elif suffix in _READ_ONLY_EXTS:
                warnings.append("EXIF of this RAW format is not read")
        except Exception as exc:  # read never raises: unexpected data is a read error, read-only
            errors.append(f"metadata read error: {type(exc).__name__}: {exc}")

        if writable and reason:
            writable = False
        record = self._make_record(path, read_errors=errors, warnings=warnings,
                                   supports_write=writable, supports_delete=writable)
        if reason and _container_kind(path) and not errors:
            record.read_only_reason = reason
        record.fields = fields
        if not writable or errors:
            for f in fields:
                f.editable = f.deletable = False
        for f in fields:
            if f.editable and not self._is_canonical(f):
                f.editable = False
        record.dedupe_keys()
        if writable and not errors:
            try:
                record.addable_fields = self.writable_fields(path, record)
            except Exception as exc:  # the schema is optional: never fail the read for it
                record.warnings.append(f"Addable fields unavailable: {type(exc).__name__}: {exc}")
        return record

    def _pillow_fields(self, path: Path, errors: list[str]) -> list[MetadataField]:
        fields: list[MetadataField] = []
        try:
            from PIL import Image
            with Image.open(path) as img:
                fields += [
                    MetadataField("img:format", "Format", img.format or path.suffix[1:].upper(),
                                  editable=False, deletable=False, source="image"),
                    MetadataField("img:mode", "Color Mode", img.mode,
                                  editable=False, deletable=False, source="image"),
                    MetadataField("img:width", "Width (px)", img.width,
                                  editable=False, deletable=False, field_type="int", source="image"),
                    MetadataField("img:height", "Height (px)", img.height,
                                  editable=False, deletable=False, field_type="int", source="image"),
                ]
                dpi = img.info.get("dpi")
                # JPEG (and MPO, a JPEG): computed from JFIF/EXIF by _jpeg_dpi (Pillow invents
                # 72 when EXIF has no resolution, which would flip on unrelated edits).
                if dpi and img.format not in ("JPEG", "MPO"):
                    # Pillow returns IFDRational when the DPI comes from EXIF (N1): use float().
                    pair = [float(dpi[0]), float(dpi[1])]
                    if all(math.isfinite(v) for v in pair):
                        fields.append(MetadataField("img:dpi", "DPI", pair, editable=False,
                                                    deletable=False, field_type="float",
                                                    source="image", multi=True, length=2))
                if img.format == "GIF" and isinstance(img.info.get("comment"), bytes):
                    fields.append(MetadataField(
                        "gif:comment", "GIF Comment",
                        values.decode_text(img.info["comment"], ("utf-8",)),
                        editable=False, deletable=False, field_type="text", source="gif"))
        except Exception as e:
            errors.append(f"Pillow read error: {e}")
        return fields

    def _jpeg_fields(self, path, writable, errors, warnings) -> tuple[list[MetadataField], str | None]:
        """Fields of a JPEG and, when its metadata must not be rewritten, the reason.

        Read tolerantly: extraneous bytes between segments do not hide the metadata."""
        try:
            state = _JpegState.load(path.read_bytes(), tolerant=True)
        except jpeg_segments.JpegFormatError as exc:
            errors.append(f"JPEG structure error: {exc}")
            return [], None
        errors += state.errors
        warnings += state.warnings
        reason = state.read_only_reason()
        if reason:
            warnings.append(reason)
        return _jpeg_dpi(state) + _jpeg_structure_fields(state) + self._state_fields(state, writable and not reason), reason

    @staticmethod
    def _state_fields(state: _JpegState, writable: bool) -> list[MetadataField]:
        fields: list[MetadataField] = []
        if state.exif is not None:
            fields += _exif_fields(state.exif, writable and state.exif_writable)
        if state.xmp is not None:
            fields += _xmp_fields(state.xmp, writable and state.xmp_writable)
        if state.iim is not None:
            fields += _iptc_fields(state, writable and state.iptc_writable)
        if isinstance(state, _PngState):
            fields += _png_text_fields(state.png, writable)
        if isinstance(state, _TiffState):
            fields += _tiff_iptc_block(state.buf, writable)
            _tiff_deletable(fields, writable)
        return fields

    def _tiff_fields(self, path: Path, errors: list[str], writable: bool = False) -> list[MetadataField]:
        try:
            with open(path, "rb") as fh, mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ) as mm:
                block = exif_codec.parse(mm, with_ifd1=False, raw=True)
        except (exif_codec.ExifFormatError, OSError, ValueError) as exc:
            errors.append(f"EXIF read error: {exc}")
            return []
        fields = _exif_fields(block, writable)
        entries = block.ifds["0th"]
        if 34675 in entries:                      # embedded ICC profile
            fields += icc.fields(entries[34675].data)
        if 700 in entries:                        # XMP packet (Lightroom, many editors)
            try:
                fields += _xmp_fields(xmp.XmpPacket(entries[700].data), writable)
            except xmp.XmpFormatError:
                pass                              # the raw packet stays visible as its binary field
        if 33723 in entries:                      # IPTC-NAA (IIM) record
            if writable:
                fields += _tiff_iptc_block(Path(path).read_bytes(), True)
            try:
                datasets, _ = iptc.parse_iim(entries[33723].data)
                utf8 = iptc.charset_marker(datasets) in (None, iptc.UTF8_MARKER)
                datasets_fields = _iptc_fields(SimpleNamespace(iim=datasets, iptc_utf8=utf8), False)
                for f in datasets_fields:
                    f.computed = True              # they disappear with the record
                fields += datasets_fields
            except iptc.IptcFormatError:
                pass
        if writable:
            _tiff_deletable(fields, True)
        return fields

    def _exif_bytes_fields(self, data: bytes, errors: list[str]) -> list[MetadataField]:
        try:
            return _exif_fields(exif_codec.parse(data), False)
        except exif_codec.ExifFormatError as exc:
            errors.append(f"EXIF read error: {exc}")
            return []

    def _is_canonical(self, f: MetadataField) -> bool:
        try:
            return _same(self.canonical(f, copy.deepcopy(f.value)), f.value)
        except MetadataValueError:
            return False

    def content_digest(self, path: Path) -> str | None:
        """SHA-256 of the picture data: JPEG scan data, PNG image chunks, WebP bitstream chunks."""
        import hashlib
        data = Path(path).read_bytes()
        digest = hashlib.sha256()
        if jpeg_segments.is_jpeg(data[:4]):
            try:
                digest.update(_JpegState.load(data, tolerant=True).jpeg.tail)
            except jpeg_segments.JpegFormatError:
                return None
        elif data[:8] == _PNG_SIGNATURE:
            for ctype, body in _png_chunks(data):
                if ctype in (b"IHDR", b"PLTE", b"tRNS", b"IDAT", b"acTL", b"fcTL", b"fdAT"):
                    digest.update(ctype + body)
        elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            for fourcc, body in _riff_chunks(data):
                if fourcc in (b"VP8 ", b"VP8L", b"ALPH", b"ANMF"):
                    digest.update(fourcc + body)
        elif data[:4] in (b"II*\x00", b"MM\x00*") or data[:4] in exif_codec.RAW_MAGICS:
            return tiff_inplace.media_digest(data)
        else:
            return None
        return digest.hexdigest()

    # ── schema ──
    def writable_fields(self, path: Path, record: MetadataRecord) -> list[MetadataField]:
        if not _container_kind(path) or record.read_errors:
            return []
        try:
            state = _state_for(Path(path).read_bytes())
        except (jpeg_segments.JpegFormatError, OSError):
            return []
        if state.errors or state.read_only_reason():
            return []
        addable = self._addable(state, {f.key for f in record.fields})
        return self._tiff_addable(addable) if isinstance(state, _TiffState) else addable

    @staticmethod
    def _tiff_addable(addable: list[MetadataField]) -> list[MetadataField]:
        """Of the metadata that can be added, what a TIFF-structured block may take."""
        return [f for f in addable if f.key.startswith("xmp:") or
                ((where := exif_codec.parse_key(f.key)) is not None and tiff_inplace.editable(*where))]

    def _addable(self, state: _JpegState, present: set[str]) -> list[MetadataField]:
        out: list[MetadataField] = []
        if state.exif_writable:
            for ifd, tag in exif_codec.ADDABLE:
                key = exif_codec.key_for(ifd, tag)
                if key in present or (state.exif is not None and state.exif.entry(ifd, tag)):
                    continue
                f = _new_exif_schema(ifd, tag)
                if f is not None:
                    out.append(f)
        if state.xmp_writable:
            # A mapped property present in the packet is offered only as an EMPTY array
            # (e.g. ``<dc:subject><rdf:Bag/></dc:subject>``, hidden because it holds no
            # value): writing fills it in place. Any other shape is preserved, not offered.
            blocked = set()
            if state.xmp is not None:
                for key, (uri, local) in _xmp_key_map(state.xmp).items():
                    spec = XMP_MAP.get(key)
                    if spec is None or not _xmp_empty_array(state.xmp, uri, local, spec[2]):
                        blocked.add(key)
            for key, (uri, local, form, kind, label) in XMP_MAP.items():
                if key in present or key in blocked:
                    continue
                out.append(MetadataField(key, f"XMP {label}", None, source="xmp",
                                         field_type={"rating": "int", "text": "text",
                                                     "date": "datetime"}.get(kind, "str"),
                                         multi=form in ("bag", "seq")))
        if state.iptc_writable:
            existing = {(d.record, d.number) for d in state.iim or []}
            for key, (record, number, repeatable, kind, label) in IPTC_MAP.items():
                # IIM dates/times have their own syntax: editable once present, not offered.
                if key in present or (record, number) in existing or kind in ("date", "time"):
                    continue
                out.append(MetadataField(key, f"IPTC {label}", None, source="iptc",
                                         field_type="text" if kind == "text" else "str",
                                         multi=repeatable))
        return out

    def writable_field(self, path: Path, key: str) -> MetadataField | None:
        record = self.read(path)
        for f in self.writable_fields(path, record):
            if f.key == key:
                return f
        return None

    def _schema(self, state: _JpegState) -> dict[str, MetadataField]:
        current = {f.key: f for f in self._state_fields(state, True)}
        for f in self._addable(state, set(current)):
            current.setdefault(f.key, f)
        return current

    # ── canonical values ──
    def canonical(self, field: MetadataField, value: Any) -> Any:
        key = field.key
        if key.startswith(("exif:", "gps:")):
            return _canonical_exif(field, value)
        if key.startswith("xmp:"):
            result = values.canonicalize(field, value)
            spec = XMP_MAP.get(key)
            for item in (result if isinstance(result, list) else [result]):
                if isinstance(item, str) and xmp.invalid_xml_char(item) is not None:
                    raise MetadataValueError(key, ErrorCode.INVALID_VALUE,
                                             f"{key}: character not allowed in XML")
                if spec and spec[3] == "date" and field.field_type == "str" and not _XMP_DATE.fullmatch(item):
                    raise MetadataValueError(key, ErrorCode.INVALID_VALUE,
                                             f"{key}: not an XMP date (YYYY[-MM[-DD[Thh:mm[:ss][TZD]]]])")
                if spec and spec[3] == "rating" and not -1 <= item <= 5:
                    raise MetadataValueError(key, ErrorCode.OUT_OF_RANGE, f"{key}: rating outside -1..5")
            return result
        if key.startswith("iptc:"):
            result = values.canonicalize(field, value)
            spec = IPTC_MAP.get(key)
            for item in (result if isinstance(result, list) else [result]):
                if isinstance(item, str):
                    try:
                        size = len(item.encode("utf-8"))
                    except UnicodeEncodeError:
                        raise MetadataValueError(key, ErrorCode.INVALID_VALUE,
                                                 f"{key}: text is not encodable") from None
                    if size > iptc.MAX_DATASET:
                        raise MetadataValueError(key, ErrorCode.TOO_LARGE,
                                                 f"{key}: {size} bytes, IPTC limit {iptc.MAX_DATASET}")
                if spec and spec[3] == "date" and not _IPTC_DATE.fullmatch(item):
                    raise MetadataValueError(key, ErrorCode.INVALID_VALUE, f"{key}: IPTC dates are YYYYMMDD")
                if spec and spec[3] == "time" and not _IPTC_TIME.fullmatch(item):
                    raise MetadataValueError(key, ErrorCode.INVALID_VALUE,
                                             f"{key}: IPTC times are HHMMSS±HHMM")
            return result
        return values.canonicalize(field, value)

    # ── mutation hooks ──
    def linked_changes(self, fields, writes, deletes):
        """An edit of an IPTC dataset or XMP property is mirrored to its twin when the
        twin is already in the file (nothing is created, nothing the user asked for
        is overridden)."""
        extra_writes: dict[str, Any] = {}
        extra_deletes: list[str] = []
        for key, partner in _LINK_PARTNER.items():
            twin = fields.get(partner)
            if twin is None or partner in writes or partner in deletes:
                continue
            source = fields.get(key)
            if key in writes:
                if source is not None and _same(source.value, writes[key]):
                    continue                       # not a change: the twin is left alone (G1)
                if (twin.editable and twin.field_type in ("str", "text")
                        and bool(twin.multi) == isinstance(writes[key], list)
                        and (source is None or source.field_type in ("str", "text"))):
                    extra_writes[partner] = copy.deepcopy(writes[key])
            elif key in deletes and twin.deletable:
                extra_deletes.append(partner)
        return extra_writes, extra_deletes

    def apply_write(self, tmp: Path, fields: list[MetadataField]) -> None:
        state = self._load_for_mutation(tmp)
        schema = self._schema(state)
        errors: list[MetadataValueError] = []
        planned: list[tuple[MetadataField, Any]] = []
        for f in fields:
            target = schema.get(f.key)
            try:
                if target is None:
                    raise MetadataValueError(f.key, ErrorCode.UNKNOWN_FIELD)
                if not target.editable:
                    raise MetadataValueError(f.key, ErrorCode.READ_ONLY)
                planned.append((target, self.canonical(target, copy.deepcopy(f.value))))
            except MetadataValueError as exc:
                errors.append(exc)
        _raise_all(errors)
        size_keys: dict[str, tuple[int, str]] = {}
        for target, value in planned:
            if target.value is not None and _same(target.value, value):
                # No-op: the field already holds this canonical value. Its bytes stay as
                # they are (e.g. "Canon" stored as b"Canon\0\0\0") and, when nothing else
                # changes, the file is not rewritten, so the snapshot is unchanged and the
                # API returns no Undo token.
                continue
            try:
                ns, weight = self._set(state, target, value)
            except MetadataValueError as exc:
                errors.append(exc)
                continue
            if weight >= size_keys.get(ns, (-1, ""))[0]:
                size_keys[ns] = (weight, target.key)
        _raise_all(errors)
        self._save(tmp, state, {ns: k for ns, (_, k) in size_keys.items()})
        after = {f.key: f for f in self._state_fields(self._load_for_mutation(tmp), True)}
        for target, value in planned:
            got = after.get(target.key)
            if got is None or not _same(got.value, value):
                raise MetadataValueError(target.key, ErrorCode.LOSSY_WRITE,
                                         f"{target.key}: value not stored exactly")

    def apply_delete(self, tmp: Path, keys: list[str]) -> None:
        state = self._load_for_mutation(tmp)
        current = {f.key: f for f in self._state_fields(state, True)}
        errors = []
        for key in keys:
            f = current.get(key)
            if f is None:
                errors.append(MetadataValueError(key, ErrorCode.UNKNOWN_FIELD))
            elif not f.deletable:
                errors.append(MetadataValueError(key, ErrorCode.NOT_DELETABLE))
        _raise_all(errors)
        for key in keys:
            self._put_raw(state, key, ABSENT)
        self._save(tmp, state, {})
        after = {f.key for f in self._state_fields(self._load_for_mutation(tmp), True)}
        still = [k for k in keys if k in after]
        if still:
            raise MetadataValueError(still[0], ErrorCode.LOSSY_WRITE, f"{still[0]}: not removed")

    def snapshot(self, tmp: Path, keys: list[str]) -> Snapshot:
        state = self._load_for_mutation(tmp)
        return {key: self._raw(state, key) for key in keys}

    def restore(self, tmp: Path, snap: Snapshot) -> None:
        state = self._load_for_mutation(tmp)
        groups: dict[str, list] = {"exif": [], "xmp": [], "iptc": [], "png": []}
        for key, raw in snap.items():
            groups[_namespace(key)].append((key, raw))
        for key, raw in groups["exif"]:
            self._put_raw(state, key, raw)
        if groups["xmp"]:
            self._restore_xmp(state, groups["xmp"])
        if isinstance(state, _TiffState):
            for key, raw in groups["iptc"]:
                self._put_raw(state, key, raw)                 # the whole IPTC record
        elif groups["iptc"]:
            self._restore_iptc(state, groups["iptc"])
        for key, raw in groups["png"]:
            self._put_raw(state, key, raw)
        for ns, items in groups.items():
            carrier = next((raw for _, raw in items if isinstance(raw, _SegmentRaw) and raw.segment is not None),
                           None)
            if carrier is None:
                continue                 # only added keys: no original segment to compare with
            if carrier.index is not None:
                state.hints[ns] = carrier.index
            if ns not in state.touched:
                continue
            if state.matches(ns, carrier.segment):
                state.exact[ns] = carrier.segment      # same content: put the original bytes back
            elif ns == "exif":
                self._pin_restored_makernote(state, carrier.segment)
        self._save(tmp, state, {})

    @staticmethod
    def _pin_restored_makernote(state: _JpegState, original: bytes) -> None:
        """A MakerNote brought back without its original segment returns to the offset it
        had there (vendor offsets inside it are absolute in Canon-style MakerNotes)."""
        block = state.exif
        if block is None or exif_codec.MAKERNOTE[1] not in block.ifds["Exif"] or exif_codec.MAKERNOTE in block.pins:
            return
        try:
            pin = exif_codec.parse(original).pins.get(exif_codec.MAKERNOTE)
        except exif_codec.ExifFormatError:
            return
        if pin is not None:
            block.pins[exif_codec.MAKERNOTE] = pin

    def _restore_xmp(self, state: _JpegState, items) -> None:
        if not state.xmp_writable:
            raise MetadataValueError(items[0][0], ErrorCode.READ_ONLY)
        batch = []
        for key, raw in items:
            raw = _unwrap(raw)
            if key == XMP_HIDDEN_KEY:
                if raw is ABSENT:
                    if state.xmp is not None:
                        for uri, local in _xmp_hidden(state.xmp):
                            batch.append((uri, local, None))
                else:
                    batch.extend((uri, local, occurrences) for uri, local, occurrences in raw[3])
            elif raw is ABSENT:
                name = self._xmp_name(state, key)
                if name is not None:
                    batch.append((*name, None))
            else:
                batch.append((raw[1], raw[2], raw[3]))
        if not batch:
            return
        if state.xmp is None:
            if all(occ is None for _, _, occ in batch):
                return
            state.xmp = xmp.XmpPacket.new()
        state.xmp.put_raw_many(batch)
        state.touched.add("xmp")

    def _restore_iptc(self, state: _JpegState, items) -> None:
        if not state.iptc_writable:
            raise MetadataValueError(items[0][0], ErrorCode.READ_ONLY)
        items = [(key, _unwrap(raw)) for key, raw in items]
        if state.iim is None:
            if all(raw is ABSENT for _, raw in items):
                return
            state.iim = []
        inserts = []
        for key, raw in items:
            record, number = _iptc_where(key)
            iptc.remove(state.iim, record, number)
            if raw is not ABSENT:
                inserts += [(record, index, number, data) for index, data in raw[1]]
        for record, index, number, data in sorted(inserts):
            iptc.insert_at(state.iim, iptc.Dataset(record, number, data), index)
        state.touched.add("iptc")

    def write(self, path: Path, fields: list[MetadataField]) -> None:
        if not _container_kind(path):
            raise NotImplementedError(f"Metadata write not supported for {path.suffix}")
        self._atomic_write(path, lambda tmp: self.apply_write(tmp, fields))

    def delete(self, path: Path, keys: list[str]) -> None:
        if not _container_kind(path):
            raise NotImplementedError(f"Metadata delete not supported for {path.suffix}")
        self._atomic_write(path, lambda tmp: self.apply_delete(tmp, keys))

    # ── internals ──
    @staticmethod
    def _load_for_mutation(path: Path) -> _JpegState:
        try:
            state = _state_for(Path(path).read_bytes())
        except jpeg_segments.JpegFormatError as exc:
            raise MetadataValueError(None, ErrorCode.UNSUPPORTED, str(exc)) from exc
        if state.errors:
            raise MetadataValueError(None, ErrorCode.UNSUPPORTED, "; ".join(state.errors))
        reason = state.read_only_reason()
        if reason:
            raise MetadataValueError(None, ErrorCode.UNSUPPORTED, reason)
        return state

    @staticmethod
    def _save(tmp: Path, state: _JpegState, size_keys: dict[str, str]) -> None:
        if not state.touched:
            return
        data = state.to_bytes(size_keys)
        with open(tmp, "r+b") as fh:
            fh.write(data)
            fh.truncate()

    def _set(self, state: _JpegState, target: MetadataField, value: Any) -> tuple[str, int]:
        key = target.key
        if key.startswith(("exif:", "gps:")):
            if not state.exif_writable:
                raise MetadataValueError(key, ErrorCode.READ_ONLY)
            ifd, tag = exif_codec.parse_key(key)
            if state.exif is None:
                state.exif = exif_codec.ExifBlock.empty()
            block = state.exif
            entry = block.entry(ifd, tag)
            if entry is not None:
                dec = exif_codec.decode(ifd, tag, entry, block.byteorder)
                typ, kind = entry.type, dec.kind
                count = entry.count if dec.multi else None
            else:
                typ, kind = exif_codec.kind_for_new(ifd, tag)
                count = None
            new = exif_codec.encode(key, tag, typ, kind, value, block.byteorder, count)
            block.ifds[ifd][tag] = new
            state.touched.add("exif")
            return "exif", len(new.data)
        if key.startswith("xmp:"):
            if not state.xmp_writable:
                raise MetadataValueError(key, ErrorCode.READ_ONLY)
            uri, local, form, kind, _ = XMP_MAP[key]
            if state.xmp is None:
                state.xmp = xmp.XmpPacket.new()
            text = str(value) if kind == "rating" else value
            state.xmp.write_value(uri, local, xmp.KNOWN_PREFIXES[uri], form, text)
            state.touched.add("xmp")
            return "xmp", len(repr(value))
        if key.startswith("iptc:"):
            if not state.iptc_writable:
                raise MetadataValueError(key, ErrorCode.READ_ONLY)
            record, number, _, _, _ = IPTC_MAP[key]
            items = value if isinstance(value, list) else [value]
            payloads = [item.encode("utf-8") for item in items]
            if state.iim is None:
                state.iim = []
            if any(not p.isascii() for p in payloads) and iptc.charset_marker(state.iim) is None:
                iptc.insert_at(state.iim, iptc.Dataset(*iptc.CHARSET, iptc.UTF8_MARKER), None)
            iptc.set_values(state.iim, record, number, payloads)
            state.touched.add("iptc")
            return "iptc", sum(len(p) for p in payloads)
        raise MetadataValueError(key, ErrorCode.UNKNOWN_FIELD)

    def _raw(self, state: _JpegState, key: str) -> Any:
        """Snapshot of ``key``: ABSENT, or its raw pre-image carrying the exact bytes of
        its segment (for the byte-exact restore, see ``restore``)."""
        raw = self._raw_value(state, key)
        if raw is ABSENT:
            return ABSENT
        payload, index = state.segment(_namespace(key))
        return _SegmentRaw(raw, payload, index)

    def _raw_value(self, state: _JpegState, key: str) -> Any:
        if isinstance(state, _TiffState) and (key.startswith(("exif:", "gps:")) or key == IPTC_BLOCK_KEY):
            where = ("0th", 33723) if key == IPTC_BLOCK_KEY else exif_codec.parse_key(key)
            saved = tiff_inplace.snapshot(state.buf, *where) if where else None
            return ("tiff", saved) if saved is not None else ABSENT
        if key.startswith(("exif:", "gps:")):
            where = exif_codec.parse_key(key)
            if where is None:
                raise MetadataValueError(key, ErrorCode.UNKNOWN_FIELD)
            entry = state.exif.entry(*where) if state.exif is not None else None
            if entry is None:
                return ABSENT
            return ("exif", state.exif.byteorder, entry.tag, entry.type, entry.count, entry.data)
        if key == XMP_HIDDEN_KEY:
            hidden = _xmp_hidden(state.xmp) if state.xmp is not None else []
            if not hidden:
                return ABSENT
            kept = tuple((uri, local, state.xmp.raw(uri, local)) for uri, local in hidden)
            return _PositionedRaw(("xmp", XMP_HIDDEN_KEY, None, kept), ("xmp-hidden", tuple(hidden)))
        if key.startswith("xmp:"):
            name = self._xmp_name(state, key)
            raw = state.xmp.raw(*name) if state.xmp is not None and name else None
            if raw is None:
                return ABSENT
            # kind, prefix and value / standalone XML of each occurrence; positions are only a hint
            content = ("xmp", name[0], name[1],
                       tuple((o[0], o[3], o[4]) if o[0] == "attr" else (o[0], o[3]) for o in raw))
            return _PositionedRaw(("xmp", name[0], name[1], raw), content)
        if key.startswith("iptc:"):
            where = _iptc_where(key)
            occ = iptc.occurrences(state.iim or [], *where)
            if not occ:
                return ABSENT
            return _PositionedRaw(("iptc", tuple(occ)), ("iptc", tuple(data for _, data in occ)))
        if key.startswith("png:") and isinstance(state, _PngState):
            found = [(i, bytes(c[0]), bytes(c[1])) for i, c in enumerate(state.png.chunks)
                     if _png_text_key(c) == key]
            return ("png", tuple(found)) if found else ABSENT
        raise MetadataValueError(key, ErrorCode.UNKNOWN_FIELD)

    def _put_raw(self, state: _JpegState, key: str, raw: Any) -> None:
        raw = _unwrap(raw)
        if isinstance(state, _TiffState) and (key.startswith(("exif:", "gps:")) or key == IPTC_BLOCK_KEY):
            where = ("0th", 33723) if key == IPTC_BLOCK_KEY else exif_codec.parse_key(key)
            if where is None or not (tiff_inplace.editable(*where) or where == ("0th", 33723)):
                raise MetadataValueError(key, ErrorCode.NOT_DELETABLE)
            try:
                if raw is ABSENT:
                    if tiff_inplace.remove(state.buf, where[0], {where[1]}, force=True):
                        state.touched.add("tiff")
                else:
                    tiff_inplace.put_entry(state.buf, raw[1])
                    state.touched.add("tiff")
            except tiff_inplace.TiffError as exc:
                raise MetadataValueError(key, ErrorCode.LOSSY_WRITE, f"{key}: {exc}") from exc
            return
        if key.startswith(("exif:", "gps:")):
            if not state.exif_writable:
                raise MetadataValueError(key, ErrorCode.READ_ONLY)
            ifd, tag = exif_codec.parse_key(key)
            if raw is ABSENT:
                if state.exif is not None and state.exif.ifds[ifd].pop(tag, None) is not None:
                    state.touched.add("exif")
                return
            _, bo, rtag, typ, count, data = raw
            if state.exif is None:
                state.exif = exif_codec.ExifBlock(bo)
            entry = exif_codec.swap_entry(exif_codec.Entry(rtag, typ, count, data), bo, state.exif.byteorder)
            state.exif.ifds[ifd][tag] = entry
            state.touched.add("exif")
            return
        if key == XMP_HIDDEN_KEY:
            if not state.xmp_writable:
                raise MetadataValueError(key, ErrorCode.READ_ONLY)
            if raw is ABSENT:
                if state.xmp is not None:
                    for uri, local in _xmp_hidden(state.xmp):
                        state.xmp.remove(uri, local)
                    state.touched.add("xmp")
                return
            self._restore_xmp(state, [(key, raw)])
            return
        if key.startswith("xmp:"):
            if not state.xmp_writable:
                raise MetadataValueError(key, ErrorCode.READ_ONLY)
            name = self._xmp_name(state, key)
            if raw is ABSENT:
                if state.xmp is not None and name and state.xmp.remove(*name):
                    state.touched.add("xmp")
                return
            self._restore_xmp(state, [(key, raw)])
            return
        if key.startswith("iptc:"):
            if not state.iptc_writable:
                raise MetadataValueError(key, ErrorCode.READ_ONLY)
            record, number = _iptc_where(key)
            if state.iim is None:
                if raw is ABSENT:
                    return
                state.iim = []
            removed = iptc.remove(state.iim, record, number)
            if raw is not ABSENT:
                for index, data in sorted(raw[1]):
                    iptc.insert_at(state.iim, iptc.Dataset(record, number, data), index)
            if removed is not None or raw is not ABSENT:
                state.touched.add("iptc")
            return
        if key.startswith("png:") and isinstance(state, _PngState):
            wanted = [i for i, c in enumerate(state.png.chunks) if _png_text_key(c) == key]
            for i in reversed(wanted):
                del state.png.chunks[i]
            if raw is not ABSENT:
                state.png.put_text([(index, [kind, body]) for index, kind, body in raw[1]])
            if wanted or raw is not ABSENT:
                state.touched.add("png")
            return
        raise MetadataValueError(key, ErrorCode.UNKNOWN_FIELD)

    @staticmethod
    def _xmp_name(state: _JpegState, key: str) -> tuple[str, str] | None:
        if key in XMP_MAP:
            return XMP_MAP[key][:2]
        if state.xmp is not None:
            return _xmp_key_map(state.xmp).get(key)
        return None


def _iptc_where(key: str) -> tuple[int, int]:
    if key in IPTC_MAP:
        return IPTC_MAP[key][:2]
    if key == "iptc:ApplicationRecordVersion":
        return _IPTC_RECORD_VERSION
    m = _IPTC_KEY.fullmatch(key)
    if m is None:
        raise MetadataValueError(key, ErrorCode.UNKNOWN_FIELD)
    return int(m.group(1)), int(m.group(2))


def _new_exif_schema(ifd: str, tag: int) -> MetadataField | None:
    spec = exif_codec.kind_for_new(ifd, tag)
    if spec is None:
        return None
    _, kind = spec
    key = exif_codec.key_for(ifd, tag)
    label = exif_codec.label_for(key, ifd, tag)
    choices = exif_codec.CHOICES.get((ifd, tag))
    field_type = {"datetime": "localdatetime", "int": "int", "rational": "rational"}.get(kind, "str")
    if choices:
        field_type = "enum"
    triple = (ifd, tag) in (("GPS", 2), ("GPS", 4))                 # degrees, minutes, seconds
    return MetadataField(key, label, None, source="gps" if ifd == "GPS" else "exif",
                         field_type=field_type, choices=list(choices or []),
                         multi=triple, length=3 if triple else None)


def _canonical_exif(field: MetadataField, value: Any) -> Any:
    key = field.key
    where = exif_codec.parse_key(key)
    if field.field_type == "rational":
        signed = where is None or exif_codec.spec_type(*where) in (None, 10)
        return _canonical_items(field, value, lambda v: values.canonical_rational(v, signed=signed))
    result = values.canonicalize(field, value)
    for item in (result if isinstance(result, list) else [result]):
        if isinstance(item, str) and item.endswith("\x00"):
            raise MetadataValueError(key, ErrorCode.INVALID_VALUE, f"{key}: text must not end with NUL")
        if field.field_type == "localdatetime":
            dt = values.parse_localdatetime(item)
            if dt.microsecond:
                raise MetadataValueError(key, ErrorCode.INVALID_VALUE,
                                         f"{key}: EXIF date-times have no fractional seconds")
            if dt.year < 1000:
                raise MetadataValueError(key, ErrorCode.OUT_OF_RANGE, f"{key}: EXIF dates need a 4-digit year")
        if field.field_type == "int" and where is not None:
            typ = exif_codec.spec_type(*where)
            low, high = exif_codec._INT_RANGES.get(typ, (None, None))
            if low is not None and not low <= item <= high:
                raise MetadataValueError(key, ErrorCode.OUT_OF_RANGE, f"{key}: {item} outside [{low}, {high}]")
    return result


def _canonical_items(field: MetadataField, value: Any, item_fn) -> Any:
    try:
        if value is None:
            raise MetadataValueError(None, ErrorCode.NULL_VALUE, "null is not a valid value")
        if not (field.multi or field.length is not None):
            return item_fn(value)
        if not isinstance(value, (list, tuple)):
            raise MetadataValueError(None, ErrorCode.WRONG_TYPE, f"a list is required, got {value!r}")
        if field.length is not None and len(value) != field.length:
            raise MetadataValueError(None, ErrorCode.WRONG_LENGTH,
                                     f"{len(value)} items, exactly {field.length} required")
        if not value:
            raise MetadataValueError(None, ErrorCode.EMPTY_VALUE, "empty list; delete the field instead")
        return [item_fn(v) for v in value]
    except MetadataValueError as exc:
        raise exc.with_key(field.key) from exc


def _same(a: Any, b: Any) -> bool:
    return type(a) is type(b) and a == b if not isinstance(a, list) else \
        isinstance(b, list) and len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b))


def _raise_all(errors: list[MetadataValueError]) -> None:
    if len(errors) == 1:
        raise errors[0]
    if errors:
        raise MetadataValueErrors(errors)


def _read_head(path: Path) -> bytes:
    with open(path, "rb") as fh:
        return fh.read(16)


# ──────────────────────── legacy helpers ─────────────────────────────────────

def _decode_exif_value(raw: Any) -> Any:  # pragma: no cover - kept for importers
    if isinstance(raw, bytes):
        return values.decode_text(raw.rstrip(b"\x00"), ("utf-8",))
    if isinstance(raw, tuple) and len(raw) == 2 and isinstance(raw[0], int):
        return values.format_rational(*raw)
    if isinstance(raw, (list, tuple)):
        return [_decode_exif_value(v) for v in raw]
    return raw


def _encode_exif_value(value: Any, tag_type: int) -> Any:
    """Validate ``value`` for a TIFF type (legacy API; raises ``MetadataValueError``)."""
    kind = {2: "ascii", 5: "rational", 10: "rational"}.get(tag_type, "int" if tag_type in (1, 3, 4, 6, 8, 9) else None)
    if kind is None:
        raise MetadataValueError(None, ErrorCode.READ_ONLY, "This EXIF data type is read-only")
    if kind == "int" and (isinstance(value, bool) or not isinstance(value, (int, str))):
        raise MetadataValueError(None, ErrorCode.WRONG_TYPE, "EXIF numeric tag requires an integer")
    if kind == "int":
        value = values.parse_int(value)
    if kind == "ascii" and not isinstance(value, str):
        raise MetadataValueError(None, ErrorCode.WRONG_TYPE, "EXIF text must be a string")
    return exif_codec.encode("exif", 0, tag_type, kind, value, ">").data
