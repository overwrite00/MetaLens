"""Strict parsing and canonical JSON values for metadata fields (plan B1).

``MetadataField.value`` is always JSON-native and canonical:

=================  ==========================================================
field_type         canonical value
=================  ==========================================================
str / text         ``str`` (``text`` = multi-line)
int                ``int`` (never ``bool``)
float              finite ``float``
bool               ``bool`` — parsed strictly ("False" -> False)
datetime           ISO 8601 string WITH UTC offset ("2026-01-02T03:04:05+02:00")
localdatetime      ISO 8601 string WITHOUT offset (EXIF wall-clock time)
rational           exact "n/d" string, never reduced ("1234/100" stays)
duration           seconds as a non-negative number
binary             ``{"size", "sha256", "mime"}`` descriptor — never editable
enum               one of ``field.choices``
=================  ==========================================================

``multi=True`` means the value is a list of such items (never a joined string);
``length`` fixes the list size (pairs/tuples such as track ``[3, 10]``).

Low-level parsers raise ``MetadataValueError(key=None, code)``; ``canonicalize``
attributes the error to the field key. ``null`` is never accepted.
"""
from __future__ import annotations

import hashlib
import math
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any, Iterable

from core.errors import ErrorCode, MetadataValueError

if TYPE_CHECKING:  # pragma: no cover
    from core.models import MetadataField

__all__ = [
    "FIELD_TYPES", "EPOCH",
    "parse_bool", "parse_int", "parse_float", "parse_duration", "parse_str",
    "parse_rational", "format_rational", "canonical_rational",
    "parse_datetime", "parse_localdatetime", "format_datetime",
    "utc_iso_from_ns", "datetime_from_ns", "ns_from_iso", "ns_from_datetime",
    "decode_text", "binary_descriptor", "canonicalize",
]

FIELD_TYPES = frozenset({
    "str", "text", "int", "float", "bool", "datetime", "localdatetime",
    "rational", "duration", "binary", "enum",
})

EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

_INT_RE = re.compile(r"[+-]?[0-9]+", re.ASCII)
_FLOAT_RE = re.compile(r"[+-]?(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?", re.ASCII)
_RATIONAL_RE = re.compile(r"\s*([+-]?[0-9]+)\s*/\s*([0-9]+)\s*", re.ASCII)
_DECIMAL_RE = re.compile(r"\s*([+-]?(?:[0-9]+\.?[0-9]*|\.[0-9]+))\s*", re.ASCII)
_TRUE = frozenset({"true", "1", "yes", "on"})
_FALSE = frozenset({"false", "0", "no", "off"})


def _fail(code: str, message: str) -> MetadataValueError:
    return MetadataValueError(None, code, message)


def _null_check(value: Any) -> None:
    if value is None:
        raise _fail(ErrorCode.NULL_VALUE, "null is not a valid value")


# ──────────────────────── scalars ────────────────────────────────────────────

def parse_bool(value: Any) -> bool:
    """bool, 0/1, or "true/false/1/0/yes/no/on/off" (case-insensitive). Nothing else."""
    _null_check(value)
    if isinstance(value, bool):
        return value
    if type(value) is int and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in _TRUE:
            return True
        if text in _FALSE:
            return False
    raise _fail(ErrorCode.INVALID_VALUE, f"not a boolean: {value!r}")


def _check_range(value, minimum, maximum):
    if (minimum is not None and value < minimum) or (maximum is not None and value > maximum):
        raise _fail(ErrorCode.OUT_OF_RANGE,
                    f"{value} outside [{'' if minimum is None else minimum}, "
                    f"{'' if maximum is None else maximum}]")
    return value


def parse_int(value: Any, *, minimum: int | None = None, maximum: int | None = None) -> int:
    """int, an integral finite float, or an ASCII decimal string ("+7", "-12")."""
    _null_check(value)
    if isinstance(value, bool):
        raise _fail(ErrorCode.WRONG_TYPE, f"not an integer: {value!r}")
    if isinstance(value, int):
        result = int(value)
    elif isinstance(value, float) and math.isfinite(value) and value.is_integer():
        result = int(value)
    elif isinstance(value, str) and _INT_RE.fullmatch(value.strip()):
        result = int(value.strip())
    else:
        raise _fail(ErrorCode.INVALID_VALUE, f"not an integer: {value!r}")
    return _check_range(result, minimum, maximum)


def parse_float(value: Any, *, minimum: float | None = None, maximum: float | None = None) -> float:
    """Finite number or ASCII decimal/scientific string. NaN and infinities are rejected."""
    _null_check(value)
    if isinstance(value, bool):
        raise _fail(ErrorCode.WRONG_TYPE, f"not a number: {value!r}")
    if isinstance(value, (int, float)):
        result = float(value)
    elif isinstance(value, str) and _FLOAT_RE.fullmatch(value.strip()):
        result = float(value.strip())
    else:
        raise _fail(ErrorCode.INVALID_VALUE, f"not a number: {value!r}")
    if not math.isfinite(result):
        raise _fail(ErrorCode.INVALID_VALUE, f"not a finite number: {value!r}")
    return _check_range(result, minimum, maximum)


