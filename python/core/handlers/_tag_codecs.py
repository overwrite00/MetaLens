"""Typed tag codecs for mutagen-based handlers (plan F2B.1 / F2C).

A codec maps one tag system to ``MetadataField`` values and back, honouring the
value model of ``core.values`` (plan B1):

* multi-valued text is always ``str[]`` (never joined), single text is ``str``;
* binaries (pictures, private data, ...) are read-only ``binary`` descriptors,
  structured frames (POPM, PCNT, SYLT, TIPL) read-only, all of them deletable
  and restorable exactly through ``snapshot``/``restore``;
* keys are ``<namespace>:<native id>`` and never depend on a value (ID3 URL
  frames are grouped by FrameID, PRIV by owner).

``TagMutations`` implements the mutation hooks of ``BaseMetadataHandler`` on
top of a codec: validation of every field before touching the file, write,
save and read-after-write verification (G4). Handlers provide ``_open_codec``.

Undo (``snapshot``/``restore``):

* tag systems whose library writes entries in its own canonical order (ID3
  frames, APEv2 items, MP4 atoms) snapshot the content of a key only;
* ordered containers (Vorbis comments, ASF attributes, FLAC picture blocks)
  snapshot a ``Placed``: the entries plus their positions, and ``restore_many``
  puts all keys of one Undo back together at those positions, so delete + Undo
  keeps the original order;
* an Undo that leaves the tag container empty removes it when the file did
  not have one before, so the first tag added and undone gives the original
  bytes back: ID3 chunk of WAV/AIFF, ID3v2 tag of MP3, APEv2 tag, FLAC Vorbis
  comment block with this mutagen's default vendor string. Whatever mutagen
  cannot undo itself (the FLAC PADDING block, the MP4 ``meta`` atom) is covered by the
  byte patch of the Undo journal (``api/byte_patch.py``), which gives the original
  bytes back when the changed region is still as saved; the value-level restore is
  the fallback (FLAC keeps the PADDING, MP4 an empty ``ilst``). A container that was already there but empty
  before the undone operation cannot be told apart (its snapshot is ABSENT
  too) and is removed as well; no tag value is lost.
* sizes: mutagen's padding policy keeps a tag area that the undone write had
  to enlarge (the space becomes padding), so values are exact but such a file
  is larger than before; delete + Undo, and writes that fit the existing
  padding, give the original bytes back for tags written by mutagen.

Codecs here: ID3 (MP3, WAV, AIFF, TrueAudio), Vorbis comments (FLAC, Ogg
Vorbis/Opus/Speex/FLAC, plus FLAC picture blocks), ASF (WMA) and APEv2
(Monkey's Audio, WavPack, Musepack, OptimFROG, TAK). MP4 lives in ``_mp4_codec``.

Dates: only ID3 time stamp frames have a format imposed by the standard
(``id3_timestamp_error``: ISO 8601 subset with calendar ranges). ID3v2.3 has no
time stamp frames: TDRC is stored as TYER/TDAT/TIME (year, day and month,
hours and minutes) and TDOR as TORY (year); the conversion is exact for every
value and what those frames cannot hold is rejected before writing
(``_v23_date_problem``). Vorbis ``DATE``, APEv2 ``Year``, ASF ``WM/Year`` and MP4
``©day``/``purd`` are free text in their specifications (a year, an ISO date,
"Summer 2020"...): they are not rejected.
"""
from __future__ import annotations

import base64
import calendar
import copy
import json
import re
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core import values
from core.base_handler import ABSENT
from core.errors import ErrorCode, MetadataValueError, MetadataValueErrors
from core.models import MetadataField

__all__ = [
    "TagCodec", "TagMutations", "Id3Codec", "VorbisCodec", "AsfCodec", "ApeCodec",
    "Placed", "check_text", "codec_for_source", "id3_timestamp_error",
]

# ──────────────────────── shared helpers ─────────────────────────────────────


def _err(key: str | None, code: str, message: str) -> MetadataValueError:
    return MetadataValueError(key, code, f"{key}: {message}" if key else message)


def check_text(key: str, text: str, *, latin1: bool = False) -> str:
    """Format-independent limits of a tag text item (non-empty already checked).

    NUL is the multi-value separator of ID3/APEv2 and a terminator in ASF, and
    lone surrogates cannot be encoded: both would be silently altered on save.
    """
    if "\x00" in text:
        raise _err(key, ErrorCode.INVALID_VALUE, "text must not contain NUL characters")
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        raise _err(key, ErrorCode.INVALID_VALUE, "text contains unencodable characters") from None
    if latin1:
        try:
            text.encode("latin-1")
        except UnicodeEncodeError:
            raise _err(key, ErrorCode.UNSUPPORTED,
                       "this frame stores ISO-8859-1 only (URLs must be ASCII/Latin-1)") from None
    return text


def _items(value: Any) -> list:
    return value if isinstance(value, list) else [value]


def _canon(v: Any) -> str:
    return json.dumps(v, sort_keys=True, ensure_ascii=False)


def _text_field(key: str, label: str, items: list[str], source: str, *,
                multi: bool = True, field_type: str = "str",
                editable: bool = True) -> MetadataField:
    """Text field; empty or NUL-bearing stored values cannot be re-written as read."""
    items = [str(i) for i in items]
    clean = bool(items) and all(i and "\x00" not in i for i in items)
    if multi:
        value: Any = items if items else [""]
    else:
        value = items[0] if items else ""
    return MetadataField(key, label, value, raw_value=copy.deepcopy(value),
                         editable=editable and clean, deletable=True,
                         field_type=field_type, source=source, multi=multi)


def _schema(key: str, label: str, source: str, *, field_type: str = "str",
            multi: bool = True, length: int | None = None) -> MetadataField:
    """Schema of an addable/dynamic field (value is filled by the caller)."""
    return MetadataField(key, label, None, editable=True, deletable=True,
                         field_type=field_type, source=source, multi=multi, length=length)


def _validate_generic(field: MetadataField, value: Any) -> Any:
    v = values.canonicalize(field, value, allow_empty=False)
    if field.field_type in ("str", "text"):
        for item in _items(v):
            check_text(field.key, item)
    return v


@dataclass(frozen=True, eq=False)
class Placed:
    """Raw pre-image of a key whose entries sit in an ordered container.

    ``items`` is the exact content: what ``restore`` writes back and what ``==``
    compares. ``positions[i]`` (ascending) is where ``items[i]`` was: index in the
    Vorbis comment list, in the ASF attribute list, among the non-padding FLAC
    metadata blocks. The positions are a restore HINT, not part of the value:

    * ``/undo`` answers 409 only when a key no longer holds its post-value, so
      entries of the key moved by changes to OTHER keys (another program) must not
      make it differ;
    * when the container changed so much that a position no longer exists, the
      entry goes to the nearest valid place (the end) instead of failing the
      exact-restore check. With an unchanged container the order is identical.
    """
    items: tuple
    positions: tuple

    @classmethod
    def of(cls, found: list[tuple[int, Any]]) -> "Placed":
        """From ``[(position, item), ...]`` in container order."""
        return cls(tuple(item for _, item in found), tuple(pos for pos, _ in found))

    def placed(self) -> list[tuple[int, Any]]:
        return list(zip(self.positions, self.items))

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Placed):
            return NotImplemented
        return self.items == other.items

    def __hash__(self) -> int:
        return hash(self.items)


