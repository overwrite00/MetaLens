"""Filesystem layer (phase F2H): timestamps, field-level error isolation, fs:mtime
writes, Windows attributes, extended attributes, and the generic contract run on
``FilesystemHandler`` for every sample of ``tests/factories/fs.py``.
"""
from __future__ import annotations

import errno
import importlib
import json
import os
import stat
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from core.base_handler import ABSENT
from core.errors import ErrorCode, MetadataValueError, MetadataValueErrors
from core.models import MetadataField
from core.path_security import secure_atomic_write
from tests.factories import fs as fs_samples
from tests.test_contract import (
    check_addable, check_bulk_delete, check_delete_each, check_idempotent,
    check_mutations, check_record,
)

fs_mod = importlib.import_module("core.handlers.filesystem_handler")
FilesystemHandler = fs_mod.FilesystemHandler

WINDOWS = sys.platform == "win32"
NS_1900 = -2_208_988_800 * 10**9


@pytest.fixture
def handler():
    return FilesystemHandler()


@pytest.fixture
def plain(tmp_path) -> Path:
    path = tmp_path / "plain.txt"
    path.write_bytes(b"x")
    os.utime(path, ns=(1_600_000_000 * 10**9, 1_600_000_000 * 10**9))
    return path


def _fields(record) -> dict[str, MetadataField]:
    return {f.key: f for f in record.fields}


def _mtime_field(value) -> MetadataField:
    return MetadataField("fs:mtime", "Modified", value, field_type="datetime", source="filesystem")


# ──────────────────────── reading ────────────────────────────────────────────

def test_timestamps_come_from_ns_with_microsecond_precision(handler, tmp_path):
    path = tmp_path / "f.txt"
    path.write_bytes(b"x")
    os.utime(path, ns=(1_700_000_100 * 10**9, 1_700_000_000_123_456_789))
    fields = _fields(handler.read(path))
    assert fields["fs:mtime"].value == "2023-11-14T22:13:20.123456+00:00"
    assert fields["fs:atime"].value == "2023-11-14T22:15:00+00:00"
    assert fields["fs:mtime"].field_type == "datetime"


def test_pre1970_timestamps_are_read_on_every_platform(handler, tmp_path):
    """datetime.fromtimestamp(<0) fails on Windows: every fs: field used to be lost."""
    path = tmp_path / "old.txt"
    path.write_bytes(b"old")
    os.utime(path, ns=(NS_1900, NS_1900))
    record = handler.read(path)
    fields = _fields(record)
    assert record.read_errors == [] and record.warnings == []
    assert fields["fs:mtime"].value == "1900-01-01T00:00:00+00:00"
    assert fields["fs:atime"].value == "1900-01-01T00:00:00+00:00"
    assert fields["fs:size"].value == 3
    assert {"fs:size", "fs:mtime", "fs:ctime", "fs:atime"} <= set(fields)
    assert fields["fs:mtime"].editable and not fields["fs:mtime"].deletable
    json.dumps(record.to_dict(), allow_nan=False)


def test_one_failing_field_does_not_drop_the_others(handler, plain, monkeypatch):
    atime_ns = os.stat(plain).st_atime_ns
    real = fs_mod.utc_iso_from_ns

    def flaky(ns):
        if ns == atime_ns:
            raise OverflowError("simulated conversion failure")
        return real(ns)
    monkeypatch.setattr(fs_mod, "utc_iso_from_ns", flaky)
    os.utime(plain, ns=(atime_ns, atime_ns + 10**9))  # mtime differs from atime
    record = handler.read(plain)
    fields = _fields(record)
    assert "fs:atime" not in fields
    assert {"fs:size", "fs:mtime", "fs:ctime"} <= set(fields)
    assert record.read_errors == []
    assert any("fs:atime" in w and "simulated conversion failure" in w for w in record.warnings)


def test_missing_file_is_reported_not_raised(handler, tmp_path):
    record = handler.read(tmp_path / "missing.txt")
    assert record.fields == []
    assert record.read_errors and record.warnings
    assert not record.supports_write and not record.format_supports_write


