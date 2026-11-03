"""OOXML packages: edit only docProps/core.xml; preserve macros and all other parts.

* Core properties are typed: ``created``/``modified`` = ``datetime`` (W3CDTF with
  offset; a value without offset is refused, decision D7), ``revision`` = ``int``
  (>= 1), the rest ``str``. Values in the file that do not fit are shown
  read-only (still deletable) with a warning.
* docProps/app.xml (``office:app.<Name>``) and docProps/custom.xml
  (``office:custom.<name>``) are read-only (decision D16).
* Digitally signed packages (``_xmlsignatures/``) are read-only.
* ``snapshot`` keeps the serialized core-properties nodes of each key, so
  ``restore`` puts back the exact nodes (attributes included).
"""
from __future__ import annotations

from copy import copy, deepcopy
from datetime import datetime
from pathlib import Path
import math
import re
import shutil
import tempfile
from typing import Any
import xml.etree.ElementTree as ET
from zipfile import ZipFile

from core import values
from core.base_handler import ABSENT, BaseMetadataHandler, Snapshot
from core.errors import ErrorCode, MetadataValueError, MetadataValueErrors
from core.models import MetadataField, MetadataRecord
from core.xmp import invalid_xml_char

_NS = {
    "cp": "http://schemas.openxmlformats.org/package/2006/metadata/core-properties",
    "dc": "http://purl.org/dc/elements/1.1/",
    "dcterms": "http://purl.org/dc/terms/",
    "dcmitype": "http://purl.org/dc/dcmitype/",
    "xsi": "http://www.w3.org/2001/XMLSchema-instance",
}
for _prefix, _uri in _NS.items():
    ET.register_namespace(_prefix, _uri)

_APP_NS = "http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"
_CUSTOM_NS = "http://schemas.openxmlformats.org/officeDocument/2006/custom-properties"
_VT_NS = "http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes"

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
_DATES = frozenset({"created", "modified"})
_CORE_PART = "docProps/core.xml"
_APP_PART = "docProps/app.xml"
_CUSTOM_PART = "docProps/custom.xml"
_MAX_PART = 1024 * 1024

_APP_INT = frozenset({"TotalTime", "Pages", "Words", "Characters", "CharactersWithSpaces", "Lines",
                      "Paragraphs", "DocSecurity", "Slides", "Notes", "HiddenSlides", "MMClips"})
_APP_BOOL = frozenset({"ScaleCrop", "LinksUpToDate", "SharedDoc", "HyperlinksChanged"})
_VT_INT = frozenset({"i1", "i2", "i4", "i8", "int", "ui1", "ui2", "ui4", "ui8", "uint"})
_VT_FLOAT = frozenset({"r4", "r8", "decimal"})
_VT_DATE = frozenset({"filetime", "date"})

SIGNED_REASON = "Digitally signed Office package: editing would invalidate the signature"


def _qname(name: str) -> str:
    prefix, local = name.split(":", 1)
    return "{" + _NS[prefix] + "}" + local


def _local(tag: str) -> tuple[str, str]:
    if tag.startswith("{"):
        ns, local = tag[1:].split("}", 1)
        return ns, local
    return "", tag


def _parse_part(archive: ZipFile, name: str, required: bool):
    entries = [item for item in archive.infolist() if item.filename == name]
    if not entries:
        if required:
            raise ValueError(f"Missing {name}")
        return None
    if len(entries) != 1 or entries[0].file_size > _MAX_PART:
        raise ValueError(f"Duplicated or oversized {name}")
    raw = archive.read(entries[0])
    if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
        raise ValueError("DTD/entity declarations are not supported")
    return ET.fromstring(raw)


def _load_core(archive: ZipFile):
    root = _parse_part(archive, _CORE_PART, required=True)
    if root.tag != _qname("cp:coreProperties"):
        raise ValueError("Invalid core-properties root")
    return root


def _is_signed(archive: ZipFile) -> bool:
    return any(i.filename.lower().startswith("_xmlsignatures/") for i in archive.infolist())


