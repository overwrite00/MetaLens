from __future__ import annotations
from pathlib import Path

from core.base_handler import BaseMetadataHandler
from core.models import MetadataField, MetadataRecord

_RW_EXTS = frozenset({".mp4", ".m4v", ".mov", ".m4b", ".m4r"})
_RO_EXTS = frozenset({".mkv", ".webm", ".avi", ".wmv", ".flv", ".ts", ".mts", ".m2ts"})

_MP4_LABELS = {
    "\xa9nam": "Title", "\xa9ART": "Artist", "\xa9alb": "Album",
    "\xa9day": "Year", "\xa9gen": "Genre", "\xa9cmt": "Comment",
    "\xa9wrt": "Composer", "aART": "Album Artist",
    "trkn": "Track Number", "disk": "Disc Number",
    "cprt": "Copyright", "\xa9too": "Encoded By",
    "desc": "Description", "ldes": "Long Description",
    "tvsh": "TV Show", "tvsn": "TV Season", "tves": "TV Episode",
    "purl": "Podcast URL", "egid": "Podcast Episode GUID",
}


class VideoHandler(BaseMetadataHandler):
    NAME = "video"
    EXTENSIONS = _RW_EXTS | _RO_EXTS
    PRIORITY = 25

    def read(self, path: Path) -> MetadataRecord:
        fields: list[MetadataField] = []
        errors: list[str] = []
        ext = path.suffix.lower()
        writable = ext in _RW_EXTS

        if ext in _RW_EXTS:
            fields, errors = _read_mp4(path)
        else:
            fields, errors = _read_hachoir(path)

        record = self._make_record(path, read_errors=errors,
                                   supports_write=writable, supports_delete=writable)
        record.fields = fields
        return record

    def write(self, path: Path, fields: list[MetadataField]) -> None:
        if path.suffix.lower() not in _RW_EXTS:
            raise NotImplementedError(f"Write not supported for {path.suffix}")
        import mutagen.mp4
        mp4 = mutagen.mp4.MP4(path)
        for f in fields:
            if not f.key.startswith("mp4:"):
                continue
            tag_key = f.key[len("mp4:"):]
            mp4[tag_key] = [f.value]
        mp4.save()

    def delete(self, path: Path, keys: list[str]) -> None:
        if path.suffix.lower() not in _RW_EXTS:
            raise NotImplementedError(f"Delete not supported for {path.suffix}")
        import mutagen.mp4
        mp4 = mutagen.mp4.MP4(path)
        for key in keys:
            tag_key = key[len("mp4:"):] if key.startswith("mp4:") else key
            mp4.pop(tag_key, None)
        mp4.save()


def _read_mp4(path: Path) -> tuple[list[MetadataField], list[str]]:
    fields = []
    errors = []
    try:
        import mutagen.mp4
        mp4 = mutagen.mp4.MP4(path)
        if mp4.info:
            fields += [
                MetadataField("video:duration", "Duration (s)", round(mp4.info.length, 2),
                              editable=False, deletable=False, field_type="float", source="video"),
                MetadataField("video:bitrate", "Bitrate (kbps)", mp4.info.bitrate // 1000,
                              editable=False, deletable=False, field_type="int", source="video"),
            ]
        for k, v in (mp4.tags or {}).items():
            label = _MP4_LABELS.get(k, k)
            val = v[0] if isinstance(v, list) and v else v
            if isinstance(val, bytes):
                val = f"<binary {len(val)} bytes>"
            elif not isinstance(val, (str, int, float)):
                val = str(val)
            fields.append(MetadataField(f"mp4:{k}", label, val, source="mp4"))
    except Exception as e:
        errors.append(f"MP4 read error: {e}")
    return fields, errors


def _read_hachoir(path: Path) -> tuple[list[MetadataField], list[str]]:
    fields = []
    errors = []
    try:
        from hachoir.parser import createParser
        from hachoir.metadata import extractMetadata
        parser = createParser(str(path))
        if parser is None:
            errors.append("hachoir could not parse file")
            return fields, errors
        with parser:
            meta = extractMetadata(parser)
        if meta is None:
            return fields, errors
        for item in meta.exportPlaintext():
            if ": " in item:
                label, value = item.split(": ", 1)
                label = label.strip("- ")
                fields.append(MetadataField(
                    f"hachoir:{label.lower().replace(' ', '_')}",
                    label, value.strip(),
                    editable=False, deletable=False, source="hachoir"
                ))
    except Exception as e:
        errors.append(f"hachoir read error: {e}")
    return fields, errors
