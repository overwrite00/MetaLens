"""Matroska / WebM fields (``mkv:`` namespace) and their removal / in-place edit."""
from __future__ import annotations

import mmap
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core import ebml, values
from core.models import MetadataField

_EPOCH_2001 = datetime(2001, 1, 1, tzinfo=timezone.utc)
_TRACK_TYPES = {1: "Video", 2: "Audio", 3: "Complex", 16: "Logo", 17: "Subtitle", 18: "Buttons", 32: "Control"}
INFO_KEYS = {ebml.TITLE: "mkv:Info:Title", ebml.MUXING_APP: "mkv:Info:MuxingApp", ebml.WRITING_APP: "mkv:Info:WritingApp"}


def _field(key, label, value, field_type="str", *, deletable=False, editable=False, multi=False, computed=False):
    return MetadataField(key, label, value, raw_value=value, editable=editable, deletable=deletable,
                         field_type=field_type, source="mkv", multi=multi, computed=computed)


def _open(path: Path):
    stream = open(path, "rb")
    try:
        return stream, mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ)
    except (ValueError, OSError):
        stream.close()
        raise ebml.EbmlError("empty file")


def _plan(path: Path):
    """``({key: [Element or patch tuple]}, segment, buffer, stream)`` helper shared by read and edit."""
    stream, buf = _open(path)
    return stream, buf, ebml.Segment(buf)


