"""EPUB books: read-only metadata from the OPF package document."""
from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path
from zipfile import BadZipFile, ZipFile

from core import values
from core.base_handler import BaseMetadataHandler
from core.handlers.xml_package import Mutations
from core.models import MetadataField, MetadataRecord
from core.zipxml import PackageError, local, read_xml

NS_CONTAINER = "{urn:oasis:names:tc:opendocument:xmlns:container}"
NS_DC = "http://purl.org/dc/elements/1.1/"

_DC = {"title": "Title", "creator": "Author", "contributor": "Contributor", "publisher": "Publisher",
       "language": "Language", "identifier": "Identifier", "subject": "Subject", "description": "Description",
       "rights": "Rights", "date": "Publication Date", "type": "Type", "source": "Source", "relation": "Relation",
       "coverage": "Coverage", "format": "Format"}
_MULTI = {"title", "creator", "contributor", "subject", "identifier", "language"}


def _field(key: str, label: str, value, field_type: str = "str", multi: bool = False) -> MetadataField:
    return MetadataField(f"epub:{key}", label, value, raw_value=value, editable=False, deletable=False,
                         field_type=field_type, source="epub", multi=multi)


def _opf_path(archive: ZipFile) -> str:
    container = read_xml(archive, "META-INF/container.xml")
    for node in container.iter(f"{NS_CONTAINER}rootfile"):
        path = node.get("full-path")
        if path and ".." not in path.split("/") and not path.startswith("/"):
            return path
    raise PackageError("container.xml names no package document")


def opf_fields(root) -> list[MetadataField]:
    fields: list[MetadataField] = []
    version = root.get("version")
    if version:
        fields.append(_field("version", "EPUB Version", version))
    metadata = next((c for c in root if local(c.tag) == "metadata"), None)
    if metadata is None:
        return fields
    collected: dict[str, list[str]] = {}
    modified = None
    for node in metadata:
        text = (node.text or "").strip()
        if node.tag.startswith("{" + NS_DC + "}") and text:
            collected.setdefault(local(node.tag), []).append(text)
        elif local(node.tag) == "meta" and node.get("property") == "dcterms:modified" and text:
            modified = text
    for name, label in _DC.items():
        if name in collected:
            many = name in _MULTI or len(collected[name]) > 1
            fields.append(_field(name, label, collected[name] if many else collected[name][0], "str", many))
    if modified:
        try:
            fields.append(_field("modified", "Last Modified",
                                 values.format_datetime(values.parse_datetime(modified)), "datetime"))
        except Exception:
            fields.append(_field("modified", "Last Modified", modified))
    return fields


_REQUIRED = {"version", "identifier", "title", "language"}        # a book without them is not valid


def _metadata_node(root):
    return next((c for c in root if local(c.tag) == "metadata"), None)


class EpubHandler(Mutations, BaseMetadataHandler):
    NAME = "epub"
    EXTENSIONS = frozenset({".epub"})
    PRIORITY = 17

    # ── where a key lives in the package document ──
    def part_name(self, archive) -> str:
        return _opf_path(archive)

    def removable(self, key: str) -> bool:
        return key[len("epub:"):] not in _REQUIRED

    def locate(self, root, key: str) -> list[tuple]:
        meta = _metadata_node(root)
        short = key[len("epub:"):]
        hits = []
        for node in (meta if meta is not None else []):
            if short == "modified" and local(node.tag) == "meta" and node.get("property") == "dcterms:modified":
                hits.append((meta, node, None))
            elif node.tag == "{" + NS_DC + "}" + short:
                hits.append((meta, node, None))
        return hits

    def make(self, root, key: str, value) -> list:
        meta = _metadata_node(root)
        short = key[len("epub:"):]
        if meta is None or short == "version":
            return []
        out = []
        for v in (value if isinstance(value, list) else [value]):
            if short == "modified":
                element = ET.Element("{http://www.idpf.org/2007/opf}meta", {"property": "dcterms:modified"})
            elif short in _DC:
                element = ET.Element("{%s}%s" % (NS_DC, short))
            else:
                return []
            element.text = str(v)
            out.append((meta, element))
        return out

    def read(self, path: Path) -> MetadataRecord:
        fields: list[MetadataField] = []
        errors: list[str] = []
        warnings: list[str] = []
        try:
            with ZipFile(path) as archive:
                fields = opf_fields(read_xml(archive, _opf_path(archive)))
        except (BadZipFile, PackageError, OSError) as exc:
            errors.append(f"EPUB read error: {exc}")
        writable = not errors
        for f in fields:
            short = f.key[len("epub:"):]
            f.deletable = writable and short not in _REQUIRED
            f.editable = writable and short != "version" and f.field_type == "str"
        record = self._make_record(path, read_errors=errors, warnings=warnings,
                                   supports_write=writable, supports_delete=writable)
        record.fields = fields
        record.dedupe_keys()
        return record

    def writable_fields(self, path: Path, record) -> list[MetadataField]:
        if not record.format_supports_write or record.read_errors:
            return []
        present = {f.key for f in record.fields}
        return [MetadataField(f"epub:{name}", label, None, source="epub", multi=name in _MULTI)
                for name, label in _DC.items() if f"epub:{name}" not in present and name not in _REQUIRED]

    def write(self, path: Path, fields: list[MetadataField]) -> None:
        self._atomic_write(path, lambda tmp: self.apply_write(tmp, fields))

    def delete(self, path: Path, keys: list[str]) -> None:
        self._atomic_write(path, lambda tmp: self.apply_delete(tmp, keys))
