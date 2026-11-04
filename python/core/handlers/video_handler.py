"""Video metadata: MP4/QuickTime tags through mutagen, other containers via hachoir.

MP4 family (``.mp4 .m4v .mov .m4b .m4r``): ``video:duration`` (movie length),
``audio:*`` for the first audio track when there is one, and the typed
``mp4:<atom>`` tags of ``_mp4_codec`` (same keys as ``.m4a`` in the audio
handler). Other containers are read-only (hachoir).
"""
from __future__ import annotations
from pathlib import Path

from core import sniff
from core.base_handler import BaseMetadataHandler
from core.handlers._mp4_codec import Mp4Codec
from core.handlers._tag_codecs import TagMutations
from core.handlers import mkv_fields, qt_edit, qt_patch
from core.handlers.qt_fields import qt_fields
from core.handlers.hachoir_handler import extract_fields
from core.models import MetadataField, MetadataRecord

_RW_EXTS = frozenset({".mp4", ".m4v", ".mov", ".m4b", ".m4r"})
_MP4_FORMATS = frozenset({"mp4", "mov", "m4a"})


def _is_rw(path: Path) -> bool:
    """An MP4-family file by name, or without extension but with MP4-family bytes."""
    suffix = path.suffix.lower()
    return suffix in _RW_EXTS or (suffix == "" and sniff.detect(path) in _MP4_FORMATS)


_MKV_EXTS = frozenset({".mkv", ".webm", ".mka"})
_RO_EXTS = frozenset({".avi", ".wmv", ".flv", ".ts", ".mts", ".m2ts"})


def _is_mkv(path: Path) -> bool:
    """A Matroska / WebM file by name, or without extension but with Matroska bytes."""
    suffix = path.suffix.lower()
    return suffix in _MKV_EXTS or (suffix == "" and sniff.detect(path) == "mkv")