def _insert_placed(entries: list, inserts: list[tuple[int, Any]]) -> None:
    """Insert ``(position, entry)`` pairs at their positions, lowest first.

    Every other key of the Undo is removed first, so with the rest of the
    container unchanged each entry lands exactly at its position; a position past
    the end appends (best effort when the container changed meanwhile).
    """
    for position, entry in sorted(inserts, key=lambda pair: pair[0]):
        entries.insert(position, entry)


# ──────────────────────── codec base ─────────────────────────────────────────

class TagCodec:
    """One tag system of one opened mutagen file object."""
    SOURCE = ""
    PREFIXES: tuple[str, ...] = ("tag:",)

    def __init__(self, audio) -> None:
        self.audio = audio

    @property
    def tags(self):
        return self.audio.tags

    def owns(self, key: str) -> bool:
        return key.startswith(self.PREFIXES)

    def ensure_tags(self) -> None:
        if self.audio.tags is None:
            self.audio.add_tags()

    # read
    def fields(self) -> list[MetadataField]:
        raise NotImplementedError

    def dynamic_schema(self, key: str) -> MetadataField | None:
        """Schema of a valid key absent from the file (``None`` = not addable)."""
        return None

    def addable(self, present: set[str]) -> list[MetadataField]:
        return []

    # validation: instance level may use the file (e.g. ASF attribute classes)
    def validate(self, field: MetadataField, value: Any) -> Any:
        return self.static_validate(field, value)

    @classmethod
    def static_validate(cls, field: MetadataField, value: Any) -> Any:
        return _validate_generic(field, value)

    # mutation
    def write(self, field: MetadataField, value: Any) -> None:
        raise NotImplementedError

    def delete(self, key: str) -> None:
        raise NotImplementedError

    def snapshot(self, key: str) -> Any:
        raise NotImplementedError

    def restore(self, key: str, raw: Any) -> None:
        raise NotImplementedError

    def restore_many(self, snap: dict[str, Any]) -> None:
        """Put every key of one Undo back in memory (``ABSENT`` = remove it).

        Default: key by key, for tag systems whose library writes the entries in
        its own canonical order (ID3 frames, APEv2 items, MP4 atoms). Ordered
        containers override it to restore all keys together at their positions.
        """
        if any(raw is not ABSENT for raw in snap.values()):
            self.ensure_tags()
        for key, raw in snap.items():
            if raw is ABSENT:
                self.delete(key)
                continue
            try:
                self.restore(key, raw)
            except MetadataValueError:
                raise
            except _VALUE_EXCEPTIONS as exc:
                raise _err(key, ErrorCode.INVALID_VALUE, f"cannot restore: {exc}") from exc

    def container_removable(self) -> bool:
        """True when the tag container is empty and a file without it is the
        original state (see the module docstring). Default: never."""
        return False

    def remove_container(self) -> None:
        """Remove the (empty) tag container from the file instead of saving it."""
        raise NotImplementedError

    def save(self) -> None:
        self.audio.save()


# ──────────────────────── ID3 ────────────────────────────────────────────────

ID3_LABELS = {
    "TIT1": "Grouping", "TIT2": "Title", "TIT3": "Subtitle", "TPE1": "Artist",
    "TPE2": "Album Artist", "TPE3": "Conductor", "TPE4": "Remixer", "TALB": "Album",
    "TRCK": "Track Number", "TPOS": "Disc Number", "TDRC": "Recording Date",
    "TDRL": "Release Date", "TDOR": "Original Release", "TDTG": "Tagging Time",
    "TDEN": "Encoding Time", "TYER": "Year", "TCON": "Genre", "TCOM": "Composer",
    "TEXT": "Lyricist", "TBPM": "BPM", "TKEY": "Initial Key", "TLAN": "Language",
    "TPUB": "Publisher", "TCOP": "Copyright", "TENC": "Encoded By", "TSRC": "ISRC",
    "TSSE": "Encoding Settings", "TLEN": "Length (ms)", "TMOO": "Mood",
    "TMED": "Media Type", "TOAL": "Original Album", "TOPE": "Original Artist",
    "TSOA": "Album Sort Order", "TSOP": "Artist Sort Order", "TSOT": "Title Sort Order",
    "TSO2": "Album Artist Sort Order", "TCMP": "Compilation", "TXXX": "Custom Text",
    "COMM": "Comment", "USLT": "Lyrics (unsynced)", "SYLT": "Lyrics (synced)",
    "APIC": "Picture", "GEOB": "General Object", "PRIV": "Private Data",
    "UFID": "Unique File ID", "PCNT": "Play Count", "POPM": "Popularimeter (rating, count)",
    "TIPL": "Involved People", "TMCL": "Musician Credits", "IPLS": "Involved People",
    "WCOM": "Commercial URL", "WCOP": "Copyright URL", "WOAF": "Audio File URL",
    "WOAR": "Artist URL", "WOAS": "Source URL", "WORS": "Radio Station URL",
    "WPAY": "Payment URL", "WPUB": "Publisher URL", "WXXX": "Custom URL",
    "MCDI": "Music CD ID", "RVA2": "Relative Volume", "CHAP": "Chapter",
    "CTOC": "Table of Contents",
}

#: Frames whose mutagen HashKey is value-independent (besides text frames).
_ID3_STABLE_HASHKEY = frozenset({
    "WXXX", "USLT", "SYLT", "APIC", "GEOB", "UFID", "POPM", "RVA2", "EQU2",
    "CHAP", "CTOC", "AENC", "ENCR",
})
#: v2.4-only frames dropped by mutagen's update_to_v23 (TDRC/TDOR are converted).
_ID3_V24_ONLY = frozenset({
    "ASPI", "EQU2", "RVA2", "SEEK", "SIGN", "TDEN", "TDRL", "TDTG", "TIPL",
    "TMCL", "TMOO", "TPRO", "TSOA", "TSOP", "TSOT", "TSST",
})
#: v2.4 frames mutagen's update_to_v23 deletes without converting them. iTunes & co.
#: write some of them (TSOP, TSOA, TSOT) into v2.3 tags: when the file has them they
#: are kept (off spec, as found), otherwise every save lost them (G4: 422 for any
#: write). New ones are still not offered for v2.3 files (``_ID3_V24_ONLY``).
_ID3_V24_KEPT_IN_V23 = _ID3_V24_ONLY - {"TIPL", "TMCL"}   # those two become IPLS
#: v2.3 frames mutagen converts (TYER/TDAT/TIME -> TDRC, TORY -> TDOR, IPLS -> TIPL)
#: or drops (TRDA, TSIZ, RVAD, EQUA) whenever it loads a tag: written under their own
#: key they could never be read back (every write was a lossy_write), so they are
#: not offered; dates go through TDRC/TDOR, which mutagen converts for v2.3 files.
_ID3_LOAD_TRANSLATED = frozenset({"TYER", "TDAT", "TIME", "TORY", "TRDA", "TSIZ", "IPLS",
                                  "RVAD", "EQUA"})
_ID3_ADDABLE = ("TIT2", "TPE1", "TALB", "TPE2", "TRCK", "TPOS", "TDRC", "TCON",
                "TCOM", "TCOP", "TPUB", "TBPM", "TSRC", "TENC", "COMM::eng", "USLT::eng")
