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
from core.handlers.qt_fields import qt_fields
from core.handlers.hachoir_handler import extract_fields
from core.models import MetadataField, MetadataRecord

_RW_EXTS = frozenset({".mp4", ".m4v", ".mov", ".m4b", ".m4r"})
_MP4_FORMATS = frozenset({"mp4", "mov", "m4a"})


def _is_rw(path: Path) -> bool:
    """An MP4-family file by name, or without extension but with MP4-family bytes."""
    suffix = path.suffix.lower()
    return suffix in _RW_EXTS or (suffix == "" and sniff.detect(path) in _MP4_FORMATS)


_RO_EXTS = frozenset({".mkv", ".webm", ".avi", ".wmv", ".flv", ".ts", ".mts", ".m2ts"})


class VideoHandler(TagMutations, BaseMetadataHandler):
    NAME = "video"
    EXTENSIONS = _RW_EXTS | _RO_EXTS
    PRIORITY = 25

    def _open_codec(self, path: Path):
        if not _is_rw(path):
            return None, False
        import mutagen.mp4
        return Mp4Codec(mutagen.mp4.MP4(path)), True

    def read(self, path: Path) -> MetadataRecord:
        ext = path.suffix.lower()
        if _is_rw(path):
            fields, errors, notes, reason = _read_mp4(path)
            writable = not errors and reason is None
        else:
            fields, errors = _read_hachoir(path)
            notes, reason = [], None
            writable = False
        record = self._make_record(path, read_errors=errors, warnings=notes,
                                   supports_write=writable, supports_delete=writable)
        if errors:
            for f in fields:
                f.editable = f.deletable = False
        elif not writable:
            record.read_only_reason = reason or f"{ext or 'this'} container is read-only"
        record.fields = fields
        return record

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
        structure, notes = qt_fields(path)
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
