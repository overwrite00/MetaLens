"""Tests for the value-model extensions of MetadataField / MetadataRecord (plan B1/B2)."""
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from core.base_handler import ABSENT, BaseMetadataHandler
from core.models import MetadataField, MetadataRecord


def _record(**kw) -> MetadataRecord:
    return MetadataRecord(file_path=Path("x.bin"), file_size=1, mtime=0.0, handler_name="t", **kw)


# ──────────────────────── MetadataField ──────────────────────────────────────

def test_field_new_attributes_have_backward_compatible_defaults():
    f = MetadataField("a:b", "B", "v")
    assert (f.multi, f.length, f.binary) == (False, None, None)
    assert (f.editable, f.deletable, f.field_type, f.source, f.choices) == (True, True, "str", "unknown", [])


def test_field_to_dict_exposes_new_attributes_and_is_json():
    f = MetadataField("tag:TRCK", "Track", [3, 10], field_type="int", multi=True, length=2)
    d = f.to_dict()
    assert d["multi"] is True and d["length"] == 2 and d["binary"] is None
    assert d["value"] == [3, 10]
    # Every key of the old wire format is still present.
    for key in ("key", "label", "value", "raw_value", "editable", "deletable",
                "field_type", "source", "choices"):
        assert key in d
    json.dumps(d, allow_nan=False)


def test_field_to_dict_serializes_nested_values():
    dt = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    f = MetadataField("x:y", "Y", [dt, b"\x00\x01"], raw_value=(b"ab", dt))
    d = f.to_dict()
    assert d["value"] == ["2026-01-02T03:04:05+00:00", "<binary 2 bytes>"]
    assert d["raw_value"] == ["<binary 2 bytes>", "2026-01-02T03:04:05+00:00"]
    json.dumps(d, allow_nan=False)


def test_field_to_dict_keeps_legacy_bytes_rendering():
    assert MetadataField("x:y", "Y", b"abc").to_dict()["value"] == "<binary 3 bytes>"


def test_binary_field_constructor():
    f = MetadataField.binary_field("tag:APIC", "Cover", b"\x89PNG", mime="image/png", source="id3")
    assert f.field_type == "binary" and not f.editable and f.deletable
    assert f.value == f.binary == {
        "size": 4,
        "sha256": hashlib.sha256(b"\x89PNG").hexdigest(),
        "mime": "image/png",
    }
    assert f.raw_value == b"\x89PNG"
    json.dumps(f.to_dict(), allow_nan=False)


def test_binary_field_constructor_multi():
    f = MetadataField.binary_field("mp4:covr", "Cover", [b"a", b"bb"], deletable=False)
    assert f.multi and f.length is None and not f.deletable
    assert [d["size"] for d in f.value] == [1, 2]
    assert f.binary == f.value


# ──────────────────────── MetadataRecord ─────────────────────────────────────

def test_record_new_attributes_default_from_legacy_flags():
    r = _record(supports_write=True, supports_delete=False)
    assert r.format_supports_write is True and r.format_supports_delete is False
    assert r.warnings == [] and r.addable_fields == [] and r.read_only_reason is None
    # The route ORs the filesystem layer into supports_* after construction:
    # the format-level capability must not follow it.
    r.supports_delete = True
    assert r.format_supports_delete is False


def test_record_explicit_format_capabilities_win():
    r = _record(supports_write=True, format_supports_write=False, read_only_reason="signed PDF")
    assert r.supports_write is True and r.format_supports_write is False
    assert r.read_only_reason == "signed PDF"


def test_record_to_dict_has_new_fields_and_is_json():
    add = MetadataField("tag:TIT2", "Title", "", source="id3")
    r = _record(supports_write=True, warnings=["MakerNote may be rewritten"],
                addable_fields=[add])
    r.fields = [MetadataField("tag:TPE1", "Artist", ["A", "B"], multi=True)]
    d = r.to_dict()
    for key in ("file_path", "file_size", "mtime", "handler_name", "fields", "read_errors",
                "supports_write", "supports_delete"):
        assert key in d
    assert d["warnings"] == ["MakerNote may be rewritten"]
    assert d["format_supports_write"] is True and d["format_supports_delete"] is False
    assert d["read_only_reason"] is None
    assert d["addable_fields"] == [add.to_dict()]
    json.loads(json.dumps(d, allow_nan=False))


def test_record_dedupe_keys_renames_and_locks_duplicates():
    r = _record(supports_write=True)
    r.fields = [MetadataField("a:x", "X", 1), MetadataField("a:y", "Y", 2),
                MetadataField("a:x", "X", 3), MetadataField("a:x", "X", 4)]
    renamed = r.dedupe_keys()
    assert [f.key for f in r.fields] == ["a:x", "a:y", "a:x#2", "a:x#3"]
    assert renamed == ["a:x#2", "a:x#3"]
    assert r.fields[0].editable and r.fields[0].deletable
    assert all(not f.editable and not f.deletable for f in r.fields[2:])
    assert len(r.warnings) == 1 and "a:x" in r.warnings[0]
    assert r.dedupe_keys() == [] and len(r.warnings) == 1


def test_record_dedupe_keys_avoids_existing_suffix():
    r = _record()
    r.fields = [MetadataField("a:x", "X", 1), MetadataField("a:x#2", "X2", 2),
                MetadataField("a:x", "X", 3)]
    r.dedupe_keys()
    keys = [f.key for f in r.fields]
    assert len(keys) == len(set(keys)) and keys[2] == "a:x#3"


# ──────────────────────── handler hooks ──────────────────────────────────────

class _Minimal(BaseMetadataHandler):
    NAME = "minimal"

    def read(self, path):
        return self._make_record(path)

    def write(self, path, fields):
        pass

    def delete(self, path, keys):
        pass


def test_absent_sentinel():
    import copy
    import pickle
    assert repr(ABSENT) == "ABSENT" and not ABSENT
    assert copy.deepcopy(ABSENT) is ABSENT and copy.copy(ABSENT) is ABSENT
    assert pickle.loads(pickle.dumps(ABSENT)) is ABSENT
    assert {"k": ABSENT} == {"k": copy.deepcopy(ABSENT)}


def test_hook_defaults(tmp_path):
    h = _Minimal()
    p = tmp_path / "f.bin"
    p.write_bytes(b"x")
    record = h.read(p)
    assert h.writable_fields(p, record) == []
    for call in (lambda: h.apply_write(p, []), lambda: h.apply_delete(p, []),
                 lambda: h.snapshot(p, []), lambda: h.restore(p, {})):
        with pytest.raises(NotImplementedError):
            call()
    assert not any(h.overrides(name) for name in BaseMetadataHandler.MUTATION_HOOKS)
    f = MetadataField("a:b", "B", "x", field_type="int")
    assert h.canonical(f, "41") == 41


def test_overrides_detects_implemented_hooks():
    class _WithHooks(_Minimal):
        def apply_write(self, tmp, fields):
            pass

        def snapshot(self, tmp, keys):
            return {}

    h = _WithHooks()
    assert h.overrides("apply_write") and h.overrides("snapshot")
    assert not h.overrides("apply_delete") and not h.overrides("restore")
    with pytest.raises(ValueError):
        h.overrides("read_everything")