class VideoHandler(TagMutations, BaseMetadataHandler):
    NAME = "video"
    EXTENSIONS = _RW_EXTS | _MKV_EXTS | _RO_EXTS
    PRIORITY = 25

    def _open_codec(self, path: Path):
        if not _is_rw(path):
            return None, False
        import mutagen.mp4
        return Mp4Codec(mutagen.mp4.MP4(path)), True

    def read(self, path: Path) -> MetadataRecord:
        ext = path.suffix.lower()
        if _is_mkv(path):
            return self._read_mkv(path)
        if _is_rw(path):
            fields, errors, notes, reason = _read_mp4(path)
            writable = not errors and reason is None
        else:
            fields, errors = _read_hachoir(path)
            notes, reason = [], None
            writable = False
        record = self._make_record(path, read_errors=errors, warnings=notes,
                                   supports_write=writable, supports_delete=writable)
        if writable:
            self._mark_removable(path, fields)
        if errors or not writable:
            for f in fields:
                f.editable = f.deletable = False
        if errors:
            pass
        elif not writable:
            record.read_only_reason = reason or f"{ext or 'this'} container is read-only"
        record.fields = fields
        return record

    def _read_mkv(self, path: Path) -> MetadataRecord:
        errors: list[str] = []
        try:
            fields, notes, editable = mkv_fields.fields(path)
        except Exception as exc:                         # damaged file: shown as an error, read-only
            fields, notes, editable = [], [], False
            errors.append(f"Matroska read error: {exc}")
        record = self._make_record(path, read_errors=errors, warnings=notes,
                                   supports_write=editable and not errors, supports_delete=editable and not errors)
        record.fields = fields
        if errors or not editable:
            for f in fields:
                f.editable = f.deletable = False
            if not errors:
                record.read_only_reason = "This recording cannot be changed"
        record.dedupe_keys()
        return record

    @staticmethod
    def _mark_removable(path: Path, fields: list[MetadataField]) -> None:
        """QuickTime structure fields and XMP can be removed (in place, nothing moves); only the
        mutagen tags are editable."""
        try:
            removable = set(qt_patch.plan(path))
        except Exception:
            removable = set()
        for f in fields:
            if f.key.startswith("qt:"):
                f.deletable = f.key in removable
                f.editable = f.key in removable and (
                    f.key in qt_edit.DATE_KEYS or f.key.endswith(":CreateDate") or f.key.startswith("qt:udta:")
                    or (f.key.startswith("qt:mdta:") and f.field_type == "str"))

    # ── mutation hooks: tags through mutagen, structure and XMP by in-place patches ──
    @staticmethod
    def _split(keys):
        qt = [k for k in keys if k.startswith("qt:")]
        xmp_keys = [k for k in keys if k.startswith("xmp:")]
        return qt, xmp_keys, [k for k in keys if k not in qt and k not in xmp_keys]

    def _mkv_split(self, keys):
        mkv = [k for k in keys if k.startswith("mkv:")]
        return mkv, [k for k in keys if k not in mkv]

    def apply_write(self, tmp: Path, fields: list[MetadataField]) -> None:
        if _is_mkv(tmp):
            from core import ebml
            from core.errors import ErrorCode, MetadataValueError, MetadataValueErrors
            current = {f.key: f.value for f in mkv_fields.fields(tmp)[0]}
            errors = []
            for f in fields:
                if current.get(f.key) == f.value:
                    continue
                try:
                    mkv_fields.set_text(tmp, f.key, f.value)
                except (ebml.EbmlError, ValueError) as exc:
                    errors.append(MetadataValueError(f.key, ErrorCode.UNSUPPORTED, f"{f.key}: {exc}"))
            if errors:
                raise MetadataValueErrors(errors)
            return
        from core.errors import ErrorCode, MetadataValueError, MetadataValueErrors
        from core.handlers.image_handler import ImageHandler
        qt = [f for f in fields if f.key.startswith("qt:")]
        xmp_fields = [f for f in fields if f.key.startswith("xmp:")]
        rest = [f for f in fields if f not in qt and f not in xmp_fields]
        errors = []
        current = {x.key: x.value for x in qt_fields(tmp)[0]} if qt else {}
        for f in qt:
            if current.get(f.key) == f.value:
                continue                                    # already what it should be: the file is left alone
            try:
                qt_edit.set_value(tmp, f.key, f.value)
            except (qt_edit.LayoutError, ValueError) as exc:
                errors.append(MetadataValueError(f.key, ErrorCode.UNSUPPORTED, f"{f.key}: {exc}"))
        if errors:
            raise MetadataValueErrors(errors)
        if rest:
            super().apply_write(tmp, rest)
        if xmp_fields:
            ImageHandler().apply_write(tmp, xmp_fields)

    def writable_fields(self, path: Path, record) -> list[MetadataField]:
        """Tags (mutagen), QuickTime dates / text entries and XMP properties that can be added."""
        from core.handlers.image_handler import ImageHandler, _Mp4State
        out = list(super().writable_fields(path, record))
        present = {f.key for f in record.fields}
        if record.read_errors or not _is_rw(path):
            return out
        try:
            fields = {f.key for f in qt_fields(path)[0]}
            with_header = any(k in fields for k in ("qt:TimeScale",))
        except Exception:
            return out
        if with_header:
            for key, label in (("qt:CreateDate", "Create Date"), ("qt:ModifyDate", "Modify Date")):
                if key not in present:
                    out.append(MetadataField(key, label, None, field_type="datetime", source="qt"))
        for atom, label in qt_edit.UDTA_ADDABLE.items():
            if f"qt:udta:{atom}" not in present:
                out.append(MetadataField(f"qt:udta:{atom}", f"QuickTime {atom}", None, source="qt"))
        for name, label in qt_edit.MDTA_ADDABLE.items():
            if f"qt:mdta:{name}" not in present:
                out.append(MetadataField(f"qt:mdta:{name}", f"QuickTime {name}", None, source="qt"))
        try:
            state = _Mp4State.load(Path(path).read_bytes())
            out += ImageHandler()._addable(state, present)
        except Exception:
            pass  # a file that cannot be parsed simply offers no fields to add
        return out

    def writable_field(self, path: Path, key: str) -> MetadataField | None:
        record = self.read(path)
        found = next((f for f in record.addable_fields if f.key == key), None)
        return found or super().writable_field(path, key)

    def apply_delete(self, tmp: Path, keys: list[str]) -> None:
        if _is_mkv(tmp):
            from core.errors import ErrorCode, MetadataValueError, MetadataValueErrors
            plan = mkv_fields.removal_plan(tmp)
            missing = [k for k in keys if k not in plan]
            if missing:
                raise MetadataValueErrors([MetadataValueError(k, ErrorCode.NOT_DELETABLE) for k in missing])
            for key in keys:
                mkv_fields.apply_patches(tmp, plan[key])
            return
        from core.handlers.image_handler import ImageHandler
        qt, xmp_keys, rest = self._split(keys)
        if qt:
            patches = qt_patch.plan(tmp)
            missing = [k for k in qt if k not in patches]
            if missing:
                from core.errors import ErrorCode, MetadataValueError, MetadataValueErrors
                raise MetadataValueErrors([MetadataValueError(k, ErrorCode.NOT_DELETABLE) for k in missing])
            for key in qt:
                qt_patch.apply(tmp, patches[key])
        if xmp_keys:
            ImageHandler().apply_delete(tmp, xmp_keys)
        if rest:
            super().apply_delete(tmp, rest)

    @staticmethod
    def _removed_in_place(path: Path, patches) -> bool:
        """The bytes of a removed field are still the zeroed / free atom this code wrote."""
        with open(path, "rb") as stream:
            for offset, original, replacement in patches:
                stream.seek(offset)
                if stream.read(len(replacement)) != replacement:
                    return False
        return True

    def snapshot(self, tmp: Path, keys: list[str]):
        from core.base_handler import ABSENT
        if _is_mkv(tmp):
            plan = mkv_fields.removal_plan(tmp)
            current = {f.key: f for f in mkv_fields.fields(tmp)[0]}
            return {k: (qt_edit.QtRaw(current[k].value, plan.get(k, ())) if k in current else ABSENT) for k in keys}
        from core.handlers.image_handler import ImageHandler
        qt, xmp_keys, rest = self._split(keys)
        snap = super().snapshot(tmp, rest) if rest else {}
        if xmp_keys:
            snap.update(ImageHandler().snapshot(tmp, xmp_keys))
        if qt:
            patches = qt_patch.plan(tmp)
            current = {f.key: f for f in qt_fields(tmp)[0]}
            for key in qt:
                if key not in current:
                    snap[key] = ABSENT
                    continue
                plan = tuple((p.offset, p.original, p.replacement) for p in patches.get(key, ()))
                snap[key] = qt_edit.QtRaw(current[key].value, plan)
        return snap

    def restore(self, tmp: Path, snap) -> None:
        from core.base_handler import ABSENT
        if _is_mkv(tmp):
            plan = mkv_fields.removal_plan(tmp)
            current = {f.key: f for f in mkv_fields.fields(tmp)[0]}
            for key, raw in snap.items():
                if raw is ABSENT:
                    if key in plan:
                        mkv_fields.apply_patches(tmp, plan[key])
                elif key in current and current[key].value == raw.value:
                    continue
                elif key not in current and raw.patches and self._removed_in_place(tmp, raw.patches):
                    mkv_fields.apply_patches(tmp, raw.patches, restore=True)
                else:
                    mkv_fields.set_text(tmp, key, raw.value)
            return
        from core.handlers.image_handler import ImageHandler
        qt, xmp_keys, rest = self._split(list(snap))
        if rest:
            super().restore(tmp, {k: snap[k] for k in rest})
        if xmp_keys:
            ImageHandler().restore(tmp, {k: snap[k] for k in xmp_keys})
        if qt:
            current = {f.key: f for f in qt_fields(tmp)[0]}
            patches = qt_patch.plan(tmp)
            for key in qt:
                raw = snap[key]
                if raw is ABSENT:
                    if key in patches:
                        qt_patch.apply(tmp, patches[key])           # added by the change: take it out again
                    continue
                if key in current and current[key].value == raw.value:
                    continue
                if key not in current and raw.patches and self._removed_in_place(tmp, raw.patches):
                    with open(tmp, "r+b") as stream:
                        for offset, original, _replacement in raw.patches:
                            stream.seek(offset)
                            stream.write(original)
                    continue
                qt_edit.set_value(tmp, key, raw.value)

    def canonical(self, field: MetadataField, value):
        if field.key.startswith("mkv:"):
            from core import values
            return values.canonicalize(field, value)
        if field.key.startswith("qt:"):
            from core import values
            result = values.canonicalize(field, value)
            if field.field_type == "datetime":
                from datetime import timezone
                result = values.format_datetime(values.parse_datetime(result).astimezone(timezone.utc))
            if field.key.startswith(("qt:udta:", "qt:mdta:")):
                try:
                    qt_edit.text_of(field.key, result)
                except qt_edit.LayoutError as exc:
                    from core.errors import ErrorCode, MetadataValueError
                    raise MetadataValueError(field.key, ErrorCode.INVALID_VALUE, f"{field.key}: {exc}") from exc
            return result
        if field.key.startswith("xmp:"):
            from core.handlers.image_handler import ImageHandler
            return ImageHandler().canonical(field, value)
        return super().canonical(field, value)

    def content_digest(self, path: Path) -> str | None:
        """SHA-256 of the ``mdat`` atoms (the audio and video data)."""
        if _is_mkv(path):
            from core import ebml
            return ebml.media_digest(path)
        if not _is_rw(path):
            return None
        import hashlib
        from core.isobmff import open_reader
        digest = hashlib.sha256()
        with open_reader(path) as rd:
            for box in rd.children():
                if box.type == b"mdat":
                    for start in range(box.data_start, box.end, 1 << 20):
                        digest.update(bytes(rd.buf[start:min(start + (1 << 20), box.end)]))
        return digest.hexdigest()

    def write(self, path: Path, fields: list[MetadataField]) -> None:
        self._atomic_write(path, lambda tmp: self._write_tags(tmp, fields))

    def _write_tags(self, path: Path, fields: list[MetadataField]) -> None:
        if not _is_rw(path):
            raise NotImplementedError(f"Write not supported for {path.suffix}")
        self.apply_write(path, fields)

    def delete(self, path: Path, keys: list[str]) -> None:
        self._atomic_write(path, lambda tmp: self._delete_tags(tmp, keys))

    def _delete_tags(self, path: Path, keys: list[str]) -> None:
        if not _is_rw(path):
            raise NotImplementedError(f"Delete not supported for {path.suffix}")
        self.apply_delete(path, keys)