_ID3_TIMESTAMP = re.compile(
    r"(\d{4})(?:-(\d{2})(?:-(\d{2})(?:T(\d{2})(?::(\d{2})(?::(\d{2}))?)?)?)?)?", re.ASCII)
_ID3_LANG = re.compile(r"[A-Za-z]{3}", re.ASCII)


def id3_timestamp_error(text: str) -> str | None:
    """Why ``text`` is not an ID3v2.4 time stamp, or ``None`` when it is one.

    ID3v2.4 (main structure, section 4) allows ``yyyy``, ``yyyy-MM``, ``yyyy-MM-dd``,
    ``yyyy-MM-ddTHH``, ``yyyy-MM-ddTHH:mm`` and ``yyyy-MM-ddTHH:mm:ss`` (a subset of
    ISO 8601); every component must exist: month 01-12, a day of that month (leap
    years of the proleptic Gregorian calendar), hour 00-23, minute and second 00-59.
    """
    match = _ID3_TIMESTAMP.fullmatch(text)
    if match is None:
        return "not an ID3 timestamp (YYYY[-MM[-DD[THH[:MM[:SS]]]]])"
    year, month, day, hour, minute, second = (None if g is None else int(g) for g in match.groups())
    if month is not None and not 1 <= month <= 12:
        return f"month {month:02d} does not exist (01-12)"
    if day is not None:
        last = calendar.monthrange(2000 + year % 400, month)[1]   # same leap years, any year
        if not 1 <= day <= last:
            return f"day {day:02d} does not exist in {year:04d}-{month:02d} (01-{last:02d})"
    if hour is not None and hour > 23:
        return f"hour {hour:02d} does not exist (00-23)"
    if minute is not None and minute > 59:
        return f"minute {minute:02d} does not exist (00-59)"
    if second is not None and second > 59:
        return f"second {second:02d} does not exist (00-59)"
    return None


def _stamp_parts(text: str) -> tuple | None:
    """(year, month, day, hour, minute, second), ``None`` for absent parts."""
    match = _ID3_TIMESTAMP.fullmatch(text.replace(" ", "T"))
    return None if match is None else tuple(None if g is None else int(g) for g in match.groups())


def _v23_date_problem(fid: str, stamps: list[str]) -> str | None:
    """Why ID3v2.3 cannot hold ``stamps`` exactly (``None`` when it can).

    v2.3 has no time stamp frames: TDRC is stored as TYER (year), TDAT (DDMM) and
    TIME (HHMM), read back as ``YYYY``, ``YYYY-MM-DD`` or ``YYYY-MM-DDTHH:MM:00`` with
    the values of the three frames paired by position; TDOR becomes TORY (a year).
    """
    parts = [_stamp_parts(s) for s in stamps]
    if fid == "TDOR":
        if len(parts) != 1 or parts[0] is None or parts[0][1] is not None:
            return "ID3v2.3 stores the original release year only (TORY): use one YYYY value"
        return None
    for p in parts:
        if p is None:
            return "not an ID3 timestamp"
        if p[1] is not None and p[2] is None:
            return "ID3v2.3 stores a date as day and month (TDAT): use YYYY or YYYY-MM-DD"
        if p[3] is not None and (p[4] is None or p[5] != 0):
            return "ID3v2.3 stores hours and minutes only (TIME): use YYYY-MM-DDTHH:MM:00"
    dated = [p[2] is not None for p in parts]
    timed = [p[3] is not None for p in parts]
    if dated != sorted(dated, reverse=True) or timed != sorted(timed, reverse=True):
        return ("ID3v2.3 pairs the year, date and time values by position: "
                "list the values with a date (and time) first")
    return None


def _id3():
    import mutagen.id3 as id3
    return id3


def _id3_stable_id(hashkey: str, frame) -> str:
    id3 = _id3()
    fid = frame.FrameID
    if isinstance(frame, id3.UrlFrame) and fid != "WXXX":
        return fid
    if fid == "PRIV":
        return f"PRIV:{frame.owner}"
    if isinstance(frame, id3.TextFrame) or fid in _ID3_STABLE_HASHKEY:
        return hashkey
    return fid


def _id3_norm(value: Any) -> Any:
    from mutagen.id3 import ID3Tags
    if isinstance(value, ID3Tags):
        return ("__frames__", tuple(_id3_frame_repr(f) for _, f in sorted(value.items())))
    return copy.deepcopy(value)


def _id3_denorm(value: Any) -> Any:
    from mutagen.id3 import ID3Tags
    if isinstance(value, tuple) and len(value) == 2 and value[0] == "__frames__":
        tags = ID3Tags()
        for rep in value[1]:
            tags.add(_id3_frame_from_repr(rep))
        return tags
    return copy.deepcopy(value)


def _id3_frame_repr(frame) -> tuple:
    specs = [*frame._framespec, *frame._optionalspec]
    return (type(frame).__name__,
            tuple((s.name, _id3_norm(frame.__dict__[s.name])) for s in specs
                  if s.name in frame.__dict__))


def _id3_frame_from_repr(rep: tuple):
    id3 = _id3()
    name, attrs = rep
    cls = id3.Frames.get(name) or id3.Frames_2_2.get(name)
    return cls(**{k: _id3_denorm(v) for k, v in attrs})


def _id3_ts(stamp) -> str:
    """ID3v2.4 timestamp as stored on disk ("YYYY-MM-DDTHH:MM:SS", partial allowed)."""
    return str(stamp).replace(" ", "T")


def _latin1(text: str) -> bool:
    try:
        text.encode("latin-1")
        return True
    except UnicodeEncodeError:
        return False