def _property_name(key: str) -> str:
    if not key.startswith("office:") or key[7:] not in _PROPS:
        raise MetadataValueError(key, ErrorCode.UNKNOWN_FIELD, f"{key}: unsupported Office property")
    return key[7:]


def _schema(name: str, value: Any = None) -> MetadataField:
    ftype = "datetime" if name in _DATES else "int" if name == "revision" else "str"
    return MetadataField(f"office:{name}", _PROPS[name][1], value, field_type=ftype, source="office")


def _nodes(root, name: str) -> list:
    tag = _qname(_PROPS[name][0])
    return [node for node in root if node.tag == tag]


def _core_field(name: str, node, warnings: list[str] | None) -> MetadataField:
    key, label = f"office:{name}", _PROPS[name][1]
    text = node.text or ""

    def ro(value, ftype):
        if warnings is not None:
            warnings.append(f"{key}: value {text!r} is not a valid {_schema(name).field_type}; shown read-only")
        return MetadataField(key, label, value, raw_value=text, editable=False, deletable=True,
                             field_type=ftype, source="office")

    if name in _DATES:
        try:
            dt = values.parse_datetime(text)
        except MetadataValueError:
            try:
                return ro(values.format_datetime(values.parse_localdatetime(text)), "localdatetime")
            except MetadataValueError:
                return ro(text, "str")
        return MetadataField(key, label, values.format_datetime(dt), raw_value=text,
                             field_type="datetime", source="office")
    if name == "revision":
        stripped = text.strip()
        if not (stripped.isascii() and stripped.isdigit() and int(stripped) >= 1):
            return ro(text, "str")
        return MetadataField(key, label, int(stripped), raw_value=text, field_type="int", source="office")
    return MetadataField(key, label, text, raw_value=text, field_type="str", source="office")


def _encode(name: str, value: Any) -> str:
    if name in _DATES:
        text = datetime.fromisoformat(value).isoformat()
        return text[:-6] + "Z" if text.endswith("+00:00") else text
    return str(value)


def _node_bytes(node) -> bytes:
    clone = deepcopy(node)
    clone.tail = None
    return ET.tostring(clone, encoding="utf-8")


def _ro(key: str, label: str, value: Any, ftype: str, multi: bool = False) -> MetadataField:
    return MetadataField(key, label, value, raw_value=value, editable=False, deletable=False,
                         field_type=ftype, source="office", multi=multi)


def _app_fields(root) -> list[MetadataField]:
    fields = []
    for child in root:
        ns, local = _local(child.tag)
        if ns != _APP_NS:
            continue
        key, label = f"office:app.{local}", f"{local} (application)"
        if local == "TitlesOfParts":
            items = [(e.text or "") for e in child.iter() if _local(e.tag) == (_VT_NS, "lpstr")
                     or _local(e.tag) == (_VT_NS, "lpwstr")]
            if items:
                fields.append(_ro(key, label, items, "str", multi=True))
            continue
        if len(child):
            continue  # other structured values (HeadingPairs, HLinks, DigSig...)
        text = child.text or ""
        if local in _APP_INT and re.fullmatch(r"-?[0-9]+", text.strip(), re.ASCII):
            fields.append(_ro(key, label, int(text.strip()), "int"))
        elif local in _APP_BOOL and text.strip().lower() in ("true", "false", "1", "0"):
            fields.append(_ro(key, label, text.strip().lower() in ("true", "1"), "bool"))
        else:
            fields.append(_ro(key, label, text, "str"))
    return fields


def _custom_value(node) -> tuple[Any, str]:
    _ns, vt = _local(node.tag)
    text = node.text or ""
    try:
        if vt in _VT_INT:
            return values.parse_int(text.strip()), "int"
        if vt in _VT_FLOAT:
            number = values.parse_float(text.strip())
            if math.isfinite(number):
                return number, "float"
        if vt == "bool":
            return values.parse_bool(text), "bool"
        if vt in _VT_DATE:
            return values.format_datetime(values.parse_datetime(text)), "datetime"
    except MetadataValueError:
        return text, "str"   # not of the declared variant type: keep the raw text
    return text, "str"


