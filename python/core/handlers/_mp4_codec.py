"""MP4/QuickTime ``ilst`` atoms as typed fields under ``mp4:<atom>`` (plan F2C, D8).

Shared by ``.m4a``/``.m4b``/``.m4r`` (audio handler) and ``.mp4``/``.m4v``/``.mov``
(video handler), so the same atom has the same key in every MP4 container.

=====================  ==============================================================
atom                   field
=====================  ==============================================================
text (``©nam``...)     ``str[]``
``trkn`` / ``disk``    ``int[2]`` ``[number, total]`` (0..65535)
integer (``tmpo``...)  ``int`` (one value; range of the atom width) or ``int[]``
``cpil/pgap/pcst``     ``bool``, written as ONE value (mutagen stores a scalar)
``covr``               ``binary[]`` read-only, deletable
freeform ``----:m:n``  ``str[]`` when every value is UTF-8, else ``binary[]`` read-only
=====================  ==============================================================

Integers are exact both ways in JSON (Python writes every digit; e.g. ``plID`` is
8 bytes): a client that cannot hold integers above 2**53 - 1 may send them as
decimal strings, which ``values.parse_int`` accepts.

``©day``/``purd`` are free text: the QuickTime/iTunes metadata specification
does not impose a date format (iTunes writes ``2009-08-12T07:00:00Z``, other
taggers a year or any text), so no date validation is applied (unlike ID3v2.4).

Undo: atoms are written in mutagen's own sorted order, so restoring their
content restores the file layout; the first tag added to a file without ``ilst``
makes mutagen insert ``moov.udta.meta`` (+ ``hdlr``, ``free`` padding) and there
is no mutagen API to remove that atom again: the byte patch of the Undo journal
(``api/byte_patch.py``) gives the original file back; without it (changed region)
an Undo leaves an empty ``ilst`` (no tag value; media data and chunk offsets untouched).
"""
from __future__ import annotations

import copy
from typing import Any

from core.base_handler import ABSENT
from core.errors import ErrorCode
from core.handlers._tag_codecs import (TagCodec, _err, _items, _schema, _text_field,
                                       _validate_generic, register_codec)
from core.models import MetadataField

__all__ = ["Mp4Codec", "MP4_LABELS"]

MP4_LABELS = {
    "\xa9nam": "Title", "\xa9ART": "Artist", "\xa9alb": "Album", "aART": "Album Artist",
    "\xa9day": "Year", "\xa9gen": "Genre", "\xa9cmt": "Comment", "\xa9wrt": "Composer",
    "\xa9grp": "Grouping", "\xa9lyr": "Lyrics", "\xa9too": "Encoded By", "cprt": "Copyright",
    "desc": "Description", "ldes": "Long Description", "purd": "Purchase Date",
    "catg": "Category", "keyw": "Keywords", "soal": "Album Sort Order",
    "soaa": "Album Artist Sort Order", "soar": "Artist Sort Order", "sonm": "Title Sort Order",
    "soco": "Composer Sort Order", "sosn": "Show Sort Order", "tvsh": "TV Show",
    "trkn": "Track (number, total)", "disk": "Disc (number, total)", "tmpo": "BPM",
    "cpil": "Compilation", "pgap": "Gapless Playback", "pcst": "Podcast", "covr": "Cover Art",
    "tvsn": "TV Season", "tves": "TV Episode", "stik": "Media Kind", "rtng": "Rating",
    "hdvd": "HD Video", "purl": "Podcast URL", "egid": "Podcast Episode GUID",
    "\xa9mvi": "Movement Number", "\xa9mvc": "Movement Count", "shwm": "Show Movement",
}

