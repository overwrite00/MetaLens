"""PDF document information (/Info) with typed values and XMP synchronisation.

* Text entries are ``str``; ``/CreationDate`` and ``/ModDate`` are ``datetime``
  (``localdatetime`` when the PDF date carries no UTC offset) converted between
  ``D:YYYYMMDDHHmmSSOHH'mm'`` and ISO 8601 with strict validation.
* Names (``/Trapped``), numbers and booleans are shown read-only and keep their
  PDF type on every write (they are only deletable, with exact restore).
* Encrypted and digitally signed PDFs (``/SigFlags``, a ``/FT /Sig`` field with
  ``/V``, ``/Perms``) are read-only with ``read_only_reason`` (decision D5).
* The XMP packet properties mirroring /Info are updated with it (decision D6,
  see ``_pdf_xmp``); the rest of the packet is preserved.
* ``snapshot`` keeps the original PDF object (string bytes, name, number...)
  and every XMP occurrence of the key, so ``restore`` is exact.
* pypdf safety limits: its default maximum declared stream length (75 MB)
  rejected rewriting PDFs with one large image/attachment although they were
  served as editable (500 ``LimitReachedError``). Every operation raises THAT
  limit to the size of the file being processed (``_pypdf_limits``): the whole
  file is already in memory and streams are copied, never decompressed, so this
  adds no decompression-bomb exposure. Every other limit keeps its default; a
  file hitting one is read-only (``read_only_reason``) when found while reading
  and a 422 ``too_large`` when found while rewriting (never a 500). Other pypdf
  errors while rewriting are 422 ``unsupported``.
"""
from __future__ import annotations

import io
import math
import re
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path
from typing import Any, Iterator

from core import values
from core.base_handler import ABSENT, BaseMetadataHandler, Snapshot
from core.errors import ErrorCode, MetadataValueError, MetadataValueErrors
from core.handlers._pdf_xmp import PROPERTIES as XMP_PROPERTIES, XmpError, XmpPacket
from core.models import MetadataField, MetadataRecord

_PDF_LABELS = {
    "/Title": "Title",
    "/Author": "Author",
    "/Subject": "Subject",
    "/Keywords": "Keywords",
    "/Creator": "Creator Application",
    "/Producer": "Producer",
    "/CreationDate": "Creation Date",
    "/ModDate": "Modified Date",
    "/Trapped": "Trapped",
}
_DATE_KEYS = frozenset({"/CreationDate", "/ModDate"})
_ADDABLE = tuple(k for k in _PDF_LABELS if k != "/Trapped")
_COMPUTED = frozenset({"page_count", "encrypted", "signed"})

ENCRYPTED_REASON = "Encrypted PDF: editing is not supported"
SIGNED_REASON = "Digitally signed PDF: editing would invalidate the signature"
_MAX_FIELD_NODES = 10_000

# ──────────────────────── PDF dates ──────────────────────────────────────────

_PDF_DATE = re.compile(
    r"\s*(?:D:)?(\d{4})(\d{2})?(\d{2})?(\d{2})?(\d{2})?(\d{2})?"
    r"(?:([Zz])(?:00'?(?:00'?)?)?|([+-])(\d{2})(?:'?(\d{2}))?'?)?\s*", re.ASCII)


def parse_pdf_date(text: str) -> datetime | None:
    """PDF date string -> datetime (aware when the string has an offset), None if invalid."""
    match = _PDF_DATE.fullmatch(text or "")
    if not match:
        return None
    year, month, day, hour, minute, second, zulu, sign, off_h, off_m = match.groups()
    try:
        dt = datetime(int(year), int(month or 1), int(day or 1),
                      int(hour or 0), int(minute or 0), int(second or 0))
        if zulu:
            return dt.replace(tzinfo=timezone.utc)
        if sign:
            hours, minutes = int(off_h), int(off_m or 0)
            if hours > 23 or minutes > 59:
                return None
            offset = timedelta(hours=hours, minutes=minutes)
            return dt.replace(tzinfo=timezone(-offset if sign == "-" else offset))
    except ValueError:
        return None
    return dt