def parse_duration(value: Any) -> float:
    """Seconds (number, numeric string or ``timedelta``), >= 0, as float."""
    _null_check(value)
    if isinstance(value, timedelta):
        value = value.total_seconds()
    return parse_float(value, minimum=0.0)


def parse_str(value: Any, *, allow_empty: bool = True, max_length: int | None = None) -> str:
    """A ``str`` (no coercion of numbers). ``allow_empty=False`` rejects ''."""
    _null_check(value)
    if not isinstance(value, str):
        raise _fail(ErrorCode.WRONG_TYPE, f"not a string: {value!r}")
    if not value and not allow_empty:
        raise _fail(ErrorCode.EMPTY_VALUE, "empty value; delete the field instead")
    if max_length is not None and len(value) > max_length:
        raise _fail(ErrorCode.TOO_LARGE, f"{len(value)} characters, limit {max_length}")
    return value


# ──────────────────────── rationals ──────────────────────────────────────────

def _rational_limits(signed: bool) -> tuple[int, int]:
    return (-(2 ** 31), 2 ** 31 - 1) if signed else (0, 2 ** 32 - 1)


def parse_rational(value: Any, *, signed: bool = False) -> tuple[int, int]:
    """Exact (numerator, denominator) — never reduced.

    Accepts "n/d", an int, ``[n, d]``/``(n, d)``, or a decimal (string or float) which
    is converted exactly with a power-of-ten denominator ("12.34" -> (1234, 100)).
    Ranges follow TIFF: RATIONAL = 2 x uint32, SRATIONAL = 2 x int32 (``signed=True``);
    the sign is always on the numerator. A zero denominator is allowed only as "0/0"
    (the TIFF "unknown" value).
    """
    _null_check(value)
    if isinstance(value, bool):
        raise _fail(ErrorCode.WRONG_TYPE, f"not a rational: {value!r}")
    num = den = None
    if isinstance(value, int):
        num, den = value, 1
    elif isinstance(value, (list, tuple)):
        if len(value) == 2 and all(type(v) is int for v in value):
            num, den = value
    elif isinstance(value, float):
        if math.isfinite(value):
            num, den = _decimal_ratio(repr(value))
    elif isinstance(value, str):
        match = _RATIONAL_RE.fullmatch(value)
        if match:
            num, den = int(match.group(1)), int(match.group(2))
        elif _DECIMAL_RE.fullmatch(value):
            num, den = _decimal_ratio(value.strip())
    if num is None:
        raise _fail(ErrorCode.INVALID_VALUE, f"not a rational: {value!r}")
    low, high = _rational_limits(signed)
    if den < 0:
        raise _fail(ErrorCode.INVALID_VALUE, f"negative denominator: {value!r}")
    if den == 0 and num != 0:
        raise _fail(ErrorCode.INVALID_VALUE, f"zero denominator: {value!r}")
    if not (low <= num <= high) or not (0 <= den <= high):
        raise _fail(ErrorCode.OUT_OF_RANGE, f"rational {num}/{den} outside the format range")
    return num, den


def _decimal_ratio(text: str) -> tuple[int, int] | tuple[None, None]:
    try:
        dec = Decimal(text)
    except InvalidOperation:
        return None, None
    if not dec.is_finite():
        return None, None
    sign, digits, exponent = dec.as_tuple()
    num = int("".join(map(str, digits)) or "0")
    den = 1
    if exponent >= 0:
        num *= 10 ** exponent
    else:
        den = 10 ** -exponent
    return (-num if sign else num), den


def format_rational(num: int, den: int) -> str:
    return f"{num}/{den}"


def canonical_rational(value: Any, *, signed: bool = False) -> str:
    return format_rational(*parse_rational(value, signed=signed))


# ──────────────────────── dates and times ────────────────────────────────────

def _parse_iso(value: Any) -> datetime:
    _null_check(value)
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        raise _fail(ErrorCode.WRONG_TYPE, f"not an ISO 8601 date-time: {value!r}")
    text = value.strip()
    # A date alone is not a date-time; EXIF "YYYY:MM:DD" is not ISO 8601.
    if len(text) < 16 or text[10:11] not in ("T", " "):
        raise _fail(ErrorCode.INVALID_VALUE, f"not an ISO 8601 date-time: {value!r}")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        raise _fail(ErrorCode.INVALID_VALUE, f"not an ISO 8601 date-time: {value!r}") from None


def parse_datetime(value: Any) -> datetime:
    """Timezone-aware date-time; an ISO string without offset is rejected (D7)."""
    dt = _parse_iso(value)
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise _fail(ErrorCode.MISSING_OFFSET, f"date-time without UTC offset: {value!r}")
    return dt