def test_read_only_fields_are_not_modifiable(handler, plain):
    fields = _fields(handler.read(plain))
    for key in ("fs:size", "fs:ctime", "fs:atime"):
        assert not fields[key].editable and not fields[key].deletable, key
    assert fields["fs:size"].field_type == "int"


def test_read_only_file_has_nothing_modifiable_and_says_why(handler, tmp_path):
    """fs:mtime was editable on a READONLY file although every write answered 403."""
    path = fs_samples.fs_readonly(tmp_path)
    if os.access(path, os.W_OK):
        pytest.skip("running with privileges that ignore read-only permissions")
    try:
        record = handler.read(path)
        mtime = _fields(record)["fs:mtime"]
        assert mtime.editable is False and mtime.deletable is False
        assert not any(f.editable or f.deletable for f in record.fields)
        assert record.supports_write is False and record.format_supports_write is False
        reason = record.read_only_reason
        assert reason and ("read-only" in reason.lower() or "permission" in reason.lower()), reason
        assert record.warnings == [] and record.read_errors == []
        assert fs_mod.write_block_reason(path) == reason
    finally:
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
    record = handler.read(path)
    assert _fields(record)["fs:mtime"].editable is True and record.read_only_reason is None
    assert fs_mod.write_block_reason(path) is None


@pytest.mark.skipif(WINDOWS, reason="POSIX folder permissions (the transaction replaces the file)")
def test_file_in_a_read_only_folder_is_not_modifiable(handler, tmp_path):
    folder = tmp_path / "locked"
    folder.mkdir()
    path = folder / "f.txt"
    path.write_text("x")
    os.chmod(folder, stat.S_IRUSR | stat.S_IXUSR)
    try:
        if os.access(folder, os.W_OK):
            pytest.skip("running with privileges that ignore folder permissions")
        record = handler.read(path)
        assert _fields(record)["fs:mtime"].editable is False
        assert "folder" in record.read_only_reason
    finally:
        os.chmod(folder, stat.S_IRWXU)


@pytest.mark.skipif(not WINDOWS, reason="Windows file attributes")
def test_windows_attributes_are_a_list_of_flags(handler, tmp_path):
    path = fs_samples.fs_readonly(tmp_path)
    fs_samples.set_hidden(path)
    f = _fields(handler.read(path))["fs:win_attrs"]
    assert f.multi is True and f.field_type == "str"
    assert {"READONLY", "HIDDEN"} <= set(f.value)
    assert not f.editable and not f.deletable
    assert f.value == [name for name in f.value if name]  # no empty entries
    os.chmod(path, stat.S_IWRITE | stat.S_IREAD)


@pytest.mark.skipif(not WINDOWS, reason="Windows file attributes")
def test_windows_attributes_keep_unknown_bits(handler, plain, monkeypatch):
    real_stat = os.stat
    flags = stat.FILE_ATTRIBUTE_ARCHIVE | 0x8000_0000

    class FakeStat:
        def __init__(self, st):
            self._st = st

        def __getattr__(self, name):
            return flags if name == "st_file_attributes" else getattr(self._st, name)

    monkeypatch.setattr(fs_mod.os, "stat", lambda p, *a, **k: FakeStat(real_stat(p, *a, **k)))
    f = _fields(handler.read(plain))["fs:win_attrs"]
    assert f.value == ["ARCHIVE", "0x80000000"]


# ──────────────────────── writing fs:mtime ───────────────────────────────────

def test_write_mtime_is_exact(handler, plain):
    handler.write(plain, [_mtime_field("2001-02-03T04:05:06.789012+02:00")])
    assert os.stat(plain).st_mtime_ns == 981_165_906_789_012_000
    assert _fields(handler.read(plain))["fs:mtime"].value == "2001-02-03T02:05:06.789012+00:00"