def pdf_date_to_iso(text: str) -> str | None:
    dt = parse_pdf_date(text)
    return None if dt is None else values.format_datetime(dt)


def iso_to_pdf_date(iso: str) -> str:
    """Canonical ISO value -> ``D:YYYYMMDDHHmmSS`` + ``Z`` / ``+HH'mm'`` (none when naive)."""
    dt = datetime.fromisoformat(iso)
    text = f"D:{dt.year:04d}{dt.month:02d}{dt.day:02d}{dt.hour:02d}{dt.minute:02d}{dt.second:02d}"
    offset = dt.utcoffset()
    if offset is None:
        return text
    minutes = int(offset.total_seconds()) // 60
    if minutes == 0:
        return text + "Z"
    sign = "-" if minutes < 0 else "+"
    hours, mins = divmod(abs(minutes), 60)
    return f"{text}{sign}{hours:02d}'{mins:02d}'"


# ──────────────────────── pypdf limits and errors ────────────────────────────

@contextmanager
def _pypdf_limits(path: Path) -> Iterator[None]:
    """Let pypdf read streams as long as the file itself (see the module docstring)."""
    from pypdf import apply_configuration, get_configuration
    try:
        size = Path(path).stat().st_size
    except OSError:
        size = 0
    if size <= get_configuration().maximum_declared_stream_length:
        yield
        return
    with apply_configuration(maximum_declared_stream_length=size):
        yield


def limit_reason(exc: Exception) -> str:
    return f"The PDF exceeds a safety limit of the PDF library ({exc}): its metadata is read-only"


def _rewriting(method):
    """Mutation hook wrapper: file-sized limits, pypdf errors as 422 (never 500)."""
    @wraps(method)
    def hook(self, tmp, *args, **kwargs):
        from pypdf.errors import LimitReachedError, PyPdfError
        try:
            with _pypdf_limits(tmp):
                return method(self, tmp, *args, **kwargs)
        except LimitReachedError as exc:
            raise MetadataValueError(None, ErrorCode.TOO_LARGE,
                                     f"The PDF exceeds a safety limit of the PDF library ({exc}); "
                                     "its metadata cannot be modified") from exc
        except PyPdfError as exc:
            raise MetadataValueError(None, ErrorCode.UNSUPPORTED,
                                     f"The PDF structure cannot be rewritten ({type(exc).__name__}: {exc})") from exc
    return hook


# ──────────────────────── PDF objects <-> raw snapshot ───────────────────────

def _open(path: Path):
    from pypdf import PdfReader
    # Read the bytes ourselves: no handle is kept open on the file.
    reader = PdfReader(io.BytesIO(Path(path).read_bytes()))
    if reader.is_encrypted:
        try:
            reader.decrypt("")
        except Exception:  # noqa: BLE001 - a wrong/unsupported password only limits reading
            pass
    return reader


def _raw(obj) -> tuple:
    """Comparable, deep-copyable image of a direct PDF object."""
    from pypdf.generic import (BooleanObject, ByteStringObject, FloatObject, NameObject,
                               NullObject, NumberObject, TextStringObject)
    if isinstance(obj, NameObject):
        return ("name", str(obj))
    if isinstance(obj, TextStringObject):
        return ("string", bytes(obj.original_bytes))
    if isinstance(obj, ByteStringObject):
        return ("string", bytes(obj))
    if isinstance(obj, BooleanObject):
        return ("bool", bool(obj))
    if isinstance(obj, NumberObject):
        return ("int", int(obj))
    if isinstance(obj, FloatObject):
        return ("float", str(obj))
    if isinstance(obj, NullObject):
        return ("null",)
    stream = io.BytesIO()
    obj.write_to_stream(stream)
    return ("opaque", stream.getvalue())


