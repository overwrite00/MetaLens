"""Format recognition from the content of a file (magic numbers).

Handlers pick files by extension. A file without an extension (``photo``) or
with a wrong one would then fall to the read-only generic reader, so the
registry asks this module which format the bytes are. Only the first bytes are
read (plus the member names of a ZIP package), never the whole file.

``detect`` returns a stable format id (``jpeg``, ``png``, ``mp4``...) or ``None``
and ``HANDLER_BY_FORMAT`` maps it to the ``NAME`` of the handler that owns it.
"""
from __future__ import annotations

import zipfile
from pathlib import Path

HEAD_BYTES = 64

# Brands (ISO/IEC 14496-12 ``ftyp`` major brand) -> format id.
_HEIF_BRANDS = {b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"mif1", b"msf1"}
_AVIF_BRANDS = {b"avif", b"avis"}
_AUDIO_BRANDS = {b"M4A ", b"M4B ", b"M4P "}
_ASF_GUID = bytes.fromhex("3026b2758e66cf11a6d900aa0062ce6c")

HANDLER_BY_FORMAT: dict[str, str] = {
    "jpeg": "image", "png": "image", "gif": "image", "tiff": "image", "webp": "image", "bmp": "image",
    "mp4": "video", "mov": "video", "mkv": "video", "avi": "video",
    "m4a": "audio", "mp3": "audio", "flac": "audio", "ogg": "audio", "wav": "audio", "aiff": "audio",
    "asf": "audio", "ape": "audio", "wavpack": "audio", "musepack": "audio",
    "heif": "heif", "avif": "heif",
    "odf": "odf", "epub": "epub",
    "pdf": "pdf", "ooxml": "office", "ole": "ole",
}


def _read_head(path: Path) -> bytes:
    try:
        with open(path, "rb") as stream:
            return stream.read(HEAD_BYTES)
    except OSError:
        return b""


def _mpeg_frame_sync(head: bytes) -> bool:
    """A plausible MPEG audio frame header (11 sync bits, valid version and layer)."""
    if len(head) < 4 or head[0] != 0xFF or head[1] & 0xE0 != 0xE0:
        return False
    version, layer = (head[1] >> 3) & 3, (head[1] >> 1) & 3
    bitrate, rate = head[2] >> 4, (head[2] >> 2) & 3
    return version != 1 and layer != 0 and bitrate not in (0, 15) and rate != 3


def _zip_kind(path: Path) -> str | None:
    try:
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
    except (OSError, zipfile.BadZipFile, ValueError):
        return None
    if "[Content_Types].xml" in names:
        return "ooxml"
    if names and names[0] == "mimetype":
        try:
            with zipfile.ZipFile(path) as z:
                kind = z.read("mimetype")[:100].decode("ascii", "replace").strip()
        except (OSError, zipfile.BadZipFile, ValueError, KeyError):
            return None
        if kind == "application/epub+zip":
            return "epub"
        if kind.startswith("application/vnd.oasis.opendocument."):
            return "odf"
    return None


def detect(path: Path, head: bytes | None = None) -> str | None:
    """Format id of the file at ``path`` or ``None`` when the content is not recognized."""
    path = Path(path)
    if head is None:
        head = _read_head(path)
    if len(head) < 4:
        return None
    if head[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if head[:4] in (b"II*\x00", b"MM\x00*"):
        return "tiff"
    if head[:2] == b"BM" and len(head) >= 14 and head[6:10] == b"\x00\x00\x00\x00":
        return "bmp"
    if head[:4] == b"RIFF" and len(head) >= 12:
        return {b"WEBP": "webp", b"AVI ": "avi", b"WAVE": "wav"}.get(head[8:12])
    if head[:4] == b"FORM" and len(head) >= 12 and head[8:12] in (b"AIFF", b"AIFC"):
        return "aiff"
    if head[4:8] == b"ftyp" and len(head) >= 12:
        brand = head[8:12]
        if brand in _HEIF_BRANDS:
            return "heif"
        if brand in _AVIF_BRANDS:
            return "avif"
        if brand == b"crx ":
            return "cr3"
        if brand in _AUDIO_BRANDS:
            return "m4a"
        if brand == b"qt  ":
            return "mov"
        return "mp4"
    if head[:4] == b"\x1aE\xdf\xa3":
        return "mkv"
    if head[:4] == b"fLaC":
        return "flac"
    if head[:4] == b"OggS":
        return "ogg"
    if head[:16] == _ASF_GUID:
        return "asf"
    if head[:4] == b"MAC ":
        return "ape"
    if head[:4] == b"wvpk":
        return "wavpack"
    if head[:4] in (b"MPCK", b"MP+\x07"):
        return "musepack"
    if head[:5] == b"%PDF-":
        return "pdf"
    if head[:8] == bytes.fromhex("d0cf11e0a1b11ae1"):
        return "ole"
    if head[:4] == b"PK\x03\x04":
        return _zip_kind(path)
    if head[:3] == b"ID3" or _mpeg_frame_sync(head):
        return "mp3"
    return None


def handler_name(format_id: str | None) -> str | None:
    """``NAME`` of the handler owning ``format_id`` (``None`` when no handler does)."""
    return HANDLER_BY_FORMAT.get(format_id) if format_id else None