TEXT_ATOMS = frozenset({
    "\xa9nam", "\xa9alb", "\xa9ART", "aART", "\xa9wrt", "\xa9day", "\xa9cmt", "desc",
    "purd", "\xa9grp", "\xa9gen", "\xa9lyr", "catg", "keyw", "\xa9too", "cprt", "soal",
    "soaa", "soar", "sonm", "soco", "sosn", "tvsh", "purl", "egid", "ldes",
})
#: integer atoms -> minimum byte width written by mutagen.
INT_ATOMS = {
    "plID": 8, "cnID": 4, "geID": 4, "atID": 4, "sfID": 4, "cmID": 4, "akID": 1,
    "tvsn": 4, "tves": 4, "tmpo": 2, "\xa9mvi": 2, "\xa9mvc": 2, "shwm": 1, "stik": 1,
    "hdvd": 1, "rtng": 1,
}
BOOL_ATOMS = frozenset({"cpil", "pgap", "pcst"})
PAIR_ATOMS = frozenset({"trkn", "disk"})
_ADDABLE = ("\xa9nam", "\xa9ART", "\xa9alb", "aART", "\xa9day", "\xa9gen", "\xa9wrt",
            "\xa9cmt", "trkn", "disk", "tmpo", "cpil")
_COVER_MIME = {13: "image/jpeg", 14: "image/png", 27: "image/bmp", 12: "image/gif"}


def _int_bounds(atom: str) -> tuple[int, int]:
    # parsed as signed big-endian integers: stay within the declared width
    return 0, 2 ** (8 * INT_ATOMS[atom] - 1) - 1


def _is_freeform(atom: str) -> bool:
    return atom.startswith("----:")


def _freeform_text(vals) -> list[str] | None:
    from mutagen.mp4 import AtomDataType
    out = []
    for v in vals:
        if getattr(v, "dataformat", None) != AtomDataType.UTF8:
            return None
        try:
            out.append(bytes(v).decode("utf-8"))
        except UnicodeDecodeError:
            return None
    return out