def test_canonical_mtime_is_utc(handler):
    assert handler.canonical(_mtime_field(None), "2001-02-03T04:05:06+02:00") == \
        "2001-02-03T02:05:06+00:00"


def test_write_mtime_before_1970(handler, plain):
    handler.write(plain, [_mtime_field("1900-01-01T00:00:00+00:00")])
    assert os.stat(plain).st_mtime_ns == NS_1900
    assert _fields(handler.read(plain))["fs:mtime"].value == "1900-01-01T00:00:00+00:00"


def test_mtime_without_offset_is_rejected(handler, plain):
    before = os.stat(plain).st_mtime_ns
    with pytest.raises(MetadataValueError) as info:
        handler.write(plain, [_mtime_field("2020-01-01T00:00:00")])
    assert info.value.key == "fs:mtime" and info.value.code == ErrorCode.MISSING_OFFSET
    assert os.stat(plain).st_mtime_ns == before


@pytest.mark.parametrize("value, code", [
    ("garbage", ErrorCode.INVALID_VALUE),
    (None, ErrorCode.NULL_VALUE),
    (1577836800, ErrorCode.WRONG_TYPE),
    ("2020-01-01", ErrorCode.INVALID_VALUE),
])
def test_invalid_mtime_values_are_rejected(handler, plain, value, code):
    before = os.stat(plain).st_mtime_ns
    with pytest.raises(MetadataValueError) as info:
        handler.write(plain, [_mtime_field(value)])
    assert (info.value.key, info.value.code) == ("fs:mtime", code)
    assert os.stat(plain).st_mtime_ns == before


@pytest.mark.skipif(not WINDOWS, reason="FILETIME range")
@pytest.mark.parametrize("value", ["1601-01-01T00:00:00+00:00", "1600-12-31T23:59:59+00:00"])
def test_mtime_outside_the_windows_range_is_rejected(handler, plain, value):
    """FILETIME 0 (1601-01-01) means "leave unchanged" to SetFileTime: never send it."""
    before = os.stat(plain).st_mtime_ns
    with pytest.raises(MetadataValueError) as info:
        handler.write(plain, [_mtime_field(value)])
    assert (info.value.key, info.value.code) == ("fs:mtime", ErrorCode.OUT_OF_RANGE)
    assert os.stat(plain).st_mtime_ns == before


def test_mtime_write_is_verified(handler, plain, monkeypatch):
    """A filesystem that ignores or rounds the time must not report success (G4)."""
    before = os.stat(plain).st_mtime_ns
    monkeypatch.setattr(fs_mod.os, "utime", lambda *a, **k: None)
    with pytest.raises(MetadataValueError) as info:
        handler.write(plain, [_mtime_field("2010-01-01T00:00:00+00:00")])
    assert (info.value.key, info.value.code) == ("fs:mtime", ErrorCode.LOSSY_WRITE)
    assert os.stat(plain).st_mtime_ns == before


def test_writing_the_current_value_keeps_sub_microsecond_precision(handler, tmp_path):
    path = tmp_path / "ns.txt"
    path.write_bytes(b"x")
    os.utime(path, ns=(10**18, 1_700_000_000_123_456_789))
    stored = os.stat(path).st_mtime_ns  # NTFS keeps 100 ns, ext4 1 ns
    current = _fields(handler.read(path))["fs:mtime"].value
    handler.apply_write(path, [_mtime_field(current)])
    assert os.stat(path).st_mtime_ns == stored


def test_read_only_and_unknown_keys_are_rejected(handler, plain):
    fields = [MetadataField("fs:size", "Size", 5, field_type="int"),
              MetadataField("fs:nope", "Nope", "x"),
              _mtime_field("2020-01-01T00:00:00")]
    before = os.stat(plain).st_mtime_ns
    with pytest.raises(MetadataValueErrors) as info:
        handler.apply_write(plain, fields)
    assert {(e.key, e.code) for e in info.value.errors} == {
        ("fs:size", ErrorCode.READ_ONLY), ("fs:nope", ErrorCode.UNKNOWN_FIELD),
        ("fs:mtime", ErrorCode.MISSING_OFFSET)}
    assert os.stat(plain).st_mtime_ns == before
    with pytest.raises(MetadataValueError) as one:
        handler.apply_delete(plain, ["fs:mtime"])
    assert one.value.code == ErrorCode.NOT_DELETABLE