class Id3Codec(TagCodec):
    """ID3v2 frames under ``tag:<id>``; saved with the file's own ID3 version (D14)."""
    SOURCE = "id3"

    def __init__(self, audio) -> None:
        super().__init__(audio)
        version = getattr(audio.tags, "version", (2, 4, 0)) if audio.tags is not None else (2, 4, 0)
        # mutagen writes only v2.3/v2.4; v2.2 tags are saved as v2.3.
        self.v2_version = 4 if version[1] >= 4 else 3

    @property
    def v23(self) -> bool:
        return self.v2_version == 3

    def _groups(self) -> dict[str, list]:
        groups: dict[str, list] = {}
        if self.tags is None:
            return groups
        for hashkey, frame in sorted(self.tags.items()):
            groups.setdefault(_id3_stable_id(hashkey, frame), []).append((hashkey, frame))
        return groups

    @staticmethod
    def _label(sid: str) -> str:
        fid, _, rest = sid.partition(":")
        base = ID3_LABELS.get(fid, fid)
        return f"{base} ({rest})" if rest else base

    def fields(self) -> list[MetadataField]:
        id3 = _id3()
        out = []
        for sid, frames in self._groups().items():
            key, label = f"tag:{sid}", self._label(sid)
            frame = frames[0][1]
            fid = frame.FrameID
            if isinstance(frame, id3.TimeStampTextFrame):
                # A date another tool stored with impossible components (13th month)
                # is shown as is but cannot be written back as read: read-only (G1).
                stamps = [_id3_ts(t) for t in frame.text]
                valid = all(id3_timestamp_error(s) is None for s in stamps)
                out.append(_text_field(key, label, stamps, self.SOURCE, editable=valid))
            elif isinstance(frame, id3.TextFrame):
                out.append(_text_field(key, label, [str(t) for t in frame.text], self.SOURCE))
            elif fid == "USLT":
                out.append(_text_field(key, label, [frame.text], self.SOURCE,
                                       multi=False, field_type="text"))
            elif fid == "WXXX":
                out.append(_text_field(key, label, [frame.url], self.SOURCE, multi=False))
            elif isinstance(frame, id3.UrlFrame):
                urls = [f.url for _, f in frames]
                out.append(_text_field(key, label, urls, self.SOURCE,
                                       multi=isinstance(frame, id3.UrlFrameU)))
            elif fid in ("APIC", "GEOB"):
                out.append(MetadataField.binary_field(key, label, frame.data, mime=frame.mime or None,
                                                      source=self.SOURCE, raw_value=frame.data))
            elif fid == "UFID":
                out.append(MetadataField.binary_field(key, label, frame.data, source=self.SOURCE))
            elif fid == "PRIV":
                out.append(MetadataField.binary_field(key, label, [f.data for _, f in frames],
                                                      source=self.SOURCE))
            elif fid == "POPM":
                value = [int(frame.rating), int(frame.__dict__.get("count", 0))]
                out.append(MetadataField(key, label, value, raw_value=list(value), editable=False,
                                         field_type="int", source=self.SOURCE, multi=True, length=2))
            elif fid == "PCNT":
                out.append(MetadataField(key, label, int(frame.count), raw_value=int(frame.count),
                                         editable=False, field_type="int", source=self.SOURCE))
            elif fid == "SYLT":
                lines = [f"{time}: {text}" for text, time in frame.text]
                out.append(_text_field(key, label, lines, self.SOURCE, editable=False))
            elif isinstance(frame, id3.PairedTextFrame):
                people = [f"{role}: {name}" for role, name in frame.people]
                out.append(_text_field(key, label, people, self.SOURCE, editable=False))
            else:
                payloads = []
                for _, f in frames:
                    try:
                        payloads.append(bytes(f._writeData()))
                    except Exception:  # display only; restore uses the frame snapshot
                        payloads.append(repr(f).encode("utf-8", "replace"))
                out.append(MetadataField.binary_field(key, label, payloads, source=self.SOURCE))
        return out

    def dynamic_schema(self, key: str) -> MetadataField | None:
        id3 = _id3()
        if not key.startswith("tag:"):
            return None
        nid = key[4:]
        fid, sep, rest = nid.partition(":")
        cls = id3.Frames.get(fid)
        if cls is None or fid in _ID3_LOAD_TRANSLATED or (self.v23 and fid in _ID3_V24_ONLY):
            return None
        if sep and ("\x00" in rest or not _encodable(rest)):
            return None
        label = self._label(nid)
        if fid == "TXXX":
            return _schema(key, label, self.SOURCE) if sep else None
        if fid in ("COMM", "USLT"):
            desc, sep2, lang = rest.rpartition(":")
            if not sep or not sep2 or not _ID3_LANG.fullmatch(lang):
                return None
            if fid == "USLT":
                return _schema(key, label, self.SOURCE, field_type="text", multi=False)
            return _schema(key, label, self.SOURCE)
        if fid == "WXXX":
            return _schema(key, label, self.SOURCE, multi=False) if sep else None
        if sep:
            return None
        if issubclass(cls, id3.TextFrame):
            return _schema(key, label, self.SOURCE)
        if issubclass(cls, id3.UrlFrame):
            return _schema(key, label, self.SOURCE, multi=issubclass(cls, id3.UrlFrameU))
        return None

    def addable(self, present: set[str]) -> list[MetadataField]:
        out = []
        for nid in _ID3_ADDABLE:
            key = f"tag:{nid}"
            if key not in present:
                schema = self.dynamic_schema(key)
                if schema is not None:
                    out.append(schema)
        return out

    @classmethod
    def static_validate(cls, field: MetadataField, value: Any) -> Any:
        id3 = _id3()
        v = _validate_generic(field, value)
        fid = field.key[4:].partition(":")[0] if field.key.startswith("tag:") else ""
        frame_cls = id3.Frames.get(fid)
        if frame_cls is not None and issubclass(frame_cls, id3.UrlFrame):
            for item in _items(v):
                check_text(field.key, item, latin1=True)
            if isinstance(v, list) and len(set(v)) != len(v):
                # ID3v2.4: several WOAR/WCOM frames, "but not with the same content"
                raise _err(field.key, ErrorCode.INVALID_VALUE, "the same URL is listed twice")
        if frame_cls is not None and issubclass(frame_cls, id3.TimeStampTextFrame):
            out = []
            for item in _items(v):
                text = item.strip().replace(" ", "T")
                problem = id3_timestamp_error(text)
                if problem is None and _id3_ts(id3.ID3TimeStamp(text)) != text:
                    problem = "not stored exactly by the ID3 timestamp format"
                if problem is not None:
                    raise _err(field.key, ErrorCode.INVALID_VALUE, f"{item!r}: {problem}")
                out.append(text)
            return out
        return v

    def validate(self, field: MetadataField, value: Any) -> Any:
        v = self.static_validate(field, value)
        fid = field.key[4:].partition(":")[0] if field.key.startswith("tag:") else ""
        if self.v23 and fid in ("TDRC", "TDOR"):
            problem = _v23_date_problem(fid, _items(v))
            if problem is not None:
                raise _err(field.key, ErrorCode.UNSUPPORTED, f"{v!r}: {problem}")
        return v

    def _new_encoding(self):
        id3 = _id3()
        return id3.Encoding.UTF16 if self.v23 else id3.Encoding.UTF8

    def _fit_encoding(self, frame, texts: list[str]) -> None:
        """N4: upgrade a Latin-1 frame when the new text does not fit ISO-8859-1."""
        id3 = _id3()
        if frame.encoding == id3.Encoding.LATIN1 and not all(_latin1(t) for t in texts):
            frame.encoding = self._new_encoding()

    def write(self, field: MetadataField, value: Any) -> None:
        id3 = _id3()
        tags = self.tags
        nid = field.key[4:]
        fid, _, rest = nid.partition(":")
        cls = id3.Frames[fid]
        enc = self._new_encoding()
        if issubclass(cls, id3.UrlFrame) and fid != "WXXX":
            tags.delall(fid)
            for url in _items(value):
                tags.add(cls(url=url))
            return
        existing = tags.get(nid)
        frame = copy.deepcopy(existing) if existing is not None else None
        if fid == "WXXX":
            frame = frame or cls(encoding=enc, desc=rest, url="")
            frame.url = value
            self._fit_encoding(frame, [frame.desc])
        elif fid == "USLT":
            desc, _, lang = rest.rpartition(":")
            frame = frame or cls(encoding=enc, lang=lang, desc=desc, text="")
            frame.text = value
            self._fit_encoding(frame, [frame.desc, value])
        else:
            if frame is None:
                if fid == "TXXX":
                    frame = cls(encoding=enc, desc=rest, text=[])
                elif fid == "COMM":
                    desc, _, lang = rest.rpartition(":")
                    frame = cls(encoding=enc, lang=lang, desc=desc, text=[])
                else:
                    frame = cls(encoding=enc, text=[])
            frame.text = list(value)
            self._fit_encoding(frame, [*value, getattr(frame, "desc", "")])
        tags.add(frame)

    def _matching(self, key: str) -> list[tuple[str, Any]]:
        if self.tags is None or not key.startswith("tag:"):
            return []
        sid = key[4:]
        return [(hk, f) for hk, f in sorted(self.tags.items()) if _id3_stable_id(hk, f) == sid]

    def delete(self, key: str) -> None:
        for hashkey, _ in self._matching(key):
            del self.tags[hashkey]

    def snapshot(self, key: str) -> Any:
        frames = self._matching(key)
        return tuple(_id3_frame_repr(f) for _, f in frames) if frames else ABSENT

    def restore(self, key: str, raw: Any) -> None:
        self.delete(key)
        for rep in raw:
            self.tags.add(_id3_frame_from_repr(rep))

    def save(self) -> None:
        if self.v23:
            tags = self.audio.tags
            kept = [f for fid in sorted(_ID3_V24_KEPT_IN_V23) for f in tags.getall(fid)]
            self._dates_to_v23()
            tags.update_to_v23()
            for frame in kept:
                tags.add(frame)
        # v23_sep=None keeps multi-valued text as NUL-separated values instead of
        # joining it with "/" (which would not read back as the same list).
        self.audio.save(v2_version=self.v2_version, v23_sep=None)

    def _dates_to_v23(self) -> None:
        """TDRC -> TYER/TDAT/TIME exactly as mutagen reads them back (every value,
        paired by position). mutagen's own update_to_v23 keeps only the first value
        and drops TIME when the hour or the minute is 0."""
        id3 = _id3()
        tags = self.audio.tags
        if "TDRC" not in tags or any(fid in tags for fid in ("TYER", "TDAT", "TIME")):
            return
        frame = tags.pop("TDRC")
        parts = [p for p in (_stamp_parts(str(s)) for s in frame.text) if p is not None]
        for cls, items in ((id3.TYER, [f"{p[0]:04d}" for p in parts]),
                           (id3.TDAT, [f"{p[2]:02d}{p[1]:02d}" for p in parts if p[2] is not None]),
                           (id3.TIME, [f"{p[3]:02d}{p[4]:02d}" for p in parts if p[4] is not None])):
            if items:
                tags.add(cls(encoding=frame.encoding, text=items))

    def container_removable(self) -> bool:
        tags = self.tags
        return tags is not None and len(tags) == 0 and not getattr(tags, "unknown_frames", None)

    def remove_container(self) -> None:
        """WAV/AIFF: drop the ``id3 ``/``ID3 `` chunk. MP3/TrueAudio: drop the ID3v2 tag
        only; a pre-existing ID3v1 tag stays, first re-rendered from the (now empty)
        frames by a normal save so that it does not keep the undone values."""
        from mutagen.aiff import AIFF
        from mutagen.wave import WAVE
        name = self.audio.filename
        if isinstance(self.audio, (WAVE, AIFF)):
            self.audio.tags.delete(name)
            return
        self.save()
        self.audio.tags.delete(name, delete_v1=False, delete_v2=True)