class Mp4Codec(TagCodec):
    SOURCE = "mp4"
    PREFIXES = ("mp4:",)

    def _label(self, atom: str) -> str:
        if _is_freeform(atom):
            return atom.split(":", 2)[-1]
        return MP4_LABELS.get(atom, atom)

    def fields(self) -> list[MetadataField]:
        out = []
        for atom, vals in (self.tags.items() if self.tags is not None else []):
            out.append(self._field(atom, vals))
        return out

    def _field(self, atom: str, vals) -> MetadataField:
        key, label, src = f"mp4:{atom}", self._label(atom), self.SOURCE
        if _is_freeform(atom):
            text = _freeform_text(vals)
            if text is not None:
                return _text_field(key, label, text, src)
            return MetadataField.binary_field(key, label, [bytes(v) for v in vals], source=src)
        if atom == "covr":
            formats = {int(getattr(v, "imageformat", 13)) for v in vals}
            mime = _COVER_MIME.get(formats.pop()) if len(formats) == 1 else None
            return MetadataField.binary_field(key, label, [bytes(v) for v in vals], mime=mime,
                                              source=src)
        if atom in BOOL_ATOMS and isinstance(vals, bool):
            return MetadataField(key, label, vals, raw_value=vals, field_type="bool", source=src)
        if atom in PAIR_ATOMS and isinstance(vals, list):
            pairs = [list(map(int, v)) for v in vals]
            if len(pairs) == 1:
                return MetadataField(key, label, pairs[0], raw_value=list(pairs[0]),
                                     field_type="int", source=src, multi=True, length=2)
            return _text_field(key, label, [f"{a}/{b}" for a, b in pairs], src, editable=False)
        if isinstance(vals, list) and vals and all(type(v) is int for v in vals):
            # mutagen parses integers as SIGNED (a 4-byte cnID 3000000000 reads
            # negative): a value outside the range written by ``write`` cannot be
            # written back as read (G1), so such a field is read-only (deletable).
            editable = atom in INT_ATOMS and all(
                _int_bounds(atom)[0] <= v <= _int_bounds(atom)[1] for v in vals)
            if len(vals) == 1:
                return MetadataField(key, label, vals[0], raw_value=vals[0], editable=editable,
                                     field_type="int", source=src)
            return MetadataField(key, label, list(vals), raw_value=list(vals), editable=editable,
                                 field_type="int", source=src, multi=True)
        if isinstance(vals, list) and all(isinstance(v, str) for v in vals):
            return _text_field(key, label, vals, src)
        payloads = [bytes(v) if isinstance(v, (bytes, bytearray)) else repr(v).encode()
                    for v in _items(vals)]
        return MetadataField.binary_field(key, label, payloads, source=src)

    def dynamic_schema(self, key: str) -> MetadataField | None:
        if not key.startswith("mp4:"):
            return None
        atom = key[4:]
        label = self._label(atom) if atom else ""
        if _is_freeform(atom):
            parts = atom.split(":", 2)
            if len(parts) != 3 or not parts[1] or not parts[2]:
                return None
            try:
                atom.encode("latin-1")
            except UnicodeEncodeError:
                return None
            return _schema(key, label, self.SOURCE)
        if atom in TEXT_ATOMS:
            return _schema(key, label, self.SOURCE)
        if atom in PAIR_ATOMS:
            return _schema(key, label, self.SOURCE, field_type="int", multi=True, length=2)
        if atom in INT_ATOMS:
            return _schema(key, label, self.SOURCE, field_type="int", multi=False)
        if atom in BOOL_ATOMS:
            return _schema(key, label, self.SOURCE, field_type="bool", multi=False)
        return None

    def addable(self, present: set[str]) -> list[MetadataField]:
        return [self.dynamic_schema(f"mp4:{a}") for a in _ADDABLE if f"mp4:{a}" not in present]

    @classmethod
    def static_validate(cls, field: MetadataField, value: Any) -> Any:
        v = _validate_generic(field, value)
        atom = field.key[4:] if field.key.startswith("mp4:") else ""
        if atom in PAIR_ATOMS:
            bounds = (0, 65535)
        elif atom in INT_ATOMS:
            bounds = _int_bounds(atom)
        else:
            bounds = None
        if bounds and field.field_type == "int":
            for item in _items(v):
                if not bounds[0] <= item <= bounds[1]:
                    raise _err(field.key, ErrorCode.OUT_OF_RANGE,
                               f"{item} outside [{bounds[0]}, {bounds[1]}]")
        return v

    def write(self, field: MetadataField, value: Any) -> None:
        from mutagen.mp4 import AtomDataType, MP4FreeForm
        atom = field.key[4:]
        if _is_freeform(atom):
            self.tags[atom] = [MP4FreeForm(s.encode("utf-8"), dataformat=AtomDataType.UTF8)
                               for s in value]
        elif atom in BOOL_ATOMS:
            self.tags[atom] = bool(value)          # a scalar: bool([False]) would be True
        elif atom in PAIR_ATOMS:
            self.tags[atom] = [tuple(value)]
        elif field.field_type == "int":
            self.tags[atom] = list(value) if field.multi else [value]
        else:
            self.tags[atom] = list(value)

    def delete(self, key: str) -> None:
        if self.tags is not None:
            self.tags.pop(key[4:], None)

    def snapshot(self, key: str) -> Any:
        atom = key[4:]
        if self.tags is None or atom not in self.tags:
            return ABSENT
        return _norm(self.tags[atom])

    def restore(self, key: str, raw: Any) -> None:
        self.tags[key[4:]] = _denorm(raw)


def _norm(vals) -> Any:
    from mutagen.mp4 import MP4Cover, MP4FreeForm
    if isinstance(vals, bool):
        return ("bool", vals)
    items = []
    for v in vals:
        if isinstance(v, MP4Cover):
            items.append(("cover", bytes(v), int(v.imageformat)))
        elif isinstance(v, MP4FreeForm):
            items.append(("freeform", bytes(v), int(v.dataformat), int(v.version)))
        elif isinstance(v, tuple):
            items.append(("pair", tuple(v)))
        else:
            items.append(copy.deepcopy(v))
    return ("list", tuple(items))


def _denorm(raw) -> Any:
    from mutagen.mp4 import MP4Cover, MP4FreeForm
    kind, payload = raw
    if kind == "bool":
        return payload
    out = []
    for item in payload:
        if isinstance(item, tuple) and item and item[0] == "cover":
            out.append(MP4Cover(item[1], imageformat=item[2]))
        elif isinstance(item, tuple) and item and item[0] == "freeform":
            out.append(MP4FreeForm(item[1], dataformat=item[2], version=item[3]))
        elif isinstance(item, tuple) and item and item[0] == "pair":
            out.append(tuple(item[1]))
        else:
            out.append(item)
    return out


register_codec("mp4", Mp4Codec)
