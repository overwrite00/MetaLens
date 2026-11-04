"""Safe reading of XML parts of ZIP packages (ODF, EPUB): size cap, no DTD, no duplicated names."""
from __future__ import annotations

import xml.etree.ElementTree as ET
from zipfile import ZipFile

MAX_PART_BYTES = 2 * 1024 * 1024


class PackageError(ValueError):
    """The package or one of its parts cannot be read safely."""


def read_xml(archive: ZipFile, name: str, required: bool = True):
    """Root element of the part ``name``; ``None`` when it is absent and not ``required``."""
    entries = [i for i in archive.infolist() if i.filename == name]
    if not entries:
        if required:
            raise PackageError(f"{name} is missing")
        return None
    if len(entries) != 1:
        raise PackageError(f"{name} appears more than once")
    if entries[0].file_size > MAX_PART_BYTES:
        raise PackageError(f"{name} is too large")
    raw = archive.read(entries[0])
    if b"<!DOCTYPE" in raw[:4096].upper() or b"<!ENTITY" in raw.upper():
        raise PackageError(f"{name} declares a DTD")
    try:
        return ET.fromstring(raw)
    except ET.ParseError as exc:
        raise PackageError(f"{name} is not valid XML ({exc})") from exc


def local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]