def _encodable(text: str) -> bool:
    try:
        text.encode("utf-8")
        return True
    except UnicodeEncodeError:
        return False


# ──────────────────────── Vorbis comments ────────────────────────────────────

VORBIS_LABELS = {
    "title": "Title", "artist": "Artist", "album": "Album", "albumartist": "Album Artist",
    "tracknumber": "Track Number", "tracktotal": "Track Total", "discnumber": "Disc Number",
    "date": "Date", "genre": "Genre", "composer": "Composer", "comment": "Comment",
    "description": "Description", "copyright": "Copyright", "license": "License",
    "organization": "Organization", "performer": "Performer", "isrc": "ISRC",
    "lyrics": "Lyrics", "encoder": "Encoder", "metadata_block_picture": "Picture",
}
_VORBIS_ADDABLE = ("title", "artist", "album", "albumartist", "tracknumber", "discnumber",
                   "date", "genre", "composer", "comment")
_PICTURE_KEY = "metadata_block_picture"


class VorbisCodec(TagCodec):
    """Vorbis comments ``tag:<lowercase name>`` (str[]); FLAC picture blocks ``flac:picture``."""
    SOURCE = "vorbis"
    PREFIXES = ("tag:", "flac:")

    def __init__(self, audio) -> None:
        super().__init__(audio)
        from mutagen.flac import FLAC
        self.is_flac = isinstance(audio, FLAC)

    def _pairs(self) -> list[tuple[str, str]]:
        return list(list.__iter__(self.tags)) if self.tags is not None else []

    def _replace(self, name: str, new_pairs: list[tuple[str, str]]) -> None:
        """Replace the pairs of ``name`` in place (position of the first one kept)."""
        pairs = self._pairs()
        first = next((i for i, (k, _) in enumerate(pairs) if k.lower() == name), len(pairs))
        kept = [(k, v) for k, v in pairs if k.lower() != name]
        idx = sum(1 for k, _ in pairs[:first] if k.lower() != name)
        kept[idx:idx] = new_pairs
        list.clear(self.tags)
        list.extend(self.tags, kept)

    def fields(self) -> list[MetadataField]:
        groups: dict[str, list[str]] = {}
        for k, v in self._pairs():
            groups.setdefault(k.lower(), []).append(v)
        out = []
        for name, vals in groups.items():
            key, label = f"tag:{name}", VORBIS_LABELS.get(name, name.upper())
            if name == _PICTURE_KEY:
                pictures = _decode_pictures(vals)
                if pictures is not None:
                    data = [p.data for p in pictures]
                    mime = pictures[0].mime or None if len({p.mime for p in pictures}) == 1 else None
                    out.append(MetadataField.binary_field(key, label, data, mime=mime,
                                                          source=self.SOURCE))
                    continue
                out.append(_text_field(key, label, vals, self.SOURCE, editable=False))
                continue
            out.append(_text_field(key, label, vals, self.SOURCE))
        if self.is_flac and self.audio.pictures:
            pics = self.audio.pictures
            mime = pics[0].mime or None if len({p.mime for p in pics}) == 1 else None
            out.append(MetadataField.binary_field("flac:picture", "Picture", [p.data for p in pics],
                                                  mime=mime, source="flac"))
        return out

    def dynamic_schema(self, key: str) -> MetadataField | None:
        from mutagen._vorbis import is_valid_key
        if not key.startswith("tag:"):
            return None
        name = key[4:]
        if not name or name != name.lower() or not is_valid_key(name) or name == _PICTURE_KEY:
            return None
        return _schema(key, VORBIS_LABELS.get(name, name.upper()), self.SOURCE)

    def addable(self, present: set[str]) -> list[MetadataField]:
        return [self.dynamic_schema(f"tag:{n}") for n in _VORBIS_ADDABLE if f"tag:{n}" not in present]

    def write(self, field: MetadataField, value: Any) -> None:
        name = field.key[4:]
        case = next((k for k, _ in self._pairs() if k.lower() == name), name.upper())
        self._replace(name, [(case, v) for v in value])

    def delete(self, key: str) -> None:
        if key == "flac:picture":
            self.audio.clear_pictures()
        elif key.startswith("tag:"):
            self._replace(key[4:], [])

    def _blocks(self) -> list:
        """FLAC metadata blocks except PADDING (mutagen always rewrites padding last)."""
        from mutagen.flac import Padding
        return [b for b in self.audio.metadata_blocks if b.code != Padding.code]

    def snapshot(self, key: str) -> Any:
        """``Placed``: comment pairs with their index in the comment list, picture
        blocks (raw block data) with their index among the non-padding blocks."""
        if key == "flac:picture":
            if not self.is_flac:
                return ABSENT
            from mutagen.flac import Picture
            found = [(i, bytes(b.write())) for i, b in enumerate(self._blocks())
                     if b.code == Picture.code]
            return Placed.of(found) if found else ABSENT
        if not key.startswith("tag:"):
            return ABSENT
        name = key[4:]
        found = [(i, (k, v)) for i, (k, v) in enumerate(self._pairs()) if k.lower() == name]
        return Placed.of(found) if found else ABSENT

    def restore(self, key: str, raw: Any) -> None:
        self.restore_many({key: raw})

    def restore_many(self, snap: dict[str, Any]) -> None:
        comments = {key[4:]: raw for key, raw in snap.items() if key.startswith("tag:")}
        if comments:
            inserts = [(pos, (str(k), str(v))) for raw in comments.values() if raw is not ABSENT
                       for pos, (k, v) in raw.placed()]
            if inserts:
                self.ensure_tags()
            if self.tags is not None:
                kept = [(k, v) for k, v in self._pairs() if k.lower() not in comments]
                _insert_placed(kept, inserts)
                list.clear(self.tags)
                list.extend(self.tags, kept)
        if "flac:picture" in snap and self.is_flac:
            self._restore_pictures(snap["flac:picture"])

    def _restore_pictures(self, raw: Any) -> None:
        from mutagen.flac import Padding, Picture
        blocks = self.audio.metadata_blocks
        padding = [b for b in blocks if b.code == Padding.code]
        others = [b for b in blocks if b.code not in (Padding.code, Picture.code)]
        if raw is not ABSENT:
            _insert_placed(others, [(pos, Picture(data)) for pos, data in raw.placed()])
        self.audio.metadata_blocks = others + padding

    def container_removable(self) -> bool:
        """FLAC only (the Ogg comment header is mandatory), and only an empty block
        written by this mutagen (its default vendor string): an encoder's empty block
        keeps its vendor string."""
        from mutagen._vorbis import VComment
        tags = self.tags
        return (self.is_flac and tags is not None and len(tags) == 0
                and tags.vendor == VComment.vendor)

    def remove_container(self) -> None:
        tags = self.audio.tags
        self.audio.metadata_blocks = [b for b in self.audio.metadata_blocks if b is not tags]
        self.audio.tags = None
        self.audio.save()


