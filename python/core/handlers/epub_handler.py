"""EPUB books: read-only metadata from the OPF package document."""
from __future__ import annotations

from pathlib import Path
from zipfile import BadZipFile, ZipFile

from core import values
from core.base_handler import BaseMetadataHandler
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


class EpubHandler(BaseMetadataHandler):
    NAME = "epub"
    EXTENSIONS = frozenset({".epub"})
    PRIORITY = 17

    def read(self, path: Path) -> MetadataRecord:
        fields: list[MetadataField] = []
        errors: list[str] = []
        warnings: list[str] = []
        try:
            with ZipFile(path) as archive:
                fields = opf_fields(read_xml(archive, _opf_path(archive)))
        except (BadZipFile, PackageError, OSError) as exc:
            errors.append(f"EPUB read error: {exc}")
        record = self._make_record(path, read_errors=errors, warnings=warnings,
                                   supports_write=False, supports_delete=False)
        record.fields = fields
        record.read_only_reason = "Metadata of EPUB books is read-only for now"
        record.dedupe_keys()
        return record

    def write(self, path: Path, fields: list[MetadataField]) -> None:
        raise NotImplementedError("EPUB metadata is read-only for now")

    def delete(self, path: Path, keys: list[str]) -> None:
        raise NotImplementedError("EPUB metadata is read-only for now")
