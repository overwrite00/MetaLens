from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class MetadataField:
    key: str
    label: str
    value: Any
    raw_value: Any = None
    editable: bool = True
    deletable: bool = True
    field_type: str = "str"   # str | int | float | datetime | bytes | enum
    source: str = "unknown"   # exif | iptc | xmp | id3 | vorbis | pdf | filesystem | ...
    choices: list[str] = field(default_factory=list)

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
            "choices": self.choices,
        }

    @staticmethod
    def _serialize_value(v: Any) -> Any:
        if isinstance(v, bytes):
            return f"<binary {len(v)} bytes>"
        if hasattr(v, "isoformat"):
            return v.isoformat()
        return v


@dataclass
class MetadataRecord:
    file_path: Path
    file_size: int
    mtime: float
    handler_name: str
    fields: list[MetadataField] = field(default_factory=list)
    read_errors: list[str] = field(default_factory=list)
    supports_write: bool = False
    supports_delete: bool = False

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