def _decode_pictures(vals: list[str]):
    from mutagen.flac import Picture
    try:
        return [Picture(base64.b64decode(v.encode("ascii"), validate=True)) for v in vals]
    except Exception:
        return None


# ──────────────────────── ASF (WMA) ──────────────────────────────────────────

def _asf():
    import mutagen.asf as asf
    return asf


def _asf_int_range(attr_cls) -> tuple[int, int] | None:
    asf = _asf()
    return {asf.ASFWordAttribute: (0, 2**16 - 1), asf.ASFDWordAttribute: (0, 2**32 - 1),
            asf.ASFQWordAttribute: (0, 2**64 - 1)}.get(attr_cls)


def _wm_picture_mime(data: bytes) -> str | None:
    """MIME of a WM/Picture blob: type(1) size(4) mime(UTF-16LE, NUL-terminated) ..."""
    try:
        raw = data[5:]
        end = next(i for i in range(0, len(raw) - 1, 2) if raw[i:i + 2] == b"\x00\x00")
        return raw[:end].decode("utf-16-le") or None
    except Exception:
        return None


class AsfCodec(TagCodec):
    """ASF attributes ``tag:<name>`` typed by class (N6).

    Unicode -> str[], WORD/DWORD/QWORD -> int[] (range of the class), BOOL -> bool[],
    ByteArray/GUID -> read-only binary. Writing copies the original attribute
    (class, language, stream) and changes only its value.
    """
    SOURCE = "asf"

    def _groups(self) -> dict[str, list]:
        groups: dict[str, list] = {}
        for name, attr in (list.__iter__(self.tags) if self.tags is not None else ()):
            groups.setdefault(name, []).append(attr)
        return groups

    def fields(self) -> list[MetadataField]:
        asf = _asf()
        out = []
        for name, attrs in self._groups().items():
            key = f"tag:{name}"
            classes = {type(a) for a in attrs}
            cls = next(iter(classes)) if len(classes) == 1 else None
            if cls is asf.ASFUnicodeAttribute:
                out.append(_text_field(key, name, [a.value for a in attrs], self.SOURCE))
            elif cls is not None and _asf_int_range(cls):
                vals = [int(a.value) for a in attrs]
                out.append(MetadataField(key, name, vals, raw_value=list(vals), field_type="int",
                                         source=self.SOURCE, multi=True))
            elif cls is asf.ASFBoolAttribute:
                vals = [bool(a.value) for a in attrs]
                out.append(MetadataField(key, name, vals, raw_value=list(vals), field_type="bool",
                                         source=self.SOURCE, multi=True))
            elif all(isinstance(a.value, (bytes, bytearray)) for a in attrs):
                data = [bytes(a.value) for a in attrs]
                mime = _wm_picture_mime(data[0]) if name == "WM/Picture" else None
                out.append(MetadataField.binary_field(key, name, data, mime=mime, source=self.SOURCE))
            else:  # mixed attribute classes under one name: shown, not rewritable
                out.append(_text_field(key, name, [str(a) for a in attrs], self.SOURCE, editable=False))
        return out

    def dynamic_schema(self, key: str) -> MetadataField | None:
        if not key.startswith("tag:"):
            return None
        name = key[4:]
        if not name or "\x00" in name or not _encodable(name):
            return None
        return _schema(key, name, self.SOURCE)

    def addable(self, present: set[str]) -> list[MetadataField]:
        names = ("Title", "Author", "WM/AlbumTitle", "WM/AlbumArtist", "WM/Year", "WM/Genre",
                 "WM/Composer", "Description")
        return [self.dynamic_schema(f"tag:{n}") for n in names if f"tag:{n}" not in present]

    def validate(self, field: MetadataField, value: Any) -> Any:
        v = self.static_validate(field, value)
        originals = self._groups().get(field.key[4:], [])
        for i, item in enumerate(_items(v)):
            template = originals[min(i, len(originals) - 1)] if originals else None
            bounds = _asf_int_range(type(template)) if template is not None else None
            if bounds and not (bounds[0] <= item <= bounds[1]):
                raise _err(field.key, ErrorCode.OUT_OF_RANGE,
                           f"{item} outside [{bounds[0]}, {bounds[1]}] of {type(template).__name__}")
        return v

    def _replace(self, name: str, attrs: list) -> None:
        pairs = list(list.__iter__(self.tags))
        idx = next((i for i, (k, _) in enumerate(pairs) if k == name), None)
        kept = [(k, a) for k, a in pairs if k != name]
        if idx is None:
            idx = len(kept)
        kept[idx:idx] = [(name, a) for a in attrs]
        list.clear(self.tags)
        list.extend(self.tags, kept)

    def write(self, field: MetadataField, value: Any) -> None:
        asf = _asf()
        name = field.key[4:]
        originals = self._groups().get(name, [])
        attrs = []
        for i, item in enumerate(_items(value)):
            if originals:
                t = originals[min(i, len(originals) - 1)]
                attrs.append(type(t)(value=item, language=t.language, stream=t.stream))
            else:
                attrs.append(asf.ASFUnicodeAttribute(value=item))
        self._replace(name, attrs)

    def delete(self, key: str) -> None:
        self._replace(key[4:], [])

    def snapshot(self, key: str) -> Any:
        """``Placed``: (class, value, language, stream) of each attribute of the name with
        its index in the attribute list. mutagen reads the list back object by object
        (content description, extended, metadata, metadata library) and saves each
        object's attributes in list order, so restoring the indexes restores the order."""
        if not key.startswith("tag:") or self.tags is None:
            return ABSENT
        name = key[4:]
        found = [(i, (a.TYPE, copy.deepcopy(a.value), a.language, a.stream))
                 for i, (k, a) in enumerate(list.__iter__(self.tags)) if k == name]
        return Placed.of(found) if found else ABSENT

    def restore(self, key: str, raw: Any) -> None:
        self.restore_many({key: raw})

    def restore_many(self, snap: dict[str, Any]) -> None:
        asf = _asf()
        inserts = []
        for key, raw in snap.items():
            if raw is ABSENT:
                continue
            try:
                inserts += [(pos, (key[4:], asf.ASFBaseAttribute._get_type(t)(
                                value=v, language=lang, stream=stream)))
                            for pos, (t, v, lang, stream) in raw.placed()]
            except (KeyError, *_VALUE_EXCEPTIONS) as exc:
                raise _err(key, ErrorCode.INVALID_VALUE, f"cannot restore: {exc}") from exc
        names = {key[4:] for key in snap}
        kept = [(k, a) for k, a in list.__iter__(self.tags) if k not in names]
        _insert_placed(kept, inserts)
        list.clear(self.tags)
        list.extend(self.tags, kept)