def test_snapshot_and_restore_mtime_exactly(handler, tmp_path):
    path = tmp_path / "s.txt"
    path.write_bytes(b"x")
    os.utime(path, ns=(10**18, 1_700_000_000_123_456_789))
    snap = handler.snapshot(path, ["fs:mtime"])
    assert snap == {"fs:mtime": os.stat(path).st_mtime_ns}
    handler.apply_write(path, [_mtime_field("1999-01-01T00:00:00+00:00")])
    handler.restore(path, snap)
    assert handler.snapshot(path, ["fs:mtime"]) == snap


# ──────────────────────── API ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_api_rejects_mtime_without_offset(client, plain):
    before = os.stat(plain).st_mtime_ns
    resp = await client.post("/write", json={
        "path": str(plain), "fields": [{"key": "fs:mtime", "value": "2020-01-01T00:00:00"}]})
    assert resp.status_code == 422, resp.text
    assert "fs:mtime" in resp.json()["detail"]
    assert os.stat(plain).st_mtime_ns == before
    assert sorted(p.name for p in plain.parent.iterdir()) == ["plain.txt"]


@pytest.mark.asyncio
async def test_api_writes_pre1970_mtime(client, tmp_path):
    path = fs_samples.fs_pre1970(tmp_path)
    resp = await client.get("/read", params={"path": str(path)})
    assert resp.status_code == 200
    served = {f["key"]: f for f in resp.json()["fields"]}
    assert served["fs:mtime"]["value"] == "1900-01-01T00:00:00+00:00"
    assert {"fs:size", "fs:ctime", "fs:atime"} <= set(served)
    resp = await client.post("/write", json={
        "path": str(path), "fields": [{"key": "fs:mtime", "value": "1899-12-31T23:00:00-01:00"}]})
    assert resp.status_code == 200, resp.text
    assert os.stat(path).st_mtime_ns == NS_1900
    resp = await client.post("/write", json={
        "path": str(path), "fields": [{"key": "fs:mtime", "value": "1910-05-06T07:08:09+00:00"}]})
    assert resp.status_code == 200, resp.text
    assert os.stat(path).st_mtime_ns == -1_882_630_311 * 10**9  # 1910-05-06T07:08:09Z


@pytest.mark.asyncio
async def test_api_read_only_target_is_refused(client, tmp_path):
    path = fs_samples.fs_readonly(tmp_path)
    if os.access(path, os.W_OK):
        pytest.skip("running with privileges that ignore read-only permissions")
    before = (path.read_bytes(), os.stat(path).st_mtime_ns)
    try:
        resp = await client.post("/write", json={
            "path": str(path), "fields": [{"key": "fs:mtime", "value": "2010-01-01T00:00:00+00:00"}]})
        # 403 once the API maps TargetNotWritableError (plan B4/F3A); 409 before.
        assert resp.status_code in (403, 409), resp.text
        assert (path.read_bytes(), os.stat(path).st_mtime_ns) == before
        assert sorted(p.name for p in path.parent.iterdir()) == ["readonly.txt"]
    finally:
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD)


# ──────────────────────── extended attributes ────────────────────────────────

class FakeXattr:
    """In-memory xattr backend (Linux semantics: namespaced names)."""
    namespaced = True

    def __init__(self, attrs: dict[str, bytes] | None = None):
        self.attrs = dict(attrs or {})

    def list(self, path):
        return list(self.attrs)

    def get(self, path, name):
        if name not in self.attrs:
            raise OSError(errno.ENODATA, "No data available")
        return self.attrs[name]

    def set(self, path, name, value):
        if not name.startswith("user."):
            raise OSError(errno.EPERM, "Operation not permitted")
        self.attrs[name] = bytes(value)

    def remove(self, path, name):
        if name not in self.attrs:
            raise OSError(errno.ENODATA, "No data available")
        del self.attrs[name]


