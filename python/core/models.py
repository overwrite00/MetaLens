from __future__ import annotations
import copy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core import sensitivity
from core.values import binary_descriptor


@dataclass
class MetadataField:
    """One metadata entry. ``value`` is canonical JSON (see ``core.values``).

    field_type: one of ``core.values.FIELD_TYPES``
                (str | text | int | float | bool | datetime | localdatetime |
                 rational | duration | binary | enum).
    multi:      value is a list of items of ``field_type`` (never a joined string).
    length:     fixed list size for tuples such as track [3, 10] (requires multi).
    binary:     for ``field_type == "binary"``: mirrors ``value``, i.e. the
                ``{"size", "sha256", "mime"}`` descriptor (a list of them when multi).
                Binary fields are never editable; build them with ``binary_field``.
    computed:   derived from the file (size, duration...) and so legitimately changed by any
                write; the post-write verification (G4) skips it. Every other read-only
                field must come back unchanged from a save.
    has_alternatives: the stored property holds more than the value shown (e.g. other language
                alternatives of an XMP ``rdf:Alt``). Deleting the property would lose them, so a
                delete that is only mirrored from a twin field leaves it in place. Not serialized.
    """
    key: str
    label: str
    value: Any
    raw_value: Any = None
    editable: bool = True
    deletable: bool = True
    field_type: str = "str"
    source: str = "unknown"   # exif | iptc | xmp | id3 | vorbis | pdf | filesystem | ...
    choices: list[Any] = field(default_factory=list)
    multi: bool = False
    length: int | None = None
    binary: dict | list[dict] | None = None
    computed: bool = False
    has_alternatives: bool = False

    @classmethod
    def binary_field(cls, key: str, label: str, data: bytes | list[bytes], *,
                     mime: str | None = None, deletable: bool = True,
                     source: str = "unknown", raw_value: Any = None) -> "MetadataField":
        """Read-only binary field whose value is the descriptor of ``data``.

        ``data`` may be a list of payloads (e.g. several cover images): the field is
        then ``multi`` and its value a list of descriptors. ``raw_value`` defaults to
        ``data`` (serialized as ``<binary N bytes>``).
        """
        if isinstance(data, (list, tuple)):
            descriptor: Any = [binary_descriptor(bytes(d), mime) for d in data]
            multi = True
        else:
            descriptor = binary_descriptor(bytes(data), mime)
            multi = False
        return cls(key, label, descriptor,
                   raw_value=data if raw_value is None else raw_value,
                   editable=False, deletable=deletable, field_type="binary",
                   source=source, multi=multi, binary=copy.deepcopy(descriptor))

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "value": self._serialize_value(self.value),
            "raw_value": self._serialize_value(self.raw_value),
            "editable": self.editable,
            "deletable": self.deletable,
            "field_type": self.field_type,
            "source": self.source,
            "choices": self._serialize_value(self.choices),
            "multi": self.multi,
            "length": self.length,
            "binary": self._serialize_value(self.binary),
            "sensitivity": sensitivity.classify(self.key),
        }

    @staticmethod
    def _serialize_value(v: Any) -> Any:
        if isinstance(v, (bytes, bytearray, memoryview)):
            return f"<binary {len(v)} bytes>"
        if hasattr(v, "isoformat"):
            return v.isoformat()
        if isinstance(v, (list, tuple)):
            return [MetadataField._serialize_value(x) for x in v]
        if isinstance(v, dict):
            return {str(k): MetadataField._serialize_value(x) for k, x in v.items()}
        return v


@dataclass
class MetadataRecord:
    """Result of ``handler.read``.

    supports_write / supports_delete: legacy flags, the OR of format and filesystem
        level once the API has merged the filesystem layer.
    format_supports_write / format_supports_delete: format level only; default to the
        ``supports_*`` value given at construction, so they are unaffected when the
        API later ORs the filesystem layer into ``supports_*``.
    read_only_reason: why a normally writable format is read-only (e.g. signed PDF).
    warnings: non-blocking notices (filesystem layer, MakerNote, XMP, ...).
        ``read_errors`` instead block format-level mutation.
    addable_fields: server-owned schema of fields that can be added.
    """
    file_path: Path
    file_size: int
    mtime: float
    handler_name: str
    fields: list[MetadataField] = field(default_factory=list)
    read_errors: list[str] = field(default_factory=list)
    supports_write: bool = False
    supports_delete: bool = False
    warnings: list[str] = field(default_factory=list)
    format_supports_write: bool | None = None
    format_supports_delete: bool | None = None
    read_only_reason: str | None = None
    addable_fields: list[MetadataField] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.format_supports_write is None:
            self.format_supports_write = self.supports_write
        if self.format_supports_delete is None:
            self.format_supports_delete = self.supports_delete

    def dedupe_keys(self) -> list[str]:
        """Enforce unique keys: later duplicates become ``key#2``, ``key#3``... read-only.

        Returns the new keys and adds one warning per duplicated key. Idempotent.
        """
        taken = {f.key for f in self.fields}
        seen: set[str] = set()
        renamed: list[str] = []
        duplicated: dict[str, int] = {}
        for f in self.fields:
            if f.key not in seen:
                seen.add(f.key)
                continue
            original = f.key
            n = 2
            while f"{original}#{n}" in taken:
                n += 1
            f.key = f"{original}#{n}"
            f.editable = f.deletable = False
            taken.add(f.key)
            seen.add(f.key)
            renamed.append(f.key)
            duplicated[original] = duplicated.get(original, 0) + 1
        for original, count in duplicated.items():
            self.warnings.append(
                f"Duplicate metadata key {original}: {count} extra entr"
                f"{'y' if count == 1 else 'ies'} shown read-only")
        return renamed

    def to_dict(self) -> dict:
        return {
            "file_path": str(self.file_path),
            "file_size": self.file_size,
            "mtime": self.mtime,
            "handler_name": self.handler_name,
            "fields": [f.to_dict() for f in self.fields],
            "read_errors": self.read_errors,
            "supports_write": self.supports_write,
            "supports_delete": self.supports_delete,
            "warnings": self.warnings,
            "format_supports_write": self.format_supports_write,
            "format_supports_delete": self.format_supports_delete,
            "read_only_reason": self.read_only_reason,
            "addable_fields": [f.to_dict() for f in self.addable_fields],
            "sensitivity_summary": sensitivity.summarize(f.key for f in self.fields),
        }


@dataclass
class DiffResult:
    file_a: Path
    file_b: Path
    only_in_a: list[MetadataField] = field(default_factory=list)
    only_in_b: list[MetadataField] = field(default_factory=list)
    changed: list[tuple[MetadataField, MetadataField]] = field(default_factory=list)
    identical: list[MetadataField] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "file_a": str(self.file_a),
            "file_b": str(self.file_b),
            "only_in_a": [f.to_dict() for f in self.only_in_a],
            "only_in_b": [f.to_dict() for f in self.only_in_b],
            "changed": [
                {"field_a": a.to_dict(), "field_b": b.to_dict()}
                for a, b in self.changed
            ],
            "identical": [f.to_dict() for f in self.identical],
        }