# ──────────────────────── APEv2 ──────────────────────────────────────────────

class ApeCodec(TagCodec):
    """APEv2 items ``tag:<Key>`` (N7): text str[] split on NUL, external str, binary read-only."""
    SOURCE = "ape"

    def _actual(self, name: str) -> str | None:
        if self.tags is None:
            return None
        return next((k for k in self.tags.keys() if k.lower() == name.lower()), None)

    def fields(self) -> list[MetadataField]:
        from mutagen.apev2 import BINARY, EXTERNAL
        out = []
        for name in (self.tags.keys() if self.tags is not None else []):
            value = self.tags[name]
            key = f"tag:{name}"
            if value.kind == BINARY:
                out.append(MetadataField.binary_field(key, name, bytes(value.value), source=self.SOURCE))
            elif value.kind == EXTERNAL:
                out.append(_text_field(key, name, [value.value], self.SOURCE, multi=False))
            else:
                out.append(_text_field(key, name, list(value), self.SOURCE))
        return out

    def dynamic_schema(self, key: str) -> MetadataField | None:
        from mutagen.apev2 import is_valid_apev2_key
        if not key.startswith("tag:"):
            return None
        name = key[4:]
        try:
            valid = is_valid_apev2_key(name)
        except Exception:
            valid = False
        if not valid or self._actual(name) is not None:
            return None
        return _schema(key, name, self.SOURCE)

    def addable(self, present: set[str]) -> list[MetadataField]:
        names = ("Title", "Artist", "Album", "Album Artist", "Track", "Disc", "Year",
                 "Genre", "Composer", "Comment")
        out = []
        for n in names:
            schema = self.dynamic_schema(f"tag:{n}")
            if schema is not None and schema.key not in present:
                out.append(schema)
        return out

    def write(self, field: MetadataField, value: Any) -> None:
        from mutagen.apev2 import APEValue, EXTERNAL, TEXT
        name = self._actual(field.key[4:]) or field.key[4:]
        current = self.tags.get(name) if self._actual(name) else None
        if current is not None and current.kind == EXTERNAL:
            self.tags[name] = APEValue(value, EXTERNAL)
        else:
            self.tags[name] = APEValue("\x00".join(_items(value)), TEXT)

    def delete(self, key: str) -> None:
        name = self._actual(key[4:])
        if name is not None:
            del self.tags[name]

    def snapshot(self, key: str) -> Any:
        name = self._actual(key[4:]) if key.startswith("tag:") else None
        if name is None:
            return ABSENT
        value = self.tags[name]
        return (name, value.kind, copy.deepcopy(value.value))

    def restore(self, key: str, raw: Any) -> None:
        from mutagen.apev2 import APEValue
        self.delete(key)
        name, kind, value = raw
        self.tags[name] = APEValue(value, kind)

    def save(self) -> None:
        """mutagen rewrites the APEv2 tag at the end of the file by truncating at the
        old tag, which also cut a trailing ID3v1 tag (silently: it is no field of the
        APE record), and appended a NEW tag after an ID3v1 tag, which then was no
        longer in the last 128 bytes where readers look for it. The ID3v1 tag (with a
        Lyrics3v2 block in front of it) is detached first and appended again after
        the APEv2 tag: the standard layout."""
        path = Path(self.audio.filename)
        trailer = _detach_id3v1(path)
        try:
            self.audio.save()
        finally:
            if trailer:
                with open(path, "ab") as stream:
                    stream.write(trailer)

    def container_removable(self) -> bool:
        return self.tags is not None and len(self.tags) == 0

    def remove_container(self) -> None:
        # APEv2.delete removes the tag bytes only: a following ID3v1 tag stays last.
        self.audio.tags.delete(self.audio.filename)


def _detach_id3v1(path: Path) -> bytes:
    """Cut a trailing ID3v1 tag (and a Lyrics3v2 block right before it) off ``path``.

    Same detection as mutagen's APEv2 reader: a file whose last 32 bytes are an
    APEv2 footer has no ID3v1 tag; otherwise "TAG" at -128 is one. Returns the bytes
    removed (``b""`` when there is no ID3v1 tag).
    """
    with open(path, "r+b") as stream:
        size = stream.seek(0, 2)
        if size < 128:
            return b""
        stream.seek(size - 32)
        if stream.read(8) == b"APETAGEX":
            return b""
        stream.seek(size - 128)
        if stream.read(3) != b"TAG":
            return b""
        start = size - 128
        if start >= 15:
            stream.seek(start - 15)
            tail = stream.read(15)              # 6-digit size + "LYRICS200"
            if tail[6:] == b"LYRICS200" and tail[:6].isdigit():
                begin = start - 15 - int(tail[:6])
                if begin >= 0:
                    stream.seek(begin)
                    if stream.read(11) == b"LYRICSBEGIN":
                        start = begin
        stream.seek(start)
        trailer = stream.read()
        stream.truncate(start)
    return trailer


# ──────────────────────── handler mixin ──────────────────────────────────────

def codec_for_source(source: str):
    from core.handlers._mp4_codec import Mp4Codec
    return {"id3": Id3Codec, "vorbis": VorbisCodec, "asf": AsfCodec, "ape": ApeCodec,
            "mp4": Mp4Codec}.get(source)


