"""HEIC / HEIF / AVIF images: read-only metadata from the ISO base media structure.

The still image is never decoded. The ``meta`` box is walked for the item table
(``iinf``), the item locations (``iloc``) and the image properties (``ipco`` /
``ipma``): size and rotation of the primary image, EXIF and XMP items and the ICC
profile. Item payloads are read from their ``iloc`` extents, bounded in size.
"""
from __future__ import annotations

import struct
from pathlib import Path

from core import exif_codec, icc, xmp
from core.base_handler import BaseMetadataHandler
from core.handlers.image_handler import ImageHandler, _HeifState, _exif_fields, _xmp_fields
from core.heif import MAX_EDIT_BYTES, _XMP_MIME, _items, _meta_children, _payload, _primary_properties, metadata_ranges
from core.isobmff import Reader, open_reader
from core.models import MetadataField, MetadataRecord


class HeifHandler(BaseMetadataHandler):
    NAME = "heif"
    EXTENSIONS = frozenset({".heic", ".heif", ".hif", ".avif"})
    PRIORITY = 12

    def read(self, path: Path) -> MetadataRecord:
        fields: list[MetadataField] = []
        errors: list[str] = []
        warnings: list[str] = []
        try:
            fields = self._fields(path, warnings)
        except Exception as exc:       # reading never raises: damaged data is a read error
            errors.append(f"HEIF read error: {type(exc).__name__}: {exc}")
        removable = self._editable(path) and not errors
        record = self._make_record(path, read_errors=errors, warnings=warnings,
                                   supports_write=removable, supports_delete=removable)
        record.fields = fields
        if not removable:
            record.read_only_reason = "The metadata of this HEIF/AVIF file is not stored in a way that can be changed"
            for f in fields:
                f.editable = f.deletable = False
        else:
            try:
                record.addable_fields = self.writable_fields(path, record)
            except Exception:           # the schema is optional: never fail the read for it
                record.addable_fields = []
        record.dedupe_keys()
        return record

    @staticmethod
    def _editable(path: Path) -> bool:
        """The Exif or XMP item is stored in one piece, so it can be changed."""
        if path.stat().st_size > MAX_EDIT_BYTES:
            return False
        return bool(metadata_ranges(path.read_bytes()))

    # ── mutation hooks: EXIF entries and XMP are changed in place (the image items never move) ──
    def apply_delete(self, tmp: Path, keys: list[str]) -> None:
        ImageHandler().apply_delete(tmp, keys)

    def snapshot(self, tmp: Path, keys: list[str]):
        return ImageHandler().snapshot(tmp, keys)

    def restore(self, tmp: Path, snap) -> None:
        ImageHandler().restore(tmp, snap)

    def canonical(self, field: MetadataField, value):
        return ImageHandler().canonical(field, value)

    def apply_write(self, tmp: Path, fields: list[MetadataField]) -> None:
        ImageHandler().apply_write(tmp, fields)

    def writable_fields(self, path: Path, record) -> list[MetadataField]:
        """EXIF entries (and XMP properties, when the file has an XMP item) that can be added."""
        from core import jpeg_segments
        if record.read_errors or path.stat().st_size > MAX_EDIT_BYTES:
            return []
        try:
            state = _HeifState.load(path.read_bytes())
        except jpeg_segments.JpegFormatError:
            return []
        present = {f.key for f in record.fields}
        return ImageHandler._tiff_addable(ImageHandler()._addable(state, present))

    def writable_field(self, path: Path, key: str) -> MetadataField | None:
        record = self.read(path)
        return next((f for f in record.addable_fields if f.key == key), None)

    def content_digest(self, path: Path) -> str | None:
        """SHA-256 of the data of the image items (everything except the Exif and XMP items), wherever
        the metadata items are stored."""
        import hashlib
        data = Path(path).read_bytes()
        rd = Reader(data)
        meta, children = _meta_children(rd)
        if meta is None:
            return None
        idat = next((b for b in children if b.type == b"idat"), None)
        digest = hashlib.sha256()
        for item in sorted(_items(rd, children).values(), key=lambda i: i.item_id):
            if item.kind == "Exif" or (item.kind == "mime" and item.content_type == _XMP_MIME):
                continue
            payload = _payload(rd, item, idat)
            if payload is None and item.extents:
                return None
            digest.update(struct.pack(">II", item.item_id, len(payload or b"")))
            digest.update(payload or b"")
        return digest.hexdigest()

    def _fields(self, path: Path, warnings: list[str]) -> list[MetadataField]:
        fields: list[MetadataField] = []

        def add(key, label, value, field_type="str", multi=False):
            fields.append(MetadataField(key, label, value, raw_value=value, editable=False, deletable=False,
                                        field_type=field_type, source="heif", multi=multi))

        with open_reader(path) as rd:
            ftyp = rd.find(None, b"ftyp")
            if ftyp is not None and ftyp.end - ftyp.data_start >= 8:
                major = bytes(rd.buf[ftyp.data_start:ftyp.data_start + 4]).decode("latin-1").strip()
                add("heif:MajorBrand", "Major Brand", major)
                brands = [bytes(rd.buf[p:p + 4]).decode("latin-1").strip()
                          for p in range(ftyp.data_start + 8, ftyp.end - 3, 4)]
                if brands:
                    add("heif:CompatibleBrands", "Compatible Brands", brands, multi=True)
            meta, children = _meta_children(rd)
            if meta is None:
                warnings.append("No metadata box found")
                return fields
            props = _primary_properties(rd, children)
            brands = {f.value for f in fields if f.key == "heif:MajorBrand"}
            for f in fields:
                if f.key == "heif:CompatibleBrands":
                    brands |= set(f.value)
            fmt = "AVIF" if brands & {"avif", "avis"} else "HEIF"
            fields.insert(0, MetadataField("img:format", "Format", fmt, raw_value=fmt, editable=False,
                                           deletable=False, source="image"))
            if "width" in props:
                fields.append(MetadataField("img:width", "Width (px)", props["width"], editable=False,
                                            deletable=False, field_type="int", source="image"))
                fields.append(MetadataField("img:height", "Height (px)", props["height"], editable=False,
                                            deletable=False, field_type="int", source="image"))
            if "rotation" in props:
                add("heif:Rotation", "Rotation (°)", props["rotation"], "int")
            idat = next((b for b in children if b.type == b"idat"), None)
            items = _items(rd, children)
            edited = self._state_fields(path, warnings)
            for item in ([] if edited is not None else items.values()):
                data = None
                if item.kind == "Exif":
                    data = _payload(rd, item, idat)
                    if data and len(data) > 4:
                        skip = struct.unpack_from(">I", data, 0)[0]
                        try:
                            fields += _exif_fields(exif_codec.parse(data[4 + skip:], with_ifd1=False), False)
                        except exif_codec.ExifFormatError as exc:
                            warnings.append(f"EXIF not readable: {exc}")
                elif item.kind == "mime" and item.content_type == _XMP_MIME:
                    data = _payload(rd, item, idat)
                    if data:
                        try:
                            fields += _xmp_fields(xmp.XmpPacket(data), False)
                        except xmp.XmpFormatError as exc:
                            warnings.append(f"XMP not readable: {exc}")
            if edited is not None:
                fields += edited
            for prop in (props.get("properties") or []):
                if prop.type == b"colr" and prop.end - prop.data_start > 4:
                    if bytes(rd.buf[prop.data_start:prop.data_start + 4]) in (b"prof", b"rICC"):
                        fields += icc.fields(bytes(rd.buf[prop.data_start + 4:prop.end]))
        return fields

    @staticmethod
    def _state_fields(path: Path, warnings: list[str]) -> list[MetadataField] | None:
        """EXIF / GPS / XMP fields with their removable flags, or ``None`` when the layout cannot be
        changed in place (the fields are then read straight from the items, read-only)."""
        from core import jpeg_segments
        if path.stat().st_size > MAX_EDIT_BYTES:
            return None
        data = path.read_bytes()
        if not metadata_ranges(data):
            return None
        try:
            state = _HeifState.load(data)
        except jpeg_segments.JpegFormatError:
            return None
        warnings += state.warnings
        return ImageHandler._state_fields(state, True)

    def write(self, path: Path, fields: list[MetadataField]) -> None:
        self._atomic_write(path, lambda tmp: self.apply_write(tmp, fields))

    def delete(self, path: Path, keys: list[str]) -> None:
        self._atomic_write(path, lambda tmp: self.apply_delete(tmp, keys))
