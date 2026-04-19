from __future__ import annotations
from pathlib import Path

from core.base_handler import BaseMetadataHandler
from core.models import MetadataField, MetadataRecord


class HachoirHandler(BaseMetadataHandler):
    NAME = "hachoir"
    EXTENSIONS = frozenset()  # handles everything not claimed by others
    PRIORITY = 100            # lowest priority — true fallback

    def can_handle(self, path: Path) -> bool:
        return True  # universal fallback

    def read(self, path: Path) -> MetadataRecord:
        fields: list[MetadataField] = []
        errors: list[str] = []
        try:
            from hachoir.parser import createParser
            from hachoir.metadata import extractMetadata
            parser = createParser(str(path))
            if parser is None:
                errors.append("hachoir: unrecognized format")
            else:
                with parser:
                    meta = extractMetadata(parser)
                if meta:
                    for item in meta.exportPlaintext():
                        if ": " in item:
                            label, value = item.split(": ", 1)
                            label = label.strip("- ")
                            key = f"hachoir:{label.lower().replace(' ', '_')}"
                            fields.append(MetadataField(
                                key, label, value.strip(),
                                editable=False, deletable=False, source="hachoir"
                            ))
        except Exception as e:
            errors.append(f"hachoir error: {e}")

        record = self._make_record(path, read_errors=errors,
                                   supports_write=False, supports_delete=False)
        record.fields = fields
        return record

    def write(self, path: Path, fields: list[MetadataField]) -> None:
        raise NotImplementedError("hachoir handler is read-only")

    def delete(self, path: Path, keys: list[str]) -> None:
        raise NotImplementedError("hachoir handler is read-only")
