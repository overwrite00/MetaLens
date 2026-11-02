"""Unit tests for core.values: strict parsing and canonical JSON values (plan B1)."""
from datetime import datetime, timedelta, timezone
import json

import pytest

from core import values
from core.errors import ErrorCode, MetadataValueError
from core.models import MetadataField


# ──────────────────────── booleans ───────────────────────────────────────────

@pytest.mark.parametrize("raw, expected", [
    (True, True), (False, False), (1, True), (0, False),
    ("true", True), ("False", False), ("  TRUE ", True), ("false", False),
    ("1", True), ("0", False), ("yes", True), ("no", False),
    ("on", True), ("off", False),
])
def test_parse_bool_accepts_strict_forms(raw, expected):
    assert values.parse_bool(raw) is expected


def test_parse_bool_false_string_is_false():
    # bool("False") is True: the classic bug this parser exists to prevent.
    assert values.parse_bool("False") is False


@pytest.mark.parametrize("raw", [None, "", "maybe", "2", 2, -1, 0.5, 1.0, [], [False], {}])
def test_parse_bool_rejects_everything_else(raw):
    with pytest.raises(MetadataValueError):
        values.parse_bool(raw)


# ──────────────────────── integers and floats ────────────────────────────────

@pytest.mark.parametrize("raw, expected", [
    (5, 5), (-3, -3), ("121", 121), (" +7 ", 7), ("-12", -12), (3.0, 3),
])
def test_parse_int(raw, expected):
    result = values.parse_int(raw)
    assert result == expected and type(result) is int


@pytest.mark.parametrize("raw", [None, True, False, "", "1.5", "12a", "0x10", 1.5,
                                 float("nan"), float("inf"), [1], "١٢"])
def test_parse_int_rejects(raw):
    with pytest.raises(MetadataValueError):
        values.parse_int(raw)


def test_parse_int_range():
    assert values.parse_int(255, minimum=0, maximum=255) == 255
    with pytest.raises(MetadataValueError) as exc:
        values.parse_int(256, minimum=0, maximum=255)
    assert exc.value.code == ErrorCode.OUT_OF_RANGE
    with pytest.raises(MetadataValueError):
        values.parse_int(-1, minimum=0)


@pytest.mark.parametrize("raw, expected", [(1.5, 1.5), (2, 2.0), ("3.25", 3.25), ("-1e3", -1000.0)])
def test_parse_float(raw, expected):
    result = values.parse_float(raw)
    assert result == expected and type(result) is float


@pytest.mark.parametrize("raw", [None, True, "", "abc", float("nan"), float("inf"), "nan", "inf"])
def test_parse_float_rejects(raw):
    with pytest.raises(MetadataValueError):
        values.parse_float(raw)


# ──────────────────────── rationals ──────────────────────────────────────────

def test_rational_is_exact_and_not_reduced():
    assert values.parse_rational("1234/100") == (1234, 100)
    assert values.format_rational(1234, 100) == "1234/100"
    assert values.canonical_rational("1234/100") == "1234/100"


@pytest.mark.parametrize("raw, expected", [
    ("72/1", (72, 1)), (" 7 / 3 ", (7, 3)), (72, (72, 1)), ([10, 4], (10, 4)),
    ((10, 4), (10, 4)), ("12.34", (1234, 100)), ("0.5", (5, 10)), (0.5, (5, 10)),
    ("3", (3, 1)), ("0/0", (0, 0)),
])
def test_parse_rational_forms(raw, expected):
    assert values.parse_rational(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "a/b", "1/0", "1/2/3", "-1/2", True, "nan",
                                 [1], [1, 2, 3], 2 ** 32, "4294967296/1"])
def test_parse_rational_rejects_unsigned(raw):
    with pytest.raises(MetadataValueError):
        values.parse_rational(raw)


def test_parse_rational_signed():
    assert values.parse_rational("-1/3", signed=True) == (-1, 3)
    assert values.parse_rational(-0.25, signed=True) == (-25, 100)
    with pytest.raises(MetadataValueError):
        values.parse_rational(str(2 ** 31) + "/1", signed=True)
    with pytest.raises(MetadataValueError):
        values.parse_rational("1/-3", signed=True)  # denominator sign is not canonical


# ──────────────────────── dates and times ────────────────────────────────────

def test_utc_iso_from_ns_before_epoch_is_windows_safe():
    # datetime.fromtimestamp() raises OSError on Windows for negative values.
    assert values.utc_iso_from_ns(-2208988800 * 10**9) == "1900-01-01T00:00:00+00:00"


