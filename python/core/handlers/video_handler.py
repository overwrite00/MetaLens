"""Video metadata: MP4/QuickTime tags through mutagen, other containers via hachoir.

MP4 family (``.mp4 .m4v .mov .m4b .m4r``): ``video:duration`` (movie length),
``audio:*`` for the first audio track when there is one, and the typed
``mp4:<atom>`` tags of ``_mp4_codec`` (same keys as ``.m4a`` in the audio
handler). Other containers are read-only (hachoir).
"""
from __future__ import annotations
from pathlib import Path

from core.base_handler import BaseMetadataHandler
from core.handlers._mp4_codec import MP4_LABELS as _MP4_LABELS, Mp4Codec  # noqa: F401
from core.handlers._tag_codecs import TagMutations
from core.handlers.hachoir_handler import extract_fields
from core.models import MetadataField, MetadataRecord

_RW_EXTS = frozenset({".mp4", ".m4v", ".mov", ".m4b", ".m4r"})
_RO_EXTS = frozenset({".mkv", ".webm", ".avi", ".wmv", ".flv", ".ts", ".mts", ".m2ts"})


class VideoHandler(TagMutations, BaseMetadataHandler):
    NAME = "video"
    EXTENSIONS = _RW_EXTS | _RO_EXTS
    PRIORITY = 25

    def _open_codec(self, path: Path):
        if path.suffix.lower() not in _RW_EXTS:
            return None, False
        import mutagen.mp4
        return Mp4Codec(mutagen.mp4.MP4(path)), True

    def read(self, path: Path) -> MetadataRecord:
        ext = path.suffix.lower()
        if ext in _RW_EXTS:
            fields, errors = _read_mp4(path)
            writable = not errors
        else:
            fields, errors = _read_hachoir(path)
            writable = False
        record = self._make_record(path, read_errors=errors,
                                   supports_write=writable, supports_delete=writable)
        if errors:
            for f in fields:
                f.editable = f.deletable = False
        elif not writable:
            record.read_only_reason = f"{ext or 'this'} container is read-only"
        record.fields = fields
        return record

    def write(self, path: Path, fields: list[MetadataField]) -> None:
        self._atomic_write(path, lambda tmp: self._write_tags(tmp, fields))

    def _write_tags(self, path: Path, fields: list[MetadataField]) -> None:
        if path.suffix.lower() not in _RW_EXTS:
            raise NotImplementedError(f"Write not supported for {path.suffix}")
        self.apply_write(path, fields)

    def delete(self, path: Path, keys: list[str]) -> None:
        self._atomic_write(path, lambda tmp: self._delete_tags(tmp, keys))

    def _delete_tags(self, path: Path, keys: list[str]) -> None:
        if path.suffix.lower() not in _RW_EXTS:
            raise NotImplementedError(f"Delete not supported for {path.suffix}")
        self.apply_delete(path, keys)


def _read_mp4(path: Path) -> tuple[list[MetadataField], list[str]]:
    from core.handlers.audio_handler import stream_fields
    fields: list[MetadataField] = []
    errors: list[str] = []
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
    return fields, errors


# Moved to the hachoir handler; kept under the old name for existing callers.
_read_hachoir = extract_fields