def _custom_fields(root) -> list[MetadataField]:
    fields = []
    for prop in root:
        if prop.tag != "{" + _CUSTOM_NS + "}property" or not prop.get("name"):
            continue
        children = [c for c in prop if _local(c.tag)[0] == _VT_NS]
        if not children or len(children[0]):
            continue  # vectors / blobs are not shown
        value, ftype = _custom_value(children[0])
        name = prop.get("name")
        fields.append(_ro(f"office:custom.{name}", f"{name} (custom)", value, ftype))
    return fields


def _raise_collected(errors: list[MetadataValueError]) -> None:
    if len(errors) == 1:
        raise errors[0]
    if errors:
        raise MetadataValueErrors(errors)


def _attributed(exc: MetadataValueError, key: str) -> MetadataValueError:
    return exc if exc.key else exc.with_key(key)


def _open_core(path: Path):
    with ZipFile(path) as archive:
        if _is_signed(archive):
            raise MetadataValueError(None, ErrorCode.READ_ONLY,
                                     "Editing digitally signed Office packages is not supported")
        return _load_core(archive)


class OfficeHandler(BaseMetadataHandler):
    NAME = "office"
    EXTENSIONS = frozenset({".docx", ".xlsx", ".pptx", ".docm", ".xlsm", ".pptm"})
    PRIORITY = 15

    def writable_field(self, path: Path, key: str) -> MetadataField | None:
        name = key[7:] if key.startswith("office:") else ""
        return _schema(name) if name in _PROPS else None

    def writable_fields(self, path: Path, record: MetadataRecord) -> list[MetadataField]:
        if not record.format_supports_write or record.read_errors:
            return []
        present = {f.key for f in record.fields}
        return [_schema(n) for n in _PROPS if f"office:{n}" not in present]

    def read(self, path: Path) -> MetadataRecord:
        fields: list[MetadataField] = []
        errors: list[str] = []
        warnings: list[str] = []
        signed = False
        try:
            with ZipFile(path) as archive:
                root = _load_core(archive)
                signed = _is_signed(archive)
                for part, reader in ((_APP_PART, _app_fields), (_CUSTOM_PART, _custom_fields)):
                    try:
                        extra = _parse_part(archive, part, required=False)
                        extra_fields = [] if extra is None else reader(extra)
                    except (ValueError, ET.ParseError) as exc:
                        warnings.append(f"{part} could not be read: {exc}")
                        extra_fields = []
                    fields.extend(extra_fields)
            core = []
            for name in _PROPS:
                nodes = _nodes(root, name)
                if nodes:
                    core.append(_core_field(name, nodes[0], warnings))
            fields = core + fields
        except Exception as exc:  # read never raises
            errors.append(f"Office read error: {exc}")
            fields = []
        writable = not errors and not signed
        for field in fields:
            if field.key.startswith(("office:app.", "office:custom.")):
                field.deletable = writable
        record = self._make_record(path, read_errors=errors,
                                   supports_write=writable, supports_delete=writable)
        if not writable:
            for field in fields:
                field.editable = field.deletable = False
        record.read_only_reason = SIGNED_REASON if signed and not errors else None
        record.warnings.extend(warnings)
        record.fields = fields
        record.dedupe_keys()
        return record

    # ── validation ───────────────────────────────────────────────────────────

    def canonical(self, field: MetadataField, value: Any) -> Any:
        result = super().canonical(field, value)
        if field.field_type == "int" and field.key == "office:revision" and result < 1:
            raise MetadataValueError(field.key, ErrorCode.OUT_OF_RANGE, f"{field.key}: must be >= 1")
        if isinstance(result, str) and field.field_type in ("str", "text") and invalid_xml_char(result) is not None:
            raise MetadataValueError(field.key, ErrorCode.INVALID_VALUE,
                                     f"{field.key}: contains characters not allowed in XML")
        return result

    # ── mutation hooks ───────────────────────────────────────────────────────

    def apply_write(self, tmp: Path, fields: list[MetadataField]) -> None:
        root = _open_core(tmp)
        errors: list[MetadataValueError] = []
        updates: dict[str, Any] = {}
        for f in fields:
            try:
                name = _property_name(f.key)
                nodes = _nodes(root, name)
                current = _core_field(name, nodes[0], None) if nodes else None
                if current is not None and not current.editable:
                    raise MetadataValueError(f.key, ErrorCode.READ_ONLY,
                                             f"{f.key}: value in the file is not valid; delete it first")
                value = self.canonical(current or _schema(name), f.value)
                if current is not None and len(nodes) == 1 and current.value == value:
                    continue  # unchanged: keep the original node
                updates[name] = value
            except MetadataValueError as exc:
                errors.append(_attributed(exc, f.key))
        _raise_collected(errors)
        if not updates:
            return
        for name, value in updates.items():
            nodes = _nodes(root, name)
            for extra in nodes[1:]:
                root.remove(extra)
            node = nodes[0] if nodes else ET.SubElement(root, _qname(_PROPS[name][0]))
            node.text = _encode(name, value)
            if name in _DATES and node.get(_qname("xsi:type")) is None:
                node.set(_qname("xsi:type"), "dcterms:W3CDTF")
        _rewrite_core(tmp, root)
        # Read-after-write verification (G4).
        check = _open_core(tmp)
        for name, value in updates.items():
            nodes = _nodes(check, name)
            if len(nodes) != 1 or _core_field(name, nodes[0], None).value != value:
                raise MetadataValueError(f"office:{name}", ErrorCode.LOSSY_WRITE)

    def apply_delete(self, tmp: Path, keys: list[str]) -> None:
        extra = [k for k in keys if _extra_part(k)]
        keys = [k for k in keys if not _extra_part(k)]
        if extra:
            self._delete_extra(tmp, extra)
        if not keys:
            return
        root = _open_core(tmp)
        errors: list[MetadataValueError] = []
        names = []
        for key in keys:
            try:
                names.append(_property_name(key))
            except MetadataValueError as exc:
                errors.append(exc)
        _raise_collected(errors)
        changed = False
        for name in names:
            for node in _nodes(root, name):
                root.remove(node)
                changed = True
        if changed:
            _rewrite_core(tmp, root)

    def snapshot(self, tmp: Path, keys: list[str]) -> Snapshot:
        with ZipFile(tmp) as archive:
            root = _load_core(archive)
            parts = {part: _parse_part(archive, part, required=False) for part in (_APP_PART, _CUSTOM_PART)}
        snap: Snapshot = {}
        for key in keys:
            located = _extra_part(key)
            if located:
                part_root = parts[located[0]]
                nodes = _extra_nodes(part_root, *located) if part_root is not None else []
                snap[key] = tuple((list(part_root).index(n), _node_bytes(n)) for n in nodes) if nodes else ABSENT
                continue
            nodes = _nodes(root, _property_name(key))
            snap[key] = tuple(_node_bytes(n) for n in nodes) if nodes else ABSENT
        return snap

    def restore(self, tmp: Path, snap: Snapshot) -> None:
        extra = {k: v for k, v in snap.items() if _extra_part(k)}
        snap = {k: v for k, v in snap.items() if not _extra_part(k)}
        if extra:
            self._restore_extra(tmp, extra)
        if not snap:
            return
        root = _open_core(tmp)
        for key, raw in snap.items():
            name = _property_name(key)
            for node in _nodes(root, name):
                root.remove(node)
            if raw is not ABSENT:
                for data in raw:
                    node = ET.fromstring(data)
                    if node.tag != _qname(_PROPS[name][0]):
                        raise MetadataValueError(key, ErrorCode.INVALID_VALUE, f"{key}: snapshot mismatch")
                    root.append(node)
        _rewrite_core(tmp, root)

    @staticmethod
    def _part_roots(tmp: Path) -> dict:
        with ZipFile(tmp) as archive:
            if _is_signed(archive):
                raise MetadataValueError(None, ErrorCode.READ_ONLY,
                                         "Editing digitally signed Office packages is not supported")
            return {part: _parse_part(archive, part, required=False) for part in (_APP_PART, _CUSTOM_PART)}

    def _delete_extra(self, tmp: Path, keys: list[str]) -> None:
        roots = self._part_roots(tmp)
        errors = []
        for key in keys:
            part, name = _extra_part(key)
            nodes = _extra_nodes(roots[part], part, name) if roots[part] is not None else []
            if not nodes:
                errors.append(MetadataValueError(key, ErrorCode.UNKNOWN_FIELD))
                continue
            for node in nodes:
                roots[part].remove(node)
        _raise_collected(errors)
        _rewrite_parts(tmp, {part: ET.tostring(root, encoding="utf-8", xml_declaration=True)
                             for part, root in roots.items() if root is not None})

    def _restore_extra(self, tmp: Path, snap: Snapshot) -> None:
        roots = self._part_roots(tmp)
        for key, raw in snap.items():
            part, name = _extra_part(key)
            if roots[part] is None:
                continue
            for node in _extra_nodes(roots[part], part, name):
                roots[part].remove(node)
            if raw is not ABSENT:
                for index, data in sorted(raw):
                    roots[part].insert(min(index, len(roots[part])), ET.fromstring(data))
        _rewrite_parts(tmp, {part: ET.tostring(root, encoding="utf-8", xml_declaration=True)
                             for part, root in roots.items() if root is not None})

    # ── public wrappers (own transaction) ────────────────────────────────────

    def write(self, path: Path, fields: list[MetadataField]) -> None:
        self._atomic_write(path, lambda tmp: self.apply_write(tmp, fields))

    def delete(self, path: Path, keys: list[str]) -> None:
        self._atomic_write(path, lambda tmp: self.apply_delete(tmp, keys))


