"""Edit only OOXML core properties; preserve macros and all other package parts."""
from __future__ import annotations

from copy import copy
from datetime import datetime, timezone
from pathlib import Path
import shutil
import tempfile
import xml.etree.ElementTree as ET
from zipfile import ZipFile

from core.base_handler import BaseMetadataHandler
from core.models import MetadataField, MetadataRecord

_NS = {
    "cp": "http://schemas.openxmlformats.org/package/2006/metadata/core-properties",
    "dc": "http://purl.org/dc/elements/1.1/",
    "dcterms": "http://purl.org/dc/terms/",
    "xsi": "http://www.w3.org/2001/XMLSchema-instance",
}
for _prefix, _uri in _NS.items():
    ET.register_namespace(_prefix, _uri)

_PROPS = {
    "title": ("dc:title", "Title"),
    "author": ("dc:creator", "Author"),
    "subject": ("dc:subject", "Subject"),
    "keywords": ("cp:keywords", "Keywords"),
    "description": ("dc:description", "Description"),
    "last_modified_by": ("cp:lastModifiedBy", "Last Modified By"),
    "revision": ("cp:revision", "Revision"),
    "category": ("cp:category", "Category"),
    "content_status": ("cp:contentStatus", "Content Status"),
    "identifier": ("dc:identifier", "Identifier"),
    "language": ("dc:language", "Language"),
    "version": ("cp:version", "Version"),
    "created": ("dcterms:created", "Created"),
    "modified": ("dcterms:modified", "Modified"),
}
_CORE_PART = "docProps/core.xml"


def _qname(name: str) -> str:
    prefix, local = name.split(":", 1)
    return "{" + _NS[prefix] + "}" + local


def _load_core(archive: ZipFile):
    entries = [item for item in archive.infolist() if item.filename == _CORE_PART]
    if len(entries) != 1 or entries[0].file_size > 1024 * 1024:
        raise ValueError("Missing, duplicated or oversized core-properties XML")
    raw = archive.read(entries[0])
    if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
        raise ValueError("DTD/entity declarations are not supported")
    root = ET.fromstring(raw)
    if root.tag != _qname("cp:coreProperties"):
        raise ValueError("Invalid core-properties root")
    return root


def _property_name(key: str) -> str:
    if not key.startswith("office:") or key[7:] not in _PROPS:
        raise ValueError(f"Unsupported Office property: {key}")
    return key[7:]


def _property_value(name: str, value) -> str:
    if name in {"created", "modified"}:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    if name == "revision":
        if isinstance(value, bool) or not str(value).isdigit() or int(value) < 1:
            raise ValueError("Revision must be a positive integer")
        return str(int(value))
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    if any(ord(c) < 32 and c not in "\t\n\r" for c in value):
        raise ValueError("Office property contains invalid XML characters")
    return value


class OfficeHandler(BaseMetadataHandler):
    NAME = "office"
    EXTENSIONS = frozenset({".docx", ".xlsx", ".pptx", ".docm", ".xlsm", ".pptm"})
    PRIORITY = 15

    def writable_field(self, path: Path, key: str) -> MetadataField | None:
        name = key[7:] if key.startswith("office:") else ""
        if name in _PROPS:
            return MetadataField(key, _PROPS[name][1], "", source="office")
        return None

    def read(self, path: Path) -> MetadataRecord:
        fields = []
        errors = []
        writable = False
        try:
            with ZipFile(path) as archive:
                root = _load_core(archive)
                writable = not any(i.filename.startswith("_xmlsignatures/") for i in archive.infolist())
            for name, (tag, label) in _PROPS.items():
                node = root.find(_qname(tag))
                if node is not None:
                    fields.append(MetadataField(
                        f"office:{name}", label, node.text or "", source="office",
                        field_type="datetime" if name in {"created", "modified"} else ("int" if name == "revision" else "str"),
                        editable=writable, deletable=writable,
                    ))
        except Exception as exc:
            errors.append(f"Office read error: {exc}")
        record = self._make_record(path, read_errors=errors,
                                   supports_write=writable, supports_delete=writable)
        record.fields = fields
        return record

    def write(self, path: Path, fields: list[MetadataField]) -> None:
        updates = {_property_name(f.key): _property_value(_property_name(f.key), f.value) for f in fields}
        self._atomic_write(path, lambda tmp: _rewrite_properties(tmp, updates, set()))

    def delete(self, path: Path, keys: list[str]) -> None:
        names = {_property_name(key) for key in keys}
        self._atomic_write(path, lambda tmp: _rewrite_properties(tmp, {}, names))


def _rewrite_properties(path: Path, updates: dict[str, str], removed: set[str]) -> None:
    # Stream untouched entries instead of rebuilding via an Office library,
    # which can discard VBA, custom XML, shapes and vendor extensions.
    with ZipFile(path) as source:
        if any(i.filename.startswith("_xmlsignatures/") for i in source.infolist()):
            raise ValueError("Editing digitally signed Office packages is not supported")
        root = _load_core(source)
        for name in removed | updates.keys():
            tag = _qname(_PROPS[name][0])
            for node in list(root):
                if node.tag == tag:
                    root.remove(node)
            if name in updates:
                node = ET.SubElement(root, tag)
                node.text = updates[name]
                if name in {"created", "modified"}:
                    node.set(_qname("xsi:type"), "dcterms:W3CDTF")
        raw = ET.tostring(root, encoding="utf-8", xml_declaration=True)
        with tempfile.TemporaryFile(dir=path.parent) as output:
            with ZipFile(output, "w") as destination:
                destination.comment = source.comment
                for info in source.infolist():
                    if info.filename == _CORE_PART:
                        destination.writestr(copy(info), raw)
                    else:
                        with source.open(info) as reader, destination.open(copy(info), "w") as writer:
                            shutil.copyfileobj(reader, writer)
            source.close()
            output.seek(0)
            with path.open("wb") as target:
                shutil.copyfileobj(output, target)