def _object(raw: tuple, key: str):
    from pypdf.generic import (BooleanObject, FloatObject, NameObject, NullObject, NumberObject,
                               create_string_object)
    kind = raw[0]
    if kind == "name":
        return NameObject(raw[1])
    if kind == "string":
        return create_string_object(raw[1])
    if kind == "bool":
        return BooleanObject(raw[1])
    if kind == "int":
        return NumberObject(raw[1])
    if kind == "float":
        return FloatObject(raw[1])
    if kind == "null":
        return NullObject()
    raise MetadataValueError(key, ErrorCode.UNSUPPORTED, f"{key}: this PDF value cannot be restored")


def _info_dict(info) -> dict[str, Any]:
    from pypdf.generic import DictionaryObject
    info = None if info is None else info.get_object()
    if not isinstance(info, DictionaryObject):
        return {}
    return {str(k): v.get_object() for k, v in info.items()}


def _key(name: str) -> str:
    local = name[1:]
    return f"pdf:{local}#info" if local in _COMPUTED else f"pdf:{local}"


XMP_STREAM_KEY = "pdf:@xmp"


def _xmp_bytes(root) -> bytes | None:
    """Decoded bytes of the document /Metadata stream (any filter pypdf understands), None when absent."""
    meta = root.get("/Metadata")
    if meta is None:
        return None
    try:
        return bytes(meta.get_object().get_data())
    except Exception:    # noqa: BLE001 - not a stream / undecodable: left alone
        return None


def _name(key: str) -> str:
    if not key.startswith("pdf:") or len(key) <= 4 or key[4:] in _COMPUTED or key.endswith("#info"):
        raise MetadataValueError(key, ErrorCode.UNKNOWN_FIELD, f"{key}: not a PDF information entry")
    return "/" + key[4:]


def _schema(name: str) -> MetadataField:
    """Schema of a standard entry absent from the file."""
    ftype = "datetime" if name in _DATE_KEYS else "str"
    return MetadataField(_key(name), _PDF_LABELS.get(name, name[1:]), None, field_type=ftype, source="pdf")


def _make_field(name: str, obj, warnings: list[str] | None = None) -> MetadataField | None:
    from pypdf.generic import (BooleanObject, ByteStringObject, FloatObject, NameObject,
                               NullObject, NumberObject, TextStringObject)
    key, label = _key(name), _PDF_LABELS.get(name, name[1:])
    reserved = key.endswith("#info")

    def ro(value, ftype, deletable=True):
        return MetadataField(key, label, value, raw_value=value, editable=False,
                             deletable=deletable and not reserved, field_type=ftype, source="pdf")

    if isinstance(obj, NullObject):
        return None
    if isinstance(obj, NameObject):
        return ro(str(obj)[1:], "str")
    if isinstance(obj, (TextStringObject, ByteStringObject)):
        text = str(obj) if isinstance(obj, TextStringObject) else values.decode_text(bytes(obj))
        if reserved or name == "/Trapped":
            return ro(text, "str")
        if name in _DATE_KEYS:
            dt = parse_pdf_date(text)
            if dt is None:
                if warnings is not None:
                    warnings.append(f"{key}: unparsable PDF date {text!r} shown as read-only text")
                return ro(text, "str")
            return MetadataField(key, label, values.format_datetime(dt), raw_value=text,
                                 field_type="datetime" if dt.tzinfo else "localdatetime", source="pdf")
        return MetadataField(key, label, text, raw_value=text, field_type="str", source="pdf")
    if isinstance(obj, BooleanObject):
        return ro(bool(obj), "bool")
    if isinstance(obj, NumberObject):
        return ro(int(obj), "int")
    if isinstance(obj, FloatObject) and math.isfinite(float(obj)):
        return ro(float(obj), "float")
    return ro(f"<{type(obj).__name__}>", "str", deletable=False)


# ──────────────────────── signatures and XMP ─────────────────────────────────

