"""OpenDocument files (.odt .ods .odp .odg and templates): read-only metadata from ``meta.xml``."""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path
from zipfile import BadZipFile, ZipFile

from core import values
from core.base_handler import BaseMetadataHandler
from core.handlers.xml_package import Mutations
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


_EDITABLE = {"title", "subject", "description", "creator", "initial-creator", "language", "generator", "keyword",
             "creation-date", "date", "print-date", "editing-cycles"}


def _meta_node(root):
    return next((c for c in root if local(c.tag) == "meta"), None)


def _name_of(node) -> tuple[str, str]:
    return node.tag[1:].partition("}")[::2] if node.tag.startswith("{") else ("", node.tag)


class OdfHandler(Mutations, BaseMetadataHandler):
    NAME = "odf"
    EXTENSIONS = frozenset({".odt", ".ods", ".odp", ".odg", ".odf", ".ott", ".ots", ".otp", ".otg"})
    PRIORITY = 16

    # ── where a key lives in meta.xml ──
    def part_name(self, archive) -> str:
        return "meta.xml"

    def locate(self, root, key: str) -> list[tuple]:
        meta = _meta_node(root)
        short = key[len("odf:"):] if key.startswith("odf:") else key
        hits = []
        for node in (meta if meta is not None else []):
            ident = _name_of(node)
            if ident in _TEXT and _TEXT[ident][0] == short or ident in _DATES and _DATES[ident][0] == short \
                    or ident == (NS_META, short) and short in ("editing-cycles", "editing-duration"):
                hits.append((meta, node, None))
            elif ident == (NS_META, "document-statistic") and short.startswith("stat."):
                attribute = next((a for a in node.attrib if a.rsplit("}", 1)[-1] == short[5:]), None)
                if attribute is not None:
                    hits.append((meta, node, attribute))
            elif ident == (NS_META, "user-defined") and short.startswith("custom.") \
                    and next((v for k, v in node.attrib.items() if local(k) == "name"), None) == short[7:]:
                hits.append((meta, node, None))
        return hits

    def make(self, root, key: str, value) -> list:
        meta = _meta_node(root)
        short = key[len("odf:"):]
        if meta is None:
            return []
        values_ = value if isinstance(value, list) else [value]
        if short.startswith("custom."):
            elements = [ET.Element("{%s}user-defined" % NS_META, {"{%s}name" % NS_META: short[7:]})]
            elements[0].text = str(values_[0])
            return [(meta, e) for e in elements]
        for (uri, name), (text_key, _label, _multi) in _TEXT.items():
            if text_key == short:
                made = []
                for v in values_:
                    element = ET.Element("{%s}%s" % (uri, name))
                    element.text = str(v)
                    made.append((meta, element))
                return made
        for (uri, name), (date_key, _label) in _DATES.items():
            if date_key == short:
                element = ET.Element("{%s}%s" % (uri, name))
                element.text = str(values_[0])
                return [(meta, element)]
        if short == "editing-cycles":
            element = ET.Element("{%s}editing-cycles" % NS_META)
            element.text = str(values_[0])
            return [(meta, element)]
        return []

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
        signed = any("signed" in w for w in warnings)
        writable = not errors and not signed
        for f in fields:
            short = f.key[len("odf:"):]
            f.deletable = writable
            f.editable = writable and (short in _EDITABLE or (short.startswith("custom.") and f.field_type == "str"))
        record = self._make_record(path, read_errors=errors, warnings=warnings,
                                   supports_write=writable, supports_delete=writable)
        record.fields = fields
        if not writable and not errors:
            record.read_only_reason = "Digitally signed document: editing would invalidate the signature"
        record.dedupe_keys()
        return record

    def writable_fields(self, path: Path, record) -> list[MetadataField]:
        if not record.format_supports_write or record.read_errors:
            return []
        present = {f.key for f in record.fields}
        return [MetadataField(f"odf:{key}", label, None, source="odf",
                              field_type="localdatetime" if key in ("creation-date", "date", "print-date") else
                              "int" if key == "editing-cycles" else "str", multi=multi)
                for (_uri, _name), (key, label, multi) in _TEXT.items() if f"odf:{key}" not in present]

    def canonical(self, field: MetadataField, value):
        from core import values
        result = values.canonicalize(field, value)
        for item in (result if isinstance(result, list) else [result]):
            if isinstance(item, str) and "\x00" in item:
                from core.errors import ErrorCode, MetadataValueError
                raise MetadataValueError(field.key, ErrorCode.INVALID_VALUE, f"{field.key}: NUL is not allowed")
        return result

    def write(self, path: Path, fields: list[MetadataField]) -> None:
        self._atomic_write(path, lambda tmp: self.apply_write(tmp, fields))

    def delete(self, path: Path, keys: list[str]) -> None:
        self._atomic_write(path, lambda tmp: self.apply_delete(tmp, keys))