def _read_mp4(path: Path) -> tuple[list[MetadataField], list[str], list[str], str | None]:
    from core.handlers.audio_handler import stream_fields
    fields: list[MetadataField] = []
    errors: list[str] = []
    notes: list[str] = []
    try:
        import mutagen.mp4
        mp4 = mutagen.mp4.MP4(path)
        info = mp4.info
        if info is not None:
            duration = [f for f in stream_fields(info, "video") if f.key == "video:duration"]
            fields += duration
            if getattr(info, "codec", "") or getattr(info, "channels", 0):
                # MP4Info describes the first audio track, not the video stream.
                fields += [f for f in stream_fields(info, "audio") if f.key != "audio:duration"]
        fields += Mp4Codec(mp4).fields()
    except Exception as e:
        errors.append(f"MP4 read error: {e}")
    try:
        structure, notes = qt_fields(path, xmp_writable=True)
        fields += structure
    except Exception as e:     # the structure is extra information: never fail the read for it
        notes.append(f"QuickTime structure not read ({type(e).__name__})")
        structure = []
    reason = None
    if errors and structure:
        # The tag library cannot open this file (e.g. a phone movie), but its structure was read:
        # show it as a read-only file instead of a failed read.
        reason = "The tags of this file cannot be edited yet"
        notes.append(f"Tags not read: {errors[0]}")
        errors = []
    return fields, errors, notes, reason


# Moved to the hachoir handler; kept under the old name for existing callers.
_read_hachoir = extract_fields