@pytest.fixture
def fake_xattr(monkeypatch):
    backend = FakeXattr({
        "user.comment": "MetaLens è".encode(),
        "user.blob": b"\xff\xfe\x00\x80",
        "security.selinux": b"system_u:object_r:user_home_t:s0\x00",
        "trusted.md5": b"abc",
    })
    monkeypatch.setattr(fs_mod, "XATTR_BACKEND", backend)
    return backend


def test_xattr_text_is_editable_and_deletable(handler, plain, fake_xattr):
    record = handler.read(plain)
    f = _fields(record)["xattr:user.comment"]
    assert (f.value, f.field_type, f.editable, f.deletable, f.source) == \
        ("MetaLens è", "str", True, True, "xattr")
    assert record.format_supports_delete is True
    handler.apply_write(plain, [replace(f, value="日本 €")])
    assert fake_xattr.attrs["user.comment"] == "日本 €".encode()


def test_xattr_non_utf8_is_binary_read_only(handler, plain, fake_xattr):
    f = _fields(handler.read(plain))["xattr:user.blob"]
    assert f.field_type == "binary" and not f.editable and f.deletable
    assert f.value["size"] == 4 and f.binary == f.value
    with pytest.raises(MetadataValueError) as info:
        handler.apply_write(plain, [replace(f, field_type="str", value="text")])
    assert info.value.code == ErrorCode.READ_ONLY
    assert fake_xattr.attrs["user.blob"] == b"\xff\xfe\x00\x80"


@pytest.mark.parametrize("name", ["security.selinux", "trusted.md5"])
def test_xattr_outside_user_namespace_is_read_only(handler, plain, fake_xattr, name):
    f = _fields(handler.read(plain))[f"xattr:{name}"]
    assert not f.editable and not f.deletable
    with pytest.raises(MetadataValueError) as info:
        handler.apply_delete(plain, [f.key])
    assert info.value.code == ErrorCode.NOT_DELETABLE
    with pytest.raises(MetadataValueError) as info:
        handler.apply_write(plain, [replace(f, field_type="str", value="x")])
    assert info.value.code == ErrorCode.READ_ONLY
    assert name in fake_xattr.attrs


def test_xattr_delete_and_restore_binary_exactly(handler, plain, fake_xattr):
    keys = ["xattr:user.blob", "xattr:user.comment"]
    original = dict(fake_xattr.attrs)
    snap = handler.snapshot(plain, keys)
    assert snap == {"xattr:user.blob": b"\xff\xfe\x00\x80", "xattr:user.comment": "MetaLens è".encode()}
    handler.apply_delete(plain, keys)
    assert set(fake_xattr.attrs) == {"security.selinux", "trusted.md5"}
    handler.restore(plain, snap)
    assert fake_xattr.attrs == original


def test_xattr_can_be_added_and_undone(handler, plain, fake_xattr):
    schema = handler.writable_field(plain, "xattr:user.new")
    assert schema is not None and schema.editable and schema.field_type == "str"
    assert handler.writable_field(plain, "xattr:security.new") is None
    assert handler.writable_field(plain, "xattr:user.comment") is None  # already present
    snap = handler.snapshot(plain, ["xattr:user.new"])
    assert snap == {"xattr:user.new": ABSENT}
    handler.apply_write(plain, [replace(schema, value="added")])
    assert fake_xattr.attrs["user.new"] == b"added"
    handler.restore(plain, snap)
    assert "user.new" not in fake_xattr.attrs