# TypeError is deliberately absent: a value of the wrong type is rejected by ``canonical``
# (``wrong_type``) before a codec runs, so a TypeError here is a codec bug and must surface as 500.
_VALUE_EXCEPTIONS = (ValueError, UnicodeError, struct.error, OverflowError)


def _raise_all(errors: list[MetadataValueError]) -> None:
    if len(errors) == 1:
        raise errors[0]
    if errors:
        raise MetadataValueErrors(errors)


class TagMutations:
    """Mutation hooks of ``BaseMetadataHandler`` on top of a ``TagCodec``.

    The handler implements ``_open_codec(path) -> (codec | None, writable: bool)``;
    ``codec`` is ``None`` for files without a supported tag system.
    """

    def _open_codec(self, path: Path):  # pragma: no cover - implemented by handlers
        raise NotImplementedError

    def _codec_or_fail(self, path: Path, keys: list[str], code: str):
        codec, writable = self._open_codec(path)
        if codec is None or not writable:
            _raise_all([_err(k, code, "this file format is read-only") for k in keys] or
                       [_err(None, code, "this file format is read-only")])
        return codec

    def canonical(self, field: MetadataField, value: Any) -> Any:
        codec_cls = codec_for_source(field.source)
        if codec_cls is None:
            return values.canonicalize(field, value)
        return codec_cls.static_validate(field, value)

    def writable_fields(self, path: Path, record) -> list[MetadataField]:
        try:
            codec, writable = self._open_codec(path)
        except Exception:
            return []
        if codec is None or not writable or record.read_errors:
            return []
        present = {f.key for f in record.fields}
        return [f for f in codec.addable(present) if f is not None and f.key not in present]

    def writable_field(self, path: Path, key: str) -> MetadataField | None:
        try:
            codec, writable = self._open_codec(path)
        except Exception:
            return None
        if codec is None or not writable or not codec.owns(key):
            return None
        if any(f.key == key for f in codec.fields()):
            return None
        return codec.dynamic_schema(key)

    def apply_write(self, tmp: Path, fields: list[MetadataField]) -> None:
        keys = [f.key for f in fields]
        if len(set(keys)) != len(keys):
            raise _err(None, ErrorCode.INVALID_VALUE, "duplicate metadata keys")
        codec = self._codec_or_fail(tmp, keys, ErrorCode.READ_ONLY)
        existing = {f.key: f for f in codec.fields()}
        errors: list[MetadataValueError] = []
        plan: list[tuple[MetadataField, Any]] = []
        for f in fields:
            schema = existing.get(f.key) or (codec.dynamic_schema(f.key) if codec.owns(f.key) else None)
            if schema is None:
                errors.append(_err(f.key, ErrorCode.UNKNOWN_FIELD, "unknown or unsupported field"))
                continue
            if not schema.editable:
                errors.append(_err(f.key, ErrorCode.READ_ONLY, "field is read-only"))
                continue
            try:
                value = codec.validate(schema, copy.deepcopy(f.value))
            except MetadataValueError as exc:
                errors.append(exc.with_key(f.key))
                continue
            if f.key in existing and existing[f.key].value == value:
                continue   # G1: the stored value is already this one, nothing to rewrite
            plan.append((schema, value))
        _raise_all(errors)
        if not plan:
            return
        codec.ensure_tags()
        for schema, value in plan:
            try:
                codec.write(schema, value)
            except MetadataValueError:
                raise
            except _VALUE_EXCEPTIONS as exc:
                raise _err(schema.key, ErrorCode.INVALID_VALUE, str(exc)) from exc
        self._save(codec, [s.key for s, _ in plan])
        self._verify(tmp, {s.key: v for s, v in plan})

    def apply_delete(self, tmp: Path, keys: list[str]) -> None:
        if len(set(keys)) != len(keys):
            raise _err(None, ErrorCode.INVALID_VALUE, "duplicate metadata keys")
        codec = self._codec_or_fail(tmp, keys, ErrorCode.NOT_DELETABLE)
        existing = {f.key: f for f in codec.fields()}
        errors = []
        for key in keys:
            if key not in existing:
                errors.append(_err(key, ErrorCode.UNKNOWN_FIELD, "field is not present"))
            elif not existing[key].deletable:
                errors.append(_err(key, ErrorCode.NOT_DELETABLE, "field cannot be deleted"))
        _raise_all(errors)
        if not keys:
            return
        for key in keys:
            codec.delete(key)
        self._save(codec, keys)
        remaining = {f.key for f in self._open_codec(tmp)[0].fields()} & set(keys)
        if remaining:
            raise _err(sorted(remaining)[0], ErrorCode.LOSSY_WRITE, "the field could not be removed")

    def snapshot(self, tmp: Path, keys: list[str]) -> dict[str, Any]:
        codec, _ = self._open_codec(tmp)
        result = {}
        for key in keys:
            if codec is None or codec.tags is None and key.startswith(codec.PREFIXES):
                result[key] = ABSENT
            elif not codec.owns(key):
                raise _err(key, ErrorCode.UNKNOWN_FIELD, "not a tag of this file")
            else:
                result[key] = codec.snapshot(key)
        return result

    def restore(self, tmp: Path, snap: dict[str, Any]) -> None:
        """All keys of one Undo together (positions of ordered containers, see
        ``Placed``); an Undo that leaves the tag container empty removes it."""
        if not snap:
            return
        keys = list(snap)
        codec = self._codec_or_fail(tmp, keys, ErrorCode.READ_ONLY)
        for key in keys:
            if not codec.owns(key):
                raise _err(key, ErrorCode.UNKNOWN_FIELD, "not a tag of this file")
        absent_only = all(raw is ABSENT for raw in snap.values())
        if absent_only and all(codec.snapshot(k) is ABSENT for k in keys):
            return                       # already the pre-image: nothing to write
        try:
            codec.restore_many(copy.deepcopy(snap))
        except MetadataValueError:
            raise
        except _VALUE_EXCEPTIONS as exc:
            raise _err(keys[0] if len(keys) == 1 else None, ErrorCode.INVALID_VALUE,
                       f"cannot restore: {exc}") from exc
        if absent_only and codec.container_removable():
            codec.remove_container()
        else:
            self._save(codec, keys)
        after = self.snapshot(tmp, keys)
        bad = sorted(k for k in snap if after[k] != snap[k])
        if bad:
            raise _err(bad[0], ErrorCode.LOSSY_WRITE, "the original value could not be restored exactly")

    # internals
    def _save(self, codec: TagCodec, keys: list[str]) -> None:
        try:
            codec.save()
        except _VALUE_EXCEPTIONS as exc:
            raise _err(keys[0] if len(keys) == 1 else None, ErrorCode.INVALID_VALUE,
                       f"the format rejected the value: {exc}") from exc

    def _verify(self, tmp: Path, expected: dict[str, Any]) -> None:
        """G4: re-read the file and require every written value back exactly."""
        codec, _ = self._open_codec(tmp)
        got = {f.key: f for f in codec.fields()} if codec is not None else {}
        errors = []
        for key, value in expected.items():
            f = got.get(key)
            if f is None or _canon(f.value) != _canon(value):
                errors.append(_err(key, ErrorCode.LOSSY_WRITE,
                                   f"the format did not store the value exactly "
                                   f"(read back {None if f is None else f.value!r})"))
        _raise_all(errors)