def parse_localdatetime(value: Any) -> datetime:
    """Naive wall-clock date-time (EXIF); an offset is rejected rather than dropped."""
    dt = _parse_iso(value)
    if dt.tzinfo is not None:
        raise _fail(ErrorCode.UNEXPECTED_OFFSET, f"local date-time must not carry an offset: {value!r}")
    return dt


def format_datetime(dt: datetime) -> str:
    """Canonical ISO 8601 (``+00:00`` for UTC, microseconds only when non-zero)."""
    return dt.isoformat()


def datetime_from_ns(ns: int) -> datetime:
    """UTC datetime from nanoseconds since the epoch, valid before 1970 on Windows.

    ``datetime.fromtimestamp`` fails for negative values on Windows; epoch + timedelta
    does not. Precision is truncated (floored) to microseconds.
    """
    return EPOCH + timedelta(microseconds=ns // 1000)


def utc_iso_from_ns(ns: int) -> str:
    return format_datetime(datetime_from_ns(ns))


def ns_from_datetime(dt: datetime) -> int:
    """Exact nanoseconds since the epoch for an aware datetime (no float rounding)."""
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise _fail(ErrorCode.MISSING_OFFSET, f"date-time without UTC offset: {dt.isoformat()}")
    delta = dt - EPOCH
    return (delta.days * 86_400 + delta.seconds) * 10**9 + delta.microseconds * 1000


def ns_from_iso(value: Any) -> int:
    return ns_from_datetime(parse_datetime(value))


# ──────────────────────── text and binary ────────────────────────────────────

def decode_text(data: bytes, encodings: Iterable[str] = ("utf-8",)) -> str:
    """Decode with the first strict encoding that works, else Latin-1.

    Latin-1 maps every byte, so nothing is replaced by U+FFFD and the text
    re-encodes to the same bytes; exact restoration still relies on raw snapshots.
    """
    for encoding in encodings:
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("latin-1")


def binary_descriptor(data: bytes, mime: str | None = None) -> dict:
    """Value of a binary field: ``{"size", "sha256", "mime"}``."""
    return {"size": len(data), "sha256": hashlib.sha256(data).hexdigest(), "mime": mime}


# ──────────────────────── canonicalize(field, value) ─────────────────────────

def _canonical_item(field: "MetadataField", value: Any, allow_empty: bool) -> Any:
    t = field.field_type
    if t in ("str", "text"):
        return parse_str(value, allow_empty=allow_empty)
    if t == "int":
        return parse_int(value)
    if t == "float":
        return parse_float(value)
    if t == "bool":
        return parse_bool(value)
    if t == "rational":
        return canonical_rational(value)
    if t == "datetime":
        return format_datetime(parse_datetime(value))
    if t == "localdatetime":
        return format_datetime(parse_localdatetime(value))
    if t == "duration":
        return parse_duration(value)
    if t == "enum":
        return _canonical_choice(field.choices, value)
    if t == "binary":
        raise _fail(ErrorCode.READ_ONLY, "binary values are read-only")
    raise _fail(ErrorCode.UNSUPPORTED, f"unsupported field type {t!r}")


def _canonical_choice(choices: list, value: Any) -> Any:
    _null_check(value)
    for choice in choices:
        if type(choice) is type(value) and choice == value:
            return choice
    if isinstance(value, str):  # numeric enum sent as text: "6" -> 6
        for choice in choices:
            if type(choice) is int:
                try:
                    if parse_int(value) == choice:
                        return choice
                except MetadataValueError:
                    break
    raise _fail(ErrorCode.INVALID_CHOICE, f"{value!r} is not one of {list(choices)!r}")


def canonicalize(field: "MetadataField", value: Any, *, allow_empty: bool = True) -> Any:
    """Validate ``value`` for ``field`` and return its canonical JSON form.

    Uses ``field.field_type``, ``multi``, ``length`` and ``choices``. Raises
    ``MetadataValueError`` with ``key=field.key``. ``allow_empty=False`` rejects
    empty strings for formats that cannot store them.
    """
    try:
        _null_check(value)
        if field.field_type not in FIELD_TYPES:
            raise _fail(ErrorCode.UNSUPPORTED, f"unsupported field type {field.field_type!r}")
        if not (field.multi or field.length is not None):
            return _canonical_item(field, value, allow_empty)
        if not isinstance(value, (list, tuple)):
            raise _fail(ErrorCode.WRONG_TYPE, f"a list is required, got {value!r}")
        if field.length is not None and len(value) != field.length:
            raise _fail(ErrorCode.WRONG_LENGTH, f"{len(value)} items, exactly {field.length} required")
        if not value:
            raise _fail(ErrorCode.EMPTY_VALUE, "empty list; delete the field instead")
        return [_canonical_item(field, item, allow_empty) for item in value]
    except MetadataValueError as exc:
        raise exc.with_key(field.key) from exc
