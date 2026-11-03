"""OpenDocument files (.odt .ods .odp .odg and templates): read-only metadata from ``meta.xml``."""
from __future__ import annotations

import re
from pathlib import Path
from zipfile import BadZipFile, ZipFile

from core import values
from core.base_handler import BaseMetadataHandler
from core.models import MetadataField, MetadataRecord
from core.zipxml import PackageError, local, read_xml

NS_DC = "http://purl.org/dc/elements/1.1/"
NS_META = "urn:oasis:names:tc:opendocument:xmlns:meta:1.0"

_TEXT = {  # (namespace, local name) -> (key, label, multi)
    (NS_DC, "title"): ("title", "Title", False),
    (NS_DC, "subject"): ("subject", "Subject", False),
    (NS_DC, "description"): ("description", "Description", False),
    (NS_DC, "creator"): ("creator", "Last Modified By", False),
    (NS_DC, "language"): ("language", "Language", False),
    (NS_META, "initial-creator"): ("initial-creator", "Author", False),
    (NS_META, "generator"): ("generator", "Generator", False),
    (NS_META, "keyword"): ("keyword", "Keywords", True),
}
_DATES = {(NS_DC, "date"): ("date", "Modified"), (NS_META, "creation-date"): ("creation-date", "Created"),
          (NS_META, "print-date"): ("print-date", "Last Printed")}
_DURATION = re.compile(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?", re.ASCII)


def _field(key: str, label: str, value, field_type: str = "str", multi: bool = False) -> MetadataField:
    return MetadataField(f"odf:{key}", label, value, raw_value=value, editable=False, deletable=False,
                         field_type=field_type, source="odf", multi=multi)


def _date_field(key: str, label: str, text: str) -> MetadataField:
    for kind, parse in (("datetime", values.parse_datetime), ("localdatetime", values.parse_localdatetime)):
        try:
            return _field(key, label, values.format_datetime(parse(text)), kind)
        except Exception:
            continue
    return _field(key, label, text)


def _duration(text: str) -> float | None:
    m = _DURATION.fullmatch(text.strip())
    if not m or not any(m.groups()):
        return None
    days, hours, minutes, seconds = (float(g) if g else 0.0 for g in m.groups())
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


def meta_fields(root, warnings: list[str]) -> list[MetadataField]:
    meta = next((c for c in root if local(c.tag) == "meta"), None)
    if meta is None:
        return []
    fields: list[MetadataField] = []
    texts: dict[str, list[str]] = {}
    for node in meta:
        uri, _, name = node.tag[1:].partition("}") if node.tag.startswith("{") else ("", "", node.tag)
        text = (node.text or "").strip()
        ident = (uri, name)
        if ident in _TEXT and text:
            texts.setdefault(_TEXT[ident][0], []).append(text)
        elif ident in _DATES and text:
            fields.append(_date_field(*_DATES[ident], text))
        elif ident == (NS_META, "editing-cycles") and text.isdigit():
            fields.append(_field("editing-cycles", "Revision", int(text), "int"))
        elif ident == (NS_META, "editing-duration"):
            seconds = _duration(text)
            if seconds is not None:
                fields.append(_field("editing-duration", "Total Editing Time", seconds, "duration"))
        elif ident == (NS_META, "document-statistic"):
            for attr, value in sorted(node.attrib.items()):
                short = attr.rsplit("}", 1)[-1]
                if value.isdigit():
                    fields.append(_field(f"stat.{short}", f"Statistic {short}", int(value), "int"))
        elif ident == (NS_META, "user-defined"):
            label = next((v for k, v in node.attrib.items() if local(k) == "name"), "")
            if label:
                fields.append(_field(f"custom.{label}", f"Custom {label}", text))
    ordered = []
    for (uri, name), (key, label, multi) in _TEXT.items():
        if key in texts:
            ordered.append(_field(key, label, texts[key] if multi else texts[key][0], "str", multi))
    return ordered + fields


class OdfHandler(BaseMetadataHandler):
    NAME = "odf"
    EXTENSIONS = frozenset({".odt", ".ods", ".odp", ".odg", ".odf", ".ott", ".ots", ".otp", ".otg"})
    PRIORITY = 16

    def read(self, path: Path) -> MetadataRecord:
        fields: list[MetadataField] = []
        errors: list[str] = []
        warnings: list[str] = []
        try:
            with ZipFile(path) as archive:
                root = read_xml(archive, "meta.xml", required=False)
                if root is not None:
                    fields = meta_fields(root, warnings)
                else:
                    warnings.append("The document has no meta.xml")
                if any(n.startswith("META-INF/") and "signatures" in n for n in archive.namelist()):
                    warnings.append("Digitally signed document")
        except (BadZipFile, PackageError, OSError) as exc:
            errors.append(f"ODF read error: {exc}")
        record = self._make_record(path, read_errors=errors, warnings=warnings,
                                   supports_write=False, supports_delete=False)
        record.fields = fields
        record.read_only_reason = "Metadata of OpenDocument files is read-only for now"
        record.dedupe_keys()
        return record

    def write(self, path: Path, fields: list[MetadataField]) -> None:
        raise NotImplementedError("OpenDocument metadata is read-only for now")

    def delete(self, path: Path, keys: list[str]) -> None:
        raise NotImplementedError("OpenDocument metadata is read-only for now")