def signature_reason(reader) -> str | None:
    """Why the PDF counts as signed (D5), or None."""
    from pypdf.generic import ArrayObject, DictionaryObject
    root = reader.trailer["/Root"].get_object()
    if "/Perms" in root:
        return "certification or usage-rights signature (/Perms)"
    acro = root.get("/AcroForm")
    acro = None if acro is None else acro.get_object()
    if not isinstance(acro, DictionaryObject):
        return None
    try:
        flags = int(acro.get("/SigFlags", 0))
    except (TypeError, ValueError):
        flags = 0
    if flags & 3:
        return "signatures present (/SigFlags)"
    fields = acro.get("/Fields")
    fields = None if fields is None else fields.get_object()
    stack = [(f, None) for f in fields] if isinstance(fields, ArrayObject) else []
    seen: set[int] = set()
    while stack and len(seen) < _MAX_FIELD_NODES:
        ref, inherited = stack.pop()
        node = ref.get_object()
        if not isinstance(node, DictionaryObject) or id(node) in seen:
            continue
        seen.add(id(node))
        ftype = node.get("/FT", inherited)
        if ftype == "/Sig" and "/V" in node and node["/V"].get_object() is not None:
            from pypdf.generic import NullObject
            if not isinstance(node["/V"].get_object(), NullObject):
                return "signed signature field (/FT /Sig with /V)"
        kids = node.get("/Kids")
        kids = None if kids is None else kids.get_object()
        if isinstance(kids, ArrayObject):
            stack.extend((k, ftype) for k in kids)
    return None


def _xmp_of(root) -> tuple[XmpPacket | None, Any, str | None]:
    """(packet, stream, warning) of the document XMP; packet None when absent/unusable."""
    from pypdf.generic import ArrayObject, StreamObject
    meta = root.get("/Metadata")
    if meta is None:
        return None, None, None
    stream = meta.get_object()
    if not isinstance(stream, StreamObject):
        return None, None, "XMP metadata entry is not a stream: it will not be updated"
    filters = stream.get("/Filter")
    filters = [] if filters is None else list(filters) if isinstance(filters, ArrayObject) else [filters]
    if any(str(f) != "/FlateDecode" for f in filters) or len(filters) > 1:
        return None, None, "XMP metadata uses an unsupported filter: it will not be updated"
    try:
        return XmpPacket(stream.get_data()), stream, None
    except XmpError as exc:
        return None, None, f"XMP metadata cannot be parsed ({exc}): it will not be updated"
    except Exception as exc:  # noqa: BLE001 - decoding errors of the stream
        return None, None, f"XMP metadata cannot be decoded ({exc}): it will not be updated"


class _Mutation:
    """Writable clone of a PDF; ``save`` rewrites the file in place."""

    def __init__(self, path: Path) -> None:
        from pypdf import PdfWriter
        self.path = Path(path)
        reader = _open(self.path)
        if reader.is_encrypted:
            raise MetadataValueError(None, ErrorCode.READ_ONLY, "Editing encrypted PDFs is not supported")
        reason = signature_reason(reader)
        if reason:
            raise MetadataValueError(None, ErrorCode.READ_ONLY,
                                     f"Editing digitally signed PDFs is not supported: {reason}")
        self.writer = PdfWriter(clone_from=reader)
        self.xmp, self.xmp_stream, _ = _xmp_of(self.writer._root_object)
        self.changed = self.xmp_changed = False

    def get(self, name: str):
        return _info_dict(self.writer._info).get(name)

    def set(self, name: str, obj) -> None:
        from pypdf.generic import DictionaryObject, NameObject
        if self.writer._info is None:
            self.writer._info = DictionaryObject()
        self.writer._info[NameObject(name)] = obj
        self.changed = True

    def remove(self, name: str) -> None:
        info = self.writer._info
        if info is not None and name in info:
            del info[name]
            self.changed = True

    def xmp_apply(self, name: str, fn) -> None:
        if self.xmp is not None and name in XMP_PROPERTIES:
            fn(self.xmp)
            self.xmp_changed = self.changed = True

    def save(self) -> None:
        if not self.changed:
            return
        if self.xmp_changed:
            self.xmp_stream.set_data(self.xmp.to_bytes())
        buffer = io.BytesIO()
        self.writer.write(buffer)
        # r+b + truncate: never recreate the target (Windows refuses 'wb' on hidden files).
        with self.path.open("r+b") as stream:
            stream.write(buffer.getvalue())
            stream.truncate()


