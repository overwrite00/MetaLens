"""Audio metadata through mutagen: stream info (``audio:*``) and typed tags.

Tag systems (see ``_tag_codecs`` / ``_mp4_codec``): ID3 ``tag:`` (MP3, WAV, AIFF,
TrueAudio), Vorbis ``tag:`` + ``flac:picture`` (FLAC, Ogg), ASF ``tag:`` (WMA),
APEv2 ``tag:`` (APE, WavPack, Musepack, OptimFROG, TAK) and MP4 ``mp4:`` (M4A,
M4B, M4R — same keys as the video handler). AAC (ADTS/ADIF) and formats mutagen
cannot write are read-only (``format_supports_write`` False).
"""
from __future__ import annotations

from pathlib import Path

from core.base_handler import BaseMetadataHandler
from core.handlers._tag_codecs import (ApeCodec, AsfCodec, Id3Codec, TagMutations,
                                       VorbisCodec)
from core.models import MetadataField, MetadataRecord

_AUDIO_EXTS = frozenset({
    ".mp3", ".flac", ".ogg", ".oga", ".opus",
    ".m4a", ".aac", ".wma", ".wav", ".aiff", ".aif",
    ".ape", ".wv", ".mpc", ".spx", ".tta",
})


def _writable_classes() -> tuple[type, ...]:
    """mutagen classes whose tags MetaLens can write (explicit allow-list)."""
    from mutagen.aiff import AIFF
    from mutagen.asf import ASF
    from mutagen.flac import FLAC
    from mutagen.monkeysaudio import MonkeysAudio
    from mutagen.mp3 import MP3
    from mutagen.mp4 import MP4
    from mutagen.musepack import Musepack
    from mutagen.oggflac import OggFLAC
    from mutagen.oggopus import OggOpus
    from mutagen.oggspeex import OggSpeex
    from mutagen.oggvorbis import OggVorbis
    from mutagen.optimfrog import OptimFROG
    from mutagen.tak import TAK
    from mutagen.trueaudio import TrueAudio
    from mutagen.wave import WAVE
    from mutagen.wavpack import WavPack
    return (MP3, WAVE, AIFF, TrueAudio, FLAC, OggVorbis, OggOpus, OggSpeex, OggFLAC,
            ASF, MP4, MonkeysAudio, WavPack, Musepack, OptimFROG, TAK)


def codec_for(audio):
    """Tag codec for an opened mutagen file, or ``None`` (no supported tag system)."""
    from mutagen._vorbis import VCommentDict
    from mutagen.apev2 import APEv2, APEv2File
    from mutagen.asf import ASF
    from mutagen.flac import FLAC
    from mutagen.id3 import ID3
    from mutagen.mp4 import MP4
    from mutagen.ogg import OggFileType
    from core.handlers._mp4_codec import Mp4Codec
    tags = audio.tags
    if isinstance(audio, MP4):
        return Mp4Codec(audio)
    if isinstance(audio, ASF):
        return AsfCodec(audio)
    if isinstance(tags, ID3):
        return Id3Codec(audio)
    if isinstance(tags, VCommentDict) or (tags is None and isinstance(audio, (FLAC, OggFileType))):
        return VorbisCodec(audio)
    if isinstance(tags, APEv2) or (tags is None and isinstance(audio, APEv2File)):
        return ApeCodec(audio)
    if tags is None and hasattr(audio, "add_tags") and isinstance(audio, _id3_file_classes()):
        return Id3Codec(audio)
    return None


def _id3_file_classes() -> tuple[type, ...]:
    from mutagen.aiff import AIFF
    from mutagen.mp3 import MP3
    from mutagen.trueaudio import TrueAudio
    from mutagen.wave import WAVE
    return (MP3, WAVE, AIFF, TrueAudio)


def stream_fields(info, prefix: str = "audio") -> list[MetadataField]:
    """Computed stream properties: read-only, never deletable."""
    out = []

    def add(key, label, value, field_type):
        out.append(MetadataField(key, label, value, raw_value=value, editable=False,
                                 deletable=False, field_type=field_type, source=prefix))
    length = getattr(info, "length", None)
    if isinstance(length, (int, float)) and length >= 0:
        add(f"{prefix}:duration", "Duration (s)", round(float(length), 3), "duration")
    bitrate = getattr(info, "bitrate", None)
    if isinstance(bitrate, (int, float)) and not isinstance(bitrate, bool) and bitrate > 0:
        add(f"{prefix}:bitrate", "Bitrate (kbps)", int(round(bitrate / 1000)), "int")
    for attr, key, label in (("sample_rate", "sample_rate", "Sample Rate (Hz)"),
                             ("channels", "channels", "Channels"),
                             ("bits_per_sample", "bits_per_sample", "Bits per Sample")):
        v = getattr(info, attr, None)
        if type(v) is int and v > 0:
            add(f"{prefix}:{key}", label, v, "int")
    codec = getattr(info, "codec", None)
    if isinstance(codec, str) and codec:
        add(f"{prefix}:codec", "Codec", codec, "str")
    return out


class AudioHandler(TagMutations, BaseMetadataHandler):
    NAME = "audio"
    EXTENSIONS = _AUDIO_EXTS
    PRIORITY = 20

    def _open_codec(self, path: Path):
        import mutagen
        audio = mutagen.File(path, easy=False)
        if audio is None:
            return None, False
        return codec_for(audio), isinstance(audio, _writable_classes())

    def read(self, path: Path) -> MetadataRecord:
        fields: list[MetadataField] = []
        errors: list[str] = []
        writable = False
        try:
            import mutagen
            audio = mutagen.File(path, easy=False)
            if audio is None:
                errors.append("mutagen could not parse file")
            else:
                fields += stream_fields(audio.info, "audio")
                codec = codec_for(audio)
                writable = codec is not None and isinstance(audio, _writable_classes())
                if codec is not None:
                    tag_fields = codec.fields()
                    if not writable:
                        for f in tag_fields:
                            f.editable = f.deletable = False
                    fields += tag_fields
        except Exception as e:
            errors.append(f"Audio read error: {e}")
            writable = False
        record = self._make_record(path, read_errors=errors,
                                   supports_write=writable and not errors,
                                   supports_delete=writable and not errors)
        if errors:
            for f in fields:
                f.editable = f.deletable = False
        elif not writable:
            record.read_only_reason = f"{path.suffix or 'this'} audio format is read-only"
        record.fields = fields
        return record

    def write(self, path: Path, fields: list[MetadataField]) -> None:
        self._atomic_write(path, lambda tmp: self._write_tags(tmp, fields))

    def _write_tags(self, path: Path, fields: list[MetadataField]) -> None:
        self.apply_write(path, fields)

    def delete(self, path: Path, keys: list[str]) -> None:
        self._atomic_write(path, lambda tmp: self._delete_tags(tmp, keys))

    def _delete_tags(self, path: Path, keys: list[str]) -> None:
        self.apply_delete(path, keys)
