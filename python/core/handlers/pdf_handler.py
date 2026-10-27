from __future__ import annotations
from pathlib import Path

from core.base_handler import BaseMetadataHandler
from core.models import MetadataField, MetadataRecord

_PDF_LABELS = {
    "/Title": "Title",
    "/Author": "Author",
    "/Subject": "Subject",
    "/Keywords": "Keywords",
    "/Creator": "Creator Application",
    "/Producer": "Producer",
    "/CreationDate": "Creation Date",
    "/ModDate": "Modified Date",
    "/Trapped": "Trapped",
}


class PdfHandler(BaseMetadataHandler):
    NAME = "pdf"
    EXTENSIONS = frozenset({".pdf"})
    PRIORITY = 15

    def writable_field(self, path: Path, key: str) -> MetadataField | None:
        pdf_key = "/" + key[4:] if key.startswith("pdf:") else ""
        if pdf_key in _PDF_LABELS:
            return MetadataField(key, _PDF_LABELS[pdf_key], "", source="pdf")
        return None

    def read(self, path: Path) -> MetadataRecord:
        fields: list[MetadataField] = []
        errors: list[str] = []
        writable = False
        try:
            from pypdf import PdfReader
            reader = PdfReader(str(path))
            writable = not reader.is_encrypted
            meta = reader.metadata or {}
            for key, val in meta.items():
                label = _PDF_LABELS.get(str(key), str(key).lstrip("/"))
                editable = str(key) in _PDF_LABELS
                fields.append(MetadataField(
                    f"pdf:{key.lstrip('/')}", label, str(val),
                    editable=editable, deletable=editable, source="pdf"
                ))
            # Page count
            fields.append(MetadataField("pdf:page_count", "Page Count",
                                        len(reader.pages),
                                        editable=False, deletable=False,
                                        field_type="int", source="pdf"))
            # Encryption
            fields.append(MetadataField("pdf:encrypted", "Encrypted",
                                        reader.is_encrypted,
                                        editable=False, deletable=False,
                                        field_type="str", source="pdf"))
        except Exception as e:
            errors.append(f"PDF read error: {e}")

        record = self._make_record(path, read_errors=errors,
                                   supports_write=writable, supports_delete=writable)
        if not writable:
            for field in fields:
                field.editable = field.deletable = False
        record.fields = fields
        return record

    def write(self, path: Path, fields: list[MetadataField]) -> None:
        def _do_write(tmp: Path) -> None:
            from pypdf import PdfReader, PdfWriter
            reader = PdfReader(str(tmp))
            if reader.is_encrypted:
                raise ValueError("Editing encrypted PDFs is not supported")
            writer = PdfWriter()
            writer.clone_document_from_reader(reader)
            updates = {}
            for f in fields:
                if f.key.startswith("pdf:") and f.editable:
                    pdf_key = "/" + f.key[len("pdf:"):]
                    updates[pdf_key] = str(f.value)
            if updates:
                writer.add_metadata(updates)
            import io as _io
            buf = _io.BytesIO()
            writer.write(buf)
            tmp.write_bytes(buf.getvalue())

        self._atomic_write(path, _do_write)

    def delete(self, path: Path, keys: list[str]) -> None:
        def _do_delete(tmp: Path) -> None:
            from pypdf import PdfReader, PdfWriter
            import io as _io
            reader = PdfReader(str(tmp))
            if reader.is_encrypted:
                raise ValueError("Editing encrypted PDFs is not supported")
            writer = PdfWriter()
            writer.clone_document_from_reader(reader)
            existing = dict(reader.metadata or {})
            for key in keys:
                pdf_key = "/" + key[len("pdf:"):] if key.startswith("pdf:") else key
                existing.pop(pdf_key, None)
            writer.metadata = existing
            buf = _io.BytesIO()
            writer.write(buf)
            tmp.write_bytes(buf.getvalue())

        self._atomic_write(path, _do_delete)