def _raise_collected(errors: list[MetadataValueError]) -> None:
    if len(errors) == 1:
        raise errors[0]
    if errors:
        raise MetadataValueErrors(errors)


def _attributed(exc: MetadataValueError, key: str) -> MetadataValueError:
    return exc if exc.key else exc.with_key(key)


# ──────────────────────── handler ────────────────────────────────────────────

def _xmp_stream_fields(root, removable: bool) -> list[MetadataField]:
    """The XMP properties of the document (read-only) and the whole stream (removable, computed)."""
    data = _xmp_bytes(root)
    if not data:
        return []
    from core import xmp as core_xmp
    from core.handlers.image_handler import _xmp_fields
    out: list[MetadataField] = []
    try:
        out += [f for f in _xmp_fields(core_xmp.XmpPacket(data), False) if f.key != "xmp:@hidden"]
        for f in out:
            f.computed = True          # they disappear with the stream
    except core_xmp.XmpFormatError:
        pass  # a malformed XMP stream is reported elsewhere; here it just adds no fields
    stream = MetadataField.binary_field(XMP_STREAM_KEY, "XMP metadata stream", data, mime="application/rdf+xml",
                                        deletable=removable, source="pdf")
    stream.computed = True          # the entries of the information dictionary are mirrored into it
    return out + [stream]


def _file_structure_fields(path: Path) -> list[MetadataField]:
    """Version of the file header and whether it is linearized (computed: a rewrite changes them)."""
    try:
        with open(path, "rb") as stream:
            head = stream.read(2048)
    except OSError:
        return []
    out: list[MetadataField] = []
    start = head.find(b"%PDF-")
    if start >= 0:
        version = head[start + 5:start + 8].decode("ascii", "replace")
        if len(version) == 3 and version[0].isdigit() and version[1] == "." and version[2].isdigit():
            out.append(MetadataField("pdf:PDFVersion", "PDF Version", version, raw_value=version, editable=False,
                                     deletable=False, field_type="str", source="pdf", computed=True))
    out.append(MetadataField("pdf:Linearized", "Linearized", b"/Linearized" in head, editable=False,
                             deletable=False, field_type="bool", source="pdf", computed=True))
    return out