def _rewrite_core(path: Path, root) -> None:
    """Replace docProps/core.xml, streaming every other member unchanged.

    An Office library would rebuild the package and can drop VBA, custom XML,
    shapes and vendor extensions. The result is written back into the same file
    opened ``r+b`` (never recreated: Windows refuses ``wb`` on hidden files).
    """
    _rewrite_parts(path, {_CORE_PART: ET.tostring(root, encoding="utf-8", xml_declaration=True)})


def _rewrite_parts(path: Path, replacements: dict[str, bytes]) -> None:
    """Replace the named parts of the package, streaming every other member unchanged."""
    with tempfile.TemporaryFile(dir=path.parent) as output:
        with ZipFile(path) as source:
            with ZipFile(output, "w") as destination:
                destination.comment = source.comment
                for info in source.infolist():
                    if info.filename in replacements:
                        destination.writestr(copy(info), replacements[info.filename])
                    else:
                        with source.open(info) as reader, destination.open(copy(info), "w") as writer:
                            shutil.copyfileobj(reader, writer)
        output.seek(0)
        with path.open("r+b") as target:
            shutil.copyfileobj(output, target)
            target.truncate()


# -- application / custom properties: removable one by one --

def _extra_part(key: str) -> tuple[str, str] | None:
    """(part, property name) of an ``office:app.<Name>`` / ``office:custom.<name>`` key."""
    if key.startswith("office:app."):
        return _APP_PART, key[len("office:app."):]
    if key.startswith("office:custom."):
        return _CUSTOM_PART, key[len("office:custom."):]
    return None


def _extra_nodes(root, part: str, name: str) -> list:
    if part == _APP_PART:
        return [c for c in root if _local(c.tag) == (_APP_NS, name)]
    return [c for c in root if c.tag == "{" + _CUSTOM_NS + "}property" and c.get("name") == name]