def test_utc_iso_from_ns_and_back():
    assert values.utc_iso_from_ns(0) == "1970-01-01T00:00:00+00:00"
    assert values.utc_iso_from_ns(1_500_000) == "1970-01-01T00:00:00.001500+00:00"
    for ns in (0, -2208988800 * 10**9, 1_767_225_600_123_456_000):
        assert values.ns_from_iso(values.utc_iso_from_ns(ns)) == ns
    assert values.ns_from_iso("1970-01-01T01:00:00+01:00") == 0


def test_ns_from_iso_requires_offset():
    with pytest.raises(MetadataValueError) as exc:
        values.ns_from_iso("2026-01-01T00:00:00")
    assert exc.value.code == ErrorCode.MISSING_OFFSET


def test_parse_datetime_requires_offset():
    dt = values.parse_datetime("2026-01-02T03:04:05+02:00")
    assert dt.utcoffset() == timedelta(hours=2)
    assert values.parse_datetime("2026-01-02T03:04:05Z").utcoffset() == timedelta(0)
    with pytest.raises(MetadataValueError) as exc:
        values.parse_datetime("2026-01-02T03:04:05")
    assert exc.value.code == ErrorCode.MISSING_OFFSET
    with pytest.raises(MetadataValueError):
        values.parse_datetime(datetime(2026, 1, 2, 3, 4, 5))


def test_parse_localdatetime_rejects_offset():
    dt = values.parse_localdatetime("2026-01-02 03:04:05")
    assert dt == datetime(2026, 1, 2, 3, 4, 5) and dt.tzinfo is None
    with pytest.raises(MetadataValueError) as exc:
        values.parse_localdatetime("2026-01-02T03:04:05+01:00")
    assert exc.value.code == ErrorCode.UNEXPECTED_OFFSET


@pytest.mark.parametrize("raw", [None, "", "garbage", "2026-13-01T00:00:00+00:00", "2026-01-01",
                                 "2026:01:01 12:00:00", 1700000000, True])
def test_parse_datetime_rejects_garbage(raw):
    with pytest.raises(MetadataValueError):
        values.parse_datetime(raw)
    with pytest.raises(MetadataValueError):
        values.parse_localdatetime(raw)


def test_format_datetime_is_iso():
    tz = timezone(timedelta(hours=-5))
    assert values.format_datetime(datetime(2026, 1, 2, 3, 4, 5, tzinfo=tz)) == "2026-01-02T03:04:05-05:00"
    assert values.format_datetime(datetime(2026, 1, 2, 3, 4, 5)) == "2026-01-02T03:04:05"


def test_parse_duration():
    assert values.parse_duration("90") == 90.0
    assert values.parse_duration(1.5) == 1.5
    assert values.parse_duration(timedelta(minutes=2)) == 120.0
    for raw in (-1, "x", None, True):
        with pytest.raises(MetadataValueError):
            values.parse_duration(raw)


# ──────────────────────── strings, text, binary ──────────────────────────────

def test_parse_str():
    assert values.parse_str("MetaLens è 日本 €") == "MetaLens è 日本 €"
    assert values.parse_str("") == ""
    with pytest.raises(MetadataValueError) as exc:
        values.parse_str("", allow_empty=False)
    assert exc.value.code == ErrorCode.EMPTY_VALUE
    with pytest.raises(MetadataValueError) as exc:
        values.parse_str(None)
    assert exc.value.code == ErrorCode.NULL_VALUE
    with pytest.raises(MetadataValueError):
        values.parse_str(12)
    with pytest.raises(MetadataValueError) as exc:
        values.parse_str("abcd", max_length=3)
    assert exc.value.code == ErrorCode.TOO_LARGE


def test_decode_text_is_lossless():
    assert values.decode_text("José".encode("utf-8")) == "José"
    # Not valid UTF-8: Latin-1 keeps every byte (no U+FFFD), and re-encodes exactly.
    text = values.decode_text(b"Jos\xe9")
    assert text == "José" and "�" not in text
    assert text.encode("latin-1") == b"Jos\xe9"
    assert values.decode_text(b"\x93hi\x94", encodings=("cp1252",)) == "“hi”"


def test_binary_descriptor():
    d = values.binary_descriptor(b"abc", mime="image/png")
    assert d == {"size": 3,
                 "sha256": "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
                 "mime": "image/png"}
    assert values.binary_descriptor(b"")["mime"] is None
    json.dumps(d)


# ──────────────────────── canonicalize(field, value) ─────────────────────────