class PdfHandler(BaseMetadataHandler):
    NAME = "pdf"
    EXTENSIONS = frozenset({".pdf"})
    PRIORITY = 15

    def writable_field(self, path: Path, key: str) -> MetadataField | None:
        try:
            name = _name(key)
        except MetadataValueError:
            return None
        return _schema(name) if name in _ADDABLE else None

    def writable_fields(self, path: Path, record: MetadataRecord) -> list[MetadataField]:
        if not record.format_supports_write or record.read_errors:
            return []
        present = {f.key for f in record.fields}
        return [_schema(n) for n in _ADDABLE if _key(n) not in present]

    def read(self, path: Path) -> MetadataRecord:
        with _pypdf_limits(path):
            return self._read(path)

    def _read(self, path: Path) -> MetadataRecord:
        from pypdf.errors import LimitReachedError
        fields: list[MetadataField] = []
        errors: list[str] = []
        warnings: list[str] = []
        reason = None
        try:
            reader = _open(path)
            encrypted = bool(reader.is_encrypted)
            signed = signature_reason(reader)
            try:
                info = _info_dict(reader.trailer.get("/Info"))
            except Exception as exc:  # noqa: BLE001 - encrypted with a user password
                if not encrypted:
                    raise
                info = {}
                warnings.append(f"Encrypted PDF: metadata cannot be read without the password ({exc})")
            for name, obj in info.items():
                field = _make_field(name, obj, warnings)
                if field is not None:
                    fields.append(field)
            if not encrypted:
                warning = _xmp_of(reader.trailer["/Root"].get_object())[2]
                if warning:
                    warnings.append(warning)
            if not encrypted:
                fields += _xmp_stream_fields(reader.trailer["/Root"].get_object(), signed is None)
            fields += _file_structure_fields(path)
            fields.append(MetadataField("pdf:page_count", "Page Count", len(reader.pages),
                                        editable=False, deletable=False, field_type="int", source="pdf"))
            fields.append(MetadataField("pdf:encrypted", "Encrypted", encrypted,
                                        editable=False, deletable=False, field_type="bool", source="pdf"))
            fields.append(MetadataField("pdf:signed", "Digitally Signed", signed is not None,
                                        editable=False, deletable=False, field_type="bool", source="pdf"))
            reason = ENCRYPTED_REASON if encrypted else SIGNED_REASON if signed else None
        except LimitReachedError as e:
            # Read-only with the reason, not a read error: the fields read so far are kept.
            reason = limit_reason(e)
        except Exception as e:  # read never raises
            errors.append(f"PDF read error: {e}")

        writable = not errors and reason is None
        record = self._make_record(path, read_errors=errors,
                                   supports_write=writable, supports_delete=writable)
        if not writable:
            for field in fields:
                field.editable = field.deletable = False
        record.read_only_reason = reason if not errors else None
        record.warnings.extend(warnings)
        record.fields = fields
        record.dedupe_keys()
        return record

    # ── validation ───────────────────────────────────────────────────────────

    def canonical(self, field: MetadataField, value: Any) -> Any:
        result = super().canonical(field, value)
        if field.field_type in ("str", "text"):
            try:
                result.encode("utf-16-be")
            except UnicodeEncodeError:
                raise MetadataValueError(field.key, ErrorCode.INVALID_VALUE,
                                         f"{field.key}: text contains unpaired surrogates") from None
        elif field.field_type in ("datetime", "localdatetime"):
            dt = datetime.fromisoformat(result)
            offset = dt.utcoffset()
            if dt.microsecond or (offset is not None and offset.total_seconds() % 60):
                raise MetadataValueError(field.key, ErrorCode.INVALID_VALUE,
                                         f"{field.key}: PDF dates have a precision of one second "
                                         "and offsets in whole minutes")
        return result

    def _current_schema(self, key: str, name: str, current) -> MetadataField:
        field = None if current is None else _make_field(name, current)
        if field is None:
            if name == "/Trapped":
                raise MetadataValueError(key, ErrorCode.READ_ONLY)
            return _schema(name)
        if not field.editable:
            raise MetadataValueError(key, ErrorCode.READ_ONLY, f"{key}: read-only PDF entry")
        return field

    # ── mutation hooks ───────────────────────────────────────────────────────

    @_rewriting
    def apply_write(self, tmp: Path, fields: list[MetadataField]) -> None:
        from pypdf.generic import TextStringObject
        doc = _Mutation(tmp)
        errors: list[MetadataValueError] = []
        plan: list[tuple[str, str, Any, str]] = []
        for f in fields:
            try:
                name = _name(f.key)
                current = doc.get(name)
                schema = self._current_schema(f.key, name, current)
                value = self.canonical(schema, f.value)
                if current is not None and schema.value == value:
                    continue  # unchanged: keep the original bytes
                plan.append((f.key, name, value, schema.field_type))
            except MetadataValueError as exc:
                errors.append(_attributed(exc, f.key))
        _raise_collected(errors)
        if not plan:
            return
        for key, name, value, ftype in plan:
            text = value if ftype in ("str", "text") else iso_to_pdf_date(value)
            doc.set(name, TextStringObject(text))
            doc.xmp_apply(name, lambda x, name=name, value=value: x.set(name, value))
        doc.save()
        # Read-after-write verification (G4): the library must not alter values.
        info = _info_dict(_open(tmp).trailer.get("/Info"))
        for key, name, value, _ftype in plan:
            got = _make_field(name, info[name]) if name in info else None
            if got is None or got.value != value:
                raise MetadataValueError(key, ErrorCode.LOSSY_WRITE,
                                         f"{key}: value not stored exactly by the PDF writer")

    @_rewriting
    def apply_delete(self, tmp: Path, keys: list[str]) -> None:
        doc = _Mutation(tmp)
        errors: list[MetadataValueError] = []
        names = []
        if XMP_STREAM_KEY in keys:
            keys = [k for k in keys if k != XMP_STREAM_KEY]
            if "/Metadata" not in doc.writer._root_object:
                raise MetadataValueError(XMP_STREAM_KEY, ErrorCode.UNKNOWN_FIELD)
            try:
                doc.writer._root_object["/Metadata"].get_object().set_data(b"")     # an orphaned object is still written
            except Exception:      # noqa: BLE001 - not a stream: only the entry goes
                pass
            del doc.writer._root_object["/Metadata"]
            doc.xmp = doc.xmp_stream = None
            doc.changed = True
        for key in keys:
            try:
                name = _name(key)
                current = doc.get(name)
                field = None if current is None else _make_field(name, current)
                if field is not None and not field.deletable:
                    raise MetadataValueError(key, ErrorCode.NOT_DELETABLE)
                if current is not None:
                    names.append(name)
            except MetadataValueError as exc:
                errors.append(_attributed(exc, key))
        _raise_collected(errors)
        for name in names:
            doc.remove(name)
            doc.xmp_apply(name, lambda x, name=name: x.remove(name))
        doc.save()

    @_rewriting
    def snapshot(self, tmp: Path, keys: list[str]) -> Snapshot:
        reader = _open(tmp)
        info = _info_dict(reader.trailer.get("/Info"))
        xmp = None if reader.is_encrypted else _xmp_of(reader.trailer["/Root"].get_object())[0]
        snap: Snapshot = {}
        for key in keys:
            if key == XMP_STREAM_KEY:
                data = None if reader.is_encrypted else _xmp_bytes(reader.trailer["/Root"].get_object())
                snap[key] = ABSENT if data is None else ("xmp-stream", data)
                continue
            name = _name(key)
            info_raw = _raw(info[name]) if name in info else None
            occurrences = xmp.snapshot(name) if xmp is not None and name in XMP_PROPERTIES else ()
            snap[key] = ABSENT if info_raw is None and not occurrences else (info_raw, occurrences)
        return snap

    @_rewriting
    def restore(self, tmp: Path, snap: Snapshot) -> None:
        doc = _Mutation(tmp)
        for key, raw in snap.items():
            if key == XMP_STREAM_KEY:
                from pypdf.generic import DecodedStreamObject, NameObject
                root = doc.writer._root_object
                if raw is ABSENT:
                    if "/Metadata" in root:
                        del root["/Metadata"]
                else:
                    stream = DecodedStreamObject()
                    stream.set_data(raw[1])
                    stream[NameObject("/Type")] = NameObject("/Metadata")
                    stream[NameObject("/Subtype")] = NameObject("/XML")
                    root[NameObject("/Metadata")] = doc.writer._add_object(stream)
                doc.xmp = doc.xmp_stream = None
                doc.changed = True
                continue
            name = _name(key)
            info_raw, occurrences = (None, ()) if raw is ABSENT else raw
            if info_raw is None:
                doc.remove(name)
            else:
                doc.set(name, _object(info_raw, key))
            doc.xmp_apply(name, lambda x, name=name, occ=occurrences: x.restore(name, occ))
        doc.save()

    # ── public wrappers (own transaction) ────────────────────────────────────

    def write(self, path: Path, fields: list[MetadataField]) -> None:
        self._atomic_write(path, lambda tmp: self.apply_write(tmp, fields))

    def delete(self, path: Path, keys: list[str]) -> None:
        self._atomic_write(path, lambda tmp: self.apply_delete(tmp, keys))

