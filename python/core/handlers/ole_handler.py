from __future__ import annotations
import struct
from pathlib import Path

from core.base_handler import BaseMetadataHandler
from core.models import MetadataField, MetadataRecord

_OLE_EXTS = frozenset({".doc", ".xls", ".ppt", ".dot", ".xlt", ".pot"})

_SUMMARY_PROPS = {
    2: ("ole:title", "Title"),
    3: ("ole:subject", "Subject"),
    4: ("ole:author", "Author"),
    5: ("ole:keywords", "Keywords"),
    6: ("ole:comments", "Comments"),
    7: ("ole:template", "Template"),
    8: ("ole:last_author", "Last Author"),
    9: ("ole:revision", "Revision Number"),
    10: ("ole:edit_time", "Total Edit Time"),
    11: ("ole:last_print", "Last Printed"),
    12: ("ole:create_time", "Created"),
    13: ("ole:modified_time", "Modified"),
    14: ("ole:num_pages", "Number of Pages"),
    15: ("ole:num_words", "Number of Words"),
    16: ("ole:num_chars", "Number of Characters"),
    17: ("ole:thumbnail", "Thumbnail"),
    18: ("ole:app_name", "Application Name"),
    19: ("ole:security", "Security"),
}


class OleHandler(BaseMetadataHandler):
    NAME = "ole"
    EXTENSIONS = _OLE_EXTS
    PRIORITY = 15

    def read(self, path: Path) -> MetadataRecord:
        fields: list[MetadataField] = []
        errors: list[str] = []
        try:
            import olefile
            if not olefile.isOleFile(str(path)):
                errors.append("Not a valid OLE file")
            else:
                with olefile.OleFileIO(str(path)) as ole:
                    if ole.exists("\x05SummaryInformation"):
                        props = ole.getproperties("\x05SummaryInformation", convert_time=True)
                        for pid, val in props.items():
                            info = _SUMMARY_PROPS.get(pid)
                            if info is None:
                                continue
                            key, label = info
                            if isinstance(val, bytes):
                                val = val.decode("utf-8", errors="replace")
                            elif hasattr(val, "isoformat"):
                                val = val.isoformat()
                            fields.append(MetadataField(key, label, str(val),
                                                        editable=False, deletable=False,
                                                        source="ole"))
        except Exception as e:
            errors.append(f"OLE read error: {e}")

        record = self._make_record(path, read_errors=errors,
                                   supports_write=False, supports_delete=False)
        record.fields = fields
        return record

    def write(self, path: Path, fields: list[MetadataField]) -> None:
        raise NotImplementedError("OLE write not supported — use docx/xlsx/pptx format")

    def delete(self, path: Path, keys: list[str]) -> None:
        raise NotImplementedError("OLE delete not supported — use docx/xlsx/pptx format")