def test_xattr_errors_are_isolated(handler, plain, fake_xattr, monkeypatch):
    real_get = fake_xattr.get

    def get(path, name):
        if name == "trusted.md5":
            raise OSError(errno.EACCES, "Permission denied")
        return real_get(path, name)
    monkeypatch.setattr(fake_xattr, "get", get)
    record = handler.read(plain)
    fields = _fields(record)
    assert "xattr:trusted.md5" not in fields
    assert {"fs:mtime", "xattr:user.comment", "xattr:user.blob"} <= set(fields)
    assert any("xattr:trusted.md5" in w for w in record.warnings)
    assert record.read_errors == []


def test_xattr_unsupported_filesystem_is_silent(handler, plain, monkeypatch):
    backend = FakeXattr()
    monkeypatch.setattr(backend, "list", lambda p: (_ for _ in ()).throw(OSError(errno.ENOTSUP, "nope")))
    monkeypatch.setattr(fs_mod, "XATTR_BACKEND", backend)
    record = handler.read(plain)
    assert record.warnings == [] and record.read_errors == []


def test_xattr_without_namespaces_is_read_only(handler, plain, monkeypatch):
    """macOS has no ``user.`` namespace: D18 keeps every attribute read-only there."""
    backend = FakeXattr({"com.apple.quarantine": b"0081;5f"})
    backend.namespaced = False
    monkeypatch.setattr(fs_mod, "XATTR_BACKEND", backend)
    f = _fields(handler.read(plain))["xattr:com.apple.quarantine"]
    assert not f.editable and not f.deletable


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="real user xattrs (Linux)")
def test_real_xattrs_on_linux(handler, tmp_path):
    path = fs_samples.fs_xattr_binary(tmp_path)
    fields = _fields(handler.read(path))
    text_name, text_value = fs_samples.XATTR_TEXT
    blob_name, _ = fs_samples.XATTR_BINARY
    assert fields[f"xattr:{text_name}"].value == text_value.decode()
    assert fields[f"xattr:{blob_name}"].field_type == "binary"
    handler.delete(path, [f"xattr:{blob_name}"])
    assert blob_name not in os.listxattr(path)


# ──────────────────────── contract on the filesystem layer ───────────────────

FS_CASES = [pytest.param(s, id=s.name, marks=() if s.available else pytest.mark.skip(
    reason=f"{s.name} runs only on {', '.join(s.platforms)}")) for s in fs_samples.SAMPLES]


@pytest.mark.parametrize("sample", FS_CASES)
def test_filesystem_layer_contract(sample, tmp_path, handler):
    path = sample.make(tmp_path / "sample")
    record = handler.read(path)
    strict = replace(sample, valid=True)  # the filesystem layer reads every sample cleanly
    check_record(record, strict, handler, path)
    assert record.warnings == []
    assert record.handler_name == "filesystem"
    if "read_only_target" in sample.tags:
        if os.access(path, os.W_OK):
            pytest.skip("running with privileges that ignore read-only permissions")
        with pytest.raises(Exception):
            secure_atomic_write(path, lambda tmp: handler.apply_write(
                tmp, [_mtime_field("2010-01-01T00:00:00+00:00")]))
        return
    assert check_idempotent(handler, path, sample) >= 1
    assert check_mutations(handler, path, sample) >= 1
    check_delete_each(handler, path, sample)
    check_bulk_delete(handler, path, sample)
    check_addable(handler, path, sample)
    others = sorted(p.name for p in path.parent.iterdir() if p != path)
    assert not others, others


@pytest.mark.skipif(not WINDOWS, reason="Windows hidden attribute")
@pytest.mark.parametrize("name", ["fs_hidden_jpg", "fs_hidden_pdf", "fs_hidden_docx"])
def test_hidden_samples_stay_hidden_after_mtime_write(handler, tmp_path, name):
    path = getattr(fs_samples, name)(tmp_path)
    handler.write(path, [_mtime_field("2015-06-07T08:09:10+00:00")])
    assert os.stat(path).st_file_attributes & fs_samples.FILE_ATTRIBUTE_HIDDEN
    assert os.stat(path).st_mtime_ns == 1_433_664_550 * 10**9
    assert "HIDDEN" in _fields(handler.read(path))["fs:win_attrs"].value