def fields(path: Path, writable: bool = True) -> tuple[list[MetadataField], list[str], bool]:
    """``(fields, notices, editable)`` of a Matroska file; ``editable`` is False for live recordings."""
    stream, buf = _open(path)
    try:
        seg = ebml.Segment(buf)
        notes: list[str] = []
        editable = writable and not seg.live
        if seg.live:
            notes.append("This recording has no final size: its metadata is read-only")
        out = [_field("mkv:DocType", "Document Type", seg.doc_type)]
        info = ebml.info_values(buf, seg)
        for ident, key in INFO_KEYS.items():
            if ident in info:
                out.append(_field(key, key.rsplit(":", 1)[1], ebml._text(buf, info[ident]),
                                  deletable=editable, editable=editable))
        if ebml.DATE_UTC in info and info[ebml.DATE_UTC].end - info[ebml.DATE_UTC].data_start == 8:
            nanos = int.from_bytes(buf[info[ebml.DATE_UTC].data_start:info[ebml.DATE_UTC].end], "big", signed=True)
            try:
                out.append(_field("mkv:Info:DateUTC", "Date (UTC)",
                                  values.format_datetime(_EPOCH_2001 + timedelta(microseconds=nanos // 1000)),
                                  "datetime", deletable=editable))
            except OverflowError:
                pass
        scale = ebml._uint(buf, info[ebml.TIMECODE_SCALE]) if ebml.TIMECODE_SCALE in info else 1_000_000
        if ebml.DURATION in info:
            duration = ebml._float(buf, info[ebml.DURATION])
            if duration is not None and duration >= 0:
                out.append(_field("mkv:Info:Duration", "Duration (s)", round(duration * scale / 1e9, 3), "duration",
                                  computed=True))
        seen: set[str] = set()
        for name, text, _element in ebml.simple_tags(buf, seg):
            if name in seen:
                continue
            seen.add(name)
            out.append(_field(f"mkv:tag:{name}", name, text, deletable=editable, editable=editable))
        for name, mime, data, _element in ebml.attachments(buf, seg):
            if name and f"mkv:attachment:{name}" not in seen:
                seen.add(f"mkv:attachment:{name}")
                out.append(MetadataField.binary_field(f"mkv:attachment:{name}", f"Attachment {name}", data,
                                                      mime=mime or None, deletable=editable, source="mkv"))
        for t in ebml.tracks(buf, seg):
            n = t["number"]
            out.append(_field(f"mkv:track{n}:Type", f"Track {n} Type", _TRACK_TYPES.get(t["type"], str(t["type"]))))
            if t["codec"]:
                out.append(_field(f"mkv:track{n}:CodecID", f"Track {n} Codec", t["codec"]))
            if t["language"]:
                out.append(_field(f"mkv:track{n}:Language", f"Track {n} Language", t["language"]))
            if t["name"]:
                out.append(_field(f"mkv:track{n}:Name", f"Track {n} Name", t["name"]))
        return out, notes, editable
    finally:
        buf.close()
        stream.close()


def removal_plan(path: Path) -> dict[str, tuple]:
    """Patches ``(offset, original, replacement)`` that remove each removable field, by key."""
    stream, buf = _open(path)
    try:
        seg = ebml.Segment(buf)
        if seg.live:
            return {}
        plan: dict[str, tuple] = {}
        info = ebml.info_values(buf, seg)
        for ident, key in INFO_KEYS.items():
            if ident in info:
                plan[key] = (ebml.void_patch(buf, info[ident]),)
        if ebml.DATE_UTC in info:
            plan["mkv:Info:DateUTC"] = (ebml.void_patch(buf, info[ebml.DATE_UTC]),)
        for name, _text, element in ebml.simple_tags(buf, seg):
            plan.setdefault(f"mkv:tag:{name}", ())
            plan[f"mkv:tag:{name}"] += (ebml.void_patch(buf, element),)
        for name, _mime, _data, element in ebml.attachments(buf, seg):
            if name:
                plan[f"mkv:attachment:{name}"] = (ebml.void_patch(buf, element),)
        return plan
    finally:
        buf.close()
        stream.close()


def apply_patches(path: Path, patches, restore: bool = False) -> None:
    with open(path, "r+b") as stream:
        for offset, original, replacement in patches:
            stream.seek(offset)
            stream.write(original if restore else replacement)


def _vint_size(n: int) -> bytes:
    for length in range(1, 9):
        if n < (1 << (7 * length)) - 1:
            return ((1 << (7 * length)) | n).to_bytes(length, "big")
    raise ebml.EbmlError("too large")


def set_text(path: Path, key: str, text: str) -> None:
    """Write a string over the old one; what is left of the old element becomes a Void element."""
    stream, buf = _open(path)
    try:
        seg = ebml.Segment(buf)
        if seg.live:
            raise ebml.EbmlError("a recording without a final size cannot be changed")
        target = None
        parent = None
        if key in INFO_KEYS.values():
            info = ebml.info_values(buf, seg)
            ident = next(i for i, k in INFO_KEYS.items() if k == key)
            target = info.get(ident)
            block = seg.first(ebml.INFO)
            parent = (block.data_start, block.end)
        elif key.startswith("mkv:tag:"):
            name = key[len("mkv:tag:"):]
            for tag_name, _t, simple in ebml.simple_tags(buf, seg):
                if tag_name == name:
                    kids = {e.id: e for e in ebml.children(buf, simple.data_start, simple.end)}
                    target = kids[ebml.TAG_STRING]
                    parent = (simple.data_start, simple.end)
                    break
        if target is None:
            raise ebml.EbmlError("the field was not found")
        id_bytes = bytes(buf[target.start:target.start + _id_length(buf[target.start])])
        total, start = target.size, target.start
        # the Void elements that follow it are free room (what an earlier, shorter value left behind)
        following = [e for e in ebml.children(buf, *parent) if e.start >= target.end]
        edge = target.end
        for e in following:
            if e.id != ebml.VOID or e.start != edge:
                break
            total += e.size
            edge = e.end
    finally:
        buf.close()
        stream.close()
    raw = text.encode("utf-8")
    for pad in range(0, 3):
        payload = raw + bytes(pad)
        element = id_bytes + _vint_size(len(payload)) + payload
        rest = total - len(element)
        if rest == 0 or rest >= 2:
            filler = b""
            if rest:
                filler = _void_bytes(rest)
            with open(path, "r+b") as handle:
                handle.seek(start)
                handle.write(element + filler)
            return
    raise ebml.EbmlError("the new text is longer than the room the old one leaves")


def _id_length(first: int) -> int:
    length = 1
    while length <= 4 and not first & (0x80 >> (length - 1)):
        length += 1
    return length


def _void_bytes(total: int) -> bytes:
    for size_len in range(1, 9):
        payload = total - 1 - size_len
        if payload >= 0 and payload < (1 << (7 * size_len)) - 1:
            return bytes([ebml.VOID]) + ((1 << (7 * size_len)) | payload).to_bytes(size_len, "big") + bytes(payload)
    raise ebml.EbmlError("too small to be voided")
