"""hachoir fallback reader (read-only), shared with VideoHandler's read-only formats.

Fields come from hachoir's metadata objects, never from its human-readable export:

* key ``hachoir:<group index>:<item.key>``: group 0 is the root metadata
  (header "Common" for multi-stream files, "Metadata" otherwise), groups 1..n
  follow ``MultipleMetadata.iterGroups()`` depth first. ``item.key`` is hachoir's
  internal identifier (``duration``, ``width``...), so keys are unique per
  record and do not change with the (translatable) description or the value;
* value = the raw ``item.values[i].value`` typed per ``core.values``:
  timedelta -> ``duration`` seconds, datetime -> ISO (``datetime`` when aware,
  ``localdatetime`` when naive), bool/int/float as such, bytes -> binary
  descriptor, text -> ``str``/``text``, other objects -> their hachoir text;
  several values -> list (``multi``); mixed types -> list of their texts;
* ``raw_value`` = hachoir's human text ("1 min 30 sec"), a list when multi;
* label = "<group header> — <item description>". Nothing is editable or deletable.
"""
from __future__ import annotations

import functools
import logging
import math
import threading
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

from core.base_handler import BaseMetadataHandler
from core.models import MetadataField, MetadataRecord

PARSE_ERROR = "hachoir could not parse file"
NO_METADATA = "No format-specific metadata available"

_LOG = logging.getLogger("hachoir")
_capture = threading.local()

@functools.cache
def _route_hachoir_log() -> None:
    """Send hachoir's own log (printed on stderr by default, e.g. "[warn] Skip
    parser ...") to the ``hachoir`` Python logger instead: warnings at DEBUG,
    errors at WARNING. Errors raised during ``extract_fields`` are also collected
    as read errors. Idempotent (cached); affects only hachoir's logger."""
    from hachoir.core.log import Log, log

    def forward(level, prefix, text, ctxt):
        _LOG.log(logging.WARNING if level >= Log.LOG_ERROR else logging.DEBUG, "%s", text)
        sink = getattr(_capture, "errors", None)
        if sink is not None and level >= Log.LOG_ERROR:
            sink.append(str(text))

    log.use_print = False
    log.on_new_message = forward


class HachoirHandler(BaseMetadataHandler):
    NAME = "hachoir"
    EXTENSIONS = frozenset()  # handles everything not claimed by others
    PRIORITY = 100            # lowest priority — true fallback

    def can_handle(self, path: Path) -> bool:
        return True  # universal fallback

    def read(self, path: Path) -> MetadataRecord:
        """As the primary handler of unknown files, content hachoir does not
        recognize (plain text, random bytes...) is not an error: the record is
        valid, has no format fields and carries the informative ``NO_METADATA``
        warning. ``read_errors`` are kept for real failures (unreadable file,
        extraction failing on a recognized format)."""
        fields, errors = extract_fields(path)
        warnings = []
        if errors == [PARSE_ERROR]:
            errors = []
        if not fields and not errors:
            warnings.append(NO_METADATA)
        record = self._make_record(path, read_errors=errors, warnings=warnings,
                                   supports_write=False, supports_delete=False)
        record.fields = fields
        record.read_only_reason = "no metadata writer exists for this file type"
        return record

    def write(self, path: Path, fields: list[MetadataField]) -> None:
        raise NotImplementedError("hachoir handler is read-only")

    def delete(self, path: Path, keys: list[str]) -> None:
        raise NotImplementedError("hachoir handler is read-only")


def extract_fields(path: Path) -> tuple[list[MetadataField], list[str]]:
    """Read-only hachoir fields of ``path`` and the read errors (shared with VideoHandler).

    Errors: ``"hachoir could not parse file"`` when no parser recognizes the
    content, ``"hachoir read error: <exc>"`` on any failure, including an error
    that hachoir logs (and swallows) while extracting a recognized format; the
    fields extracted before it are kept. A recognized format without a metadata
    extractor gives no fields and no error.
    """
    _capture.errors = logged = []
    try:
        _route_hachoir_log()
        from hachoir.parser import createParser
        from hachoir.metadata import extractMetadata
        if Path(path).stat().st_size == 0:  # hachoir raises "Input size is nul"
            return [], [PARSE_ERROR]
        parser = createParser(str(path))
        if parser is None:
            return [], [PARSE_ERROR]
        logged.clear()  # parser probing is not an extraction failure
        with parser:
            meta = extractMetadata(parser)
        fields = [] if meta is None else fields_from_metadata(meta)
        return fields, [f"hachoir read error: {text}" for text in logged]
    except Exception as e:
        return [], [f"hachoir read error: {e}"]
    finally:
        _capture.errors = None


def _groups(meta) -> Iterator[Any]:
    """Root first, then every sub-group depth first (stable order)."""
    yield meta
    iter_groups = getattr(meta, "iterGroups", None)
    if iter_groups is not None:
        for group in iter_groups():
            yield from _groups(group)


def fields_from_metadata(meta) -> list[MetadataField]:
    """Fields of a hachoir ``Metadata``/``MultipleMetadata`` object (see module doc)."""
    fields: list[MetadataField] = []
    for index, group in enumerate(_groups(meta)):
        header = str(getattr(group, "header", "") or "Metadata")
        # Same order as hachoir's own export: by priority, then registration.
        for data in sorted(group, key=lambda d: d.priority):
            if not data.values:
                continue
            f = _field(f"hachoir:{index}:{data.key}", f"{header} — {data.description}",
                       data.values)
            if f is not None:
                fields.append(f)
    return fields


def _typed(value: Any, text: str) -> tuple[str, Any]:
    if isinstance(value, bool):
        return "bool", value
    if isinstance(value, int):
        return "int", int(value)
    if isinstance(value, float):
        return ("float", value) if math.isfinite(value) else ("str", text)
    if isinstance(value, timedelta):
        seconds = value.total_seconds()
        return ("duration", seconds) if seconds >= 0 else ("str", text)
    if isinstance(value, datetime):
        aware = value.tzinfo is not None and value.utcoffset() is not None
        return ("datetime" if aware else "localdatetime"), value.isoformat()
    if isinstance(value, date):
        return "str", value.isoformat()
    if isinstance(value, (bytes, bytearray)):
        return "binary", bytes(value)
    if isinstance(value, str):
        return ("text" if "\n" in value or "\r" in value else "str"), value
    return "str", str(text)


def _field(key: str, label: str, items: list) -> MetadataField | None:
    typed = [_typed(item.value, item.text) for item in items]
    texts = [str(item.text) for item in items]
    types = {t for t, _ in typed}
    multi = len(typed) > 1
    if types == {"binary"}:
        data = [v for _, v in typed]
        return MetadataField.binary_field(key, label, data if multi else data[0],
                                          deletable=False, source="hachoir",
                                          raw_value=texts if multi else texts[0])
    if len(types) == 1:
        field_type = types.pop()
        values = [v for _, v in typed]
    elif types <= {"str", "text"}:
        field_type, values = "text", [v for _, v in typed]
    else:
        field_type, values = "str", texts
    return MetadataField(key, label, values if multi else values[0],
                         raw_value=texts if multi else texts[0],
                         editable=False, deletable=False, field_type=field_type,
                         source="hachoir", multi=multi)