def _field(field_type="str", **kw):
    return MetadataField("t:key", "Key", kw.pop("value", None), field_type=field_type, **kw)


def test_field_types_vocabulary():
    assert values.FIELD_TYPES == frozenset({
        "str", "text", "int", "float", "bool", "datetime", "localdatetime",
        "rational", "duration", "binary", "enum",
    })


@pytest.mark.parametrize("field_type, raw, expected", [
    ("str", "x", "x"), ("text", "a\nb", "a\nb"), ("int", "42", 42), ("float", "1.5", 1.5),
    ("bool", "False", False), ("rational", "1234/100", "1234/100"), ("rational", 0.5, "5/10"),
    ("datetime", "2026-01-02T03:04:05Z", "2026-01-02T03:04:05+00:00"),
    ("localdatetime", "2026-01-02 03:04:05", "2026-01-02T03:04:05"),
    ("duration", "2.5", 2.5),
])
def test_canonicalize_scalars(field_type, raw, expected):
    result = values.canonicalize(_field(field_type), raw)
    assert result == expected and type(result) is type(expected)
    # Canonical form is a fixed point.
    assert values.canonicalize(_field(field_type), result) == result


def test_canonicalize_errors_carry_the_field_key():
    with pytest.raises(MetadataValueError) as exc:
        values.canonicalize(_field("int"), "abc")
    assert exc.value.key == "t:key"
    assert exc.value.code == ErrorCode.INVALID_VALUE
    assert isinstance(exc.value, ValueError)


def test_canonicalize_null_is_rejected_for_every_type():
    for field_type in sorted(values.FIELD_TYPES - {"binary"}):
        with pytest.raises(MetadataValueError) as exc:
            values.canonicalize(_field(field_type, choices=["a"]), None)
        assert exc.value.code == ErrorCode.NULL_VALUE


def test_canonicalize_binary_is_read_only():
    with pytest.raises(MetadataValueError) as exc:
        values.canonicalize(_field("binary"), {"size": 1, "sha256": "00", "mime": None})
    assert exc.value.code == ErrorCode.READ_ONLY


def test_canonicalize_empty_string_policy():
    assert values.canonicalize(_field("str"), "") == ""
    with pytest.raises(MetadataValueError) as exc:
        values.canonicalize(_field("str"), "", allow_empty=False)
    assert exc.value.code == ErrorCode.EMPTY_VALUE


def test_canonicalize_enum():
    field = _field("enum", choices=[1, 3, 6, 8])
    assert values.canonicalize(field, 6) == 6
    assert values.canonicalize(field, "6") == 6
    with pytest.raises(MetadataValueError) as exc:
        values.canonicalize(field, 2)
    assert exc.value.code == ErrorCode.INVALID_CHOICE
    assert values.canonicalize(_field("enum", choices=["N", "S"]), "S") == "S"
    with pytest.raises(MetadataValueError):
        values.canonicalize(_field("enum", choices=["N", "S"]), "s")


def test_canonicalize_multi_values_are_lists():
    field = _field("str", multi=True)
    assert values.canonicalize(field, ["Rock", "Pop"]) == ["Rock", "Pop"]
    assert values.canonicalize(field, ("Rock",)) == ["Rock"]
    with pytest.raises(MetadataValueError) as exc:
        values.canonicalize(field, "Rock; Pop")  # never split or join strings
    assert exc.value.code == ErrorCode.WRONG_TYPE
    with pytest.raises(MetadataValueError) as exc:
        values.canonicalize(field, [])
    assert exc.value.code == ErrorCode.EMPTY_VALUE
    with pytest.raises(MetadataValueError) as exc:
        values.canonicalize(field, ["ok", None])
    assert exc.value.code == ErrorCode.NULL_VALUE


def test_canonicalize_fixed_length_tuples():
    field = _field("int", multi=True, length=2)
    assert values.canonicalize(field, ["3", 10]) == [3, 10]
    with pytest.raises(MetadataValueError) as exc:
        values.canonicalize(field, [3])
    assert exc.value.code == ErrorCode.WRONG_LENGTH
    rationals = _field("rational", multi=True, length=3)
    assert values.canonicalize(rationals, ["45/1", "30/1", "1234/100"]) == ["45/1", "30/1", "1234/100"]


def test_canonicalize_unknown_type_is_rejected():
    with pytest.raises(MetadataValueError) as exc:
        values.canonicalize(_field("bytes"), "x")
    assert exc.value.code == ErrorCode.UNSUPPORTED
