from __future__ import annotations
from pathlib import Path

from core.base_handler import BaseMetadataHandler
from core.models import MetadataField, MetadataRecord

_AUDIO_EXTS = frozenset({
    ".mp3", ".flac", ".ogg", ".oga", ".opus",
    ".m4a", ".aac", ".wma", ".wav", ".aiff", ".aif",
    ".ape", ".wv", ".mpc", ".spx", ".tta",
})

_ID3_LABELS = {
    "TIT2": "Title", "TPE1": "Artist", "TPE2": "Album Artist",
    "TALB": "Album", "TRCK": "Track Number", "TPOS": "Disc Number",
    "TDRC": "Year", "TCON": "Genre", "TCOM": "Composer",
    "TLYC": "Lyrics", "COMM": "Comment", "TBPM": "BPM",
    "TPUB": "Publisher", "TCOP": "Copyright", "TENC": "Encoded By",
    "TLAN": "Language", "TSRC": "ISRC", "TXXX": "Custom Tag",
    "USLT": "Lyrics (unsynced)", "SYLT": "Lyrics (synced)",
    "APIC": "Cover Art", "GEOB": "General Object",
    "PCNT": "Play Count", "POPM": "Popularimeter",
    "TSSE": "Encoding Settings", "TDTG": "Tagging Time",
    "TDOR": "Original Release", "WOAR": "Artist URL", "WOAS": "Source URL",
}


class AudioHandler(BaseMetadataHandler):
    NAME = "audio"
    EXTENSIONS = _AUDIO_EXTS
    PRIORITY = 20

    def read(self, path: Path) -> MetadataRecord:
        fields: list[MetadataField] = []
        errors: list[str] = []
        try:
            import mutagen
            audio = mutagen.File(path, easy=False)
            if audio is None:
                errors.append("mutagen could not parse file")
                record = self._make_record(path, read_errors=errors,
                                           supports_write=False, supports_delete=False)
                record.fields = fields
                return record

            # Stream info
            if hasattr(audio, "info"):
                info = audio.info
                if hasattr(info, "length"):
                    fields.append(MetadataField("audio:duration", "Duration (s)",
                                                round(info.length, 2),
                                                editable=False, deletable=False,
                                                field_type="float", source="audio"))
                if hasattr(info, "bitrate"):
                    fields.append(MetadataField("audio:bitrate", "Bitrate (kbps)",
                                                info.bitrate // 1000,
                                                editable=False, deletable=False,
                                                field_type="int", source="audio"))
                if hasattr(info, "sample_rate"):
                    fields.append(MetadataField("audio:sample_rate", "Sample Rate (Hz)",
                                                info.sample_rate,
                                                editable=False, deletable=False,
                                                field_type="int", source="audio"))
                if hasattr(info, "channels"):
                    fields.append(MetadataField("audio:channels", "Channels",
                                                info.channels,
                                                editable=False, deletable=False,
                                                field_type="int", source="audio"))

            # Tags
            tags = audio.tags
            if tags:
                for key, val in tags.items():
                    clean_key = key.split(":")[0] if ":" in key else key
                    label = _ID3_LABELS.get(clean_key, clean_key)
                    str_val = _tag_to_str(val)
                    if str_val is None:
                        continue
                    fields.append(MetadataField(
                        f"tag:{key}", label, str_val,
                        source=_detect_source(tags)
                    ))

        except Exception as e:
            errors.append(f"Audio read error: {e}")

        record = self._make_record(path, read_errors=errors,
                                   supports_write=True, supports_delete=True)
        record.fields = fields
        return record

    def write(self, path: Path, fields: list[MetadataField]) -> None:
        import mutagen
        import mutagen.id3 as id3_module
        audio = mutagen.File(path, easy=False)
        if audio is None:
            raise RuntimeError("mutagen cannot parse file for writing")
        is_id3 = isinstance(getattr(audio, 'tags', None), id3_module.ID3)
        for f in fields:
            if not f.key.startswith("tag:"):
                continue
            tag_key = f.key[len("tag:"):]
            if is_id3:
                frame_cls = getattr(id3_module, tag_key, None)
                if frame_cls is not None:
                    try:
                        audio.tags[tag_key] = frame_cls(encoding=3, text=[str(f.value)])
                    except TypeError:
                        audio.tags[tag_key] = frame_cls(text=[str(f.value)])
                else:
                    audio.tags[tag_key] = f.value
            else:
                audio[tag_key] = [str(f.value)]
        audio.save()

    def delete(self, path: Path, keys: list[str]) -> None:
        import mutagen
        audio = mutagen.File(path, easy=False)
        if audio is None:
            raise RuntimeError("mutagen cannot parse file for deletion")
        changed = False
        for key in keys:
            tag_key = key[len("tag:"):] if key.startswith("tag:") else key
            if audio.tags and tag_key in audio.tags:
                del audio.tags[tag_key]
                changed = True
        if changed:
            audio.save()


def _tag_to_str(val) -> str | None:
    if isinstance(val, list):
        parts = [_tag_to_str(v) for v in val]
        return ", ".join(p for p in parts if p is not None)
    if hasattr(val, "text"):
        return str(val.text[0]) if val.text else None
    if isinstance(val, bytes):
        return f"<binary {len(val)} bytes>"
    return str(val) if val is not None else None


def _detect_source(tags) -> str:
    t = type(tags).__name__
    if "ID3" in t:
        return "id3"
    if "VComment" in t or "Vorbis" in t:
        return "vorbis"
    if "MP4" in t or "M4A" in t:
        return "mp4"
    if "ASF" in t:
        return "asf"
    return "audio"
