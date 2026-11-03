"""Read-only view of an ICC colour profile (header and the description / copyright tags).

Written from the public ICC specification (ICC.1:2022, sections 7.2 and 10). The
profile is parsed defensively: anything unexpected yields fewer fields, never an
exception. Profile bytes are never modified by MetaLens.
"""
from __future__ import annotations

import struct
from datetime import datetime, timezone

from core import values
from core.models import MetadataField

ICC_SIGNATURE = b"acsp"
HEADER_SIZE = 128
MAX_TAGS = 200

_CLASSES = {b"scnr": "Input device", b"mntr": "Display device", b"prtr": "Output device",
            b"link": "Device link", b"abst": "Abstract", b"spac": "Color space conversion",
            b"nmcl": "Named color"}
_INTENTS = {0: "Perceptual", 1: "Relative colorimetric", 2: "Saturation", 3: "Absolute colorimetric"}
_PLATFORMS = {b"APPL": "Apple", b"MSFT": "Microsoft", b"SGI ": "Silicon Graphics", b"SUNW": "Sun", b"TGNT": "Taligent"}


def _text(raw: bytes) -> str:
    return values.decode_text(raw.rstrip(b"\x00 "), ("ascii",)).strip()


def _tag_text(profile: bytes, offset: int, size: int) -> str | None:
    """Text of a ``desc`` / ``text`` / ``mluc`` tag element, ``None`` if it is not one of them."""
    if offset + 8 > len(profile) or size < 8:
        return None
    element = profile[offset:offset + size]
    kind = element[:4]
    if kind == b"desc" and len(element) >= 12:
        count = struct.unpack_from(">I", element, 8)[0]
        return _text(element[12:12 + count]) or None
    if kind == b"text":
        return _text(element[8:]) or None
    if kind == b"mluc" and len(element) >= 16:
        number, record = struct.unpack_from(">II", element, 8)
        best = None
        for i in range(min(number, 64) if record >= 12 else 0):
            at = 16 + record * i
            if at + 12 > len(element):
                break
            length, start = struct.unpack_from(">II", element, at + 4)
            chunk = element[start:start + length]
            if len(chunk) != length:
                continue
            text = chunk.decode("utf-16-be", errors="replace").rstrip("\x00").strip()
            if text and (best is None or element[at:at + 2] == b"en"):
                best = text
        return best
    return None


def assemble(segments: list[bytes]) -> bytes | None:
    """Profile bytes from the payloads of JPEG ``ICC_PROFILE`` APP2 segments (``None`` if incomplete)."""
    parts: dict[int, bytes] = {}
    total = 0
    for payload in segments:
        if not payload.startswith(b"ICC_PROFILE\x00") or len(payload) < 14:
            return None
        number, total = payload[12], payload[13]
        parts[number] = payload[14:]
    if not parts or sorted(parts) != list(range(1, total + 1)):
        return None
    return b"".join(parts[i] for i in range(1, total + 1))


def fields(profile: bytes) -> list[MetadataField]:
    """Read-only ``icc:`` fields of ``profile`` (empty when it is not a valid ICC profile)."""
    if len(profile) < HEADER_SIZE or profile[36:40] != ICC_SIGNATURE:
        return []
    out: list[MetadataField] = []

    def add(key: str, label: str, value, field_type: str = "str") -> None:
        out.append(MetadataField(key, label, value, raw_value=value, editable=False, deletable=False,
                                 field_type=field_type, source="icc"))

    major, minor = profile[8], profile[9] >> 4
    add("icc:Version", "ICC Profile Version", f"{major}.{minor}.{profile[9] & 15}")
    add("icc:ProfileClass", "ICC Profile Class", _CLASSES.get(profile[12:16], _text(profile[12:16])))
    add("icc:ColorSpace", "ICC Color Space", _text(profile[16:20]))
    add("icc:ConnectionSpace", "ICC Connection Space", _text(profile[20:24]))
    year, month, day, hour, minute, second = struct.unpack_from(">6H", profile, 24)
    try:
        add("icc:CreationDate", "ICC Profile Date",
            values.format_datetime(datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)), "datetime")
    except ValueError:
        pass
    platform = profile[40:44]
    if platform.strip(b"\x00"):
        add("icc:Platform", "ICC Primary Platform", _PLATFORMS.get(platform, _text(platform)))
    add("icc:RenderingIntent", "ICC Rendering Intent",
        _INTENTS.get(struct.unpack_from(">I", profile, 64)[0] & 0xFFFF, "Unknown"))
    if len(profile) < HEADER_SIZE + 4:
        return out
    count = min(struct.unpack_from(">I", profile, HEADER_SIZE)[0], MAX_TAGS)
    for i in range(count):
        at = HEADER_SIZE + 4 + 12 * i
        if at + 12 > len(profile):
            break
        signature, offset, size = struct.unpack_from(">4sII", profile, at)
        key, label = {b"desc": ("icc:Description", "ICC Profile Description"),
                      b"cprt": ("icc:Copyright", "ICC Profile Copyright")}.get(signature, (None, None))
        if key and not any(f.key == key for f in out):
            text = _tag_text(profile, offset, size)
            if text:
                add(key, label, text)
    return out
