"""API contract of phase F3A: uniform error body, HTTP codes, single transaction.

Uses in-test fake handlers (JSON files with a private extension) so the route
behaviour is tested independently of the real format handlers, plus a few
end-to-end checks on real formats built in the test itself.
"""
from __future__ import annotations

import copy
import json
import os
import stat
import struct
import sys
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

import api.routes as routes
from api.routes import ApiCode as ApiCodeNames
from core.base_handler import ABSENT, BaseMetadataHandler
from core.errors import ErrorCode, MetadataValueError
from core.models import MetadataField, MetadataRecord
from core.registry import HandlerRegistry
from main import app


# ──────────────────────── helpers ────────────────────────────────────────────

def assert_error(resp, status: int, code: str | None = None, key: str | None = "<any>") -> dict:
    """Uniform error body: {"ok": false, "errors": [{code, key, message}], "detail": str}."""
    assert resp.status_code == status, resp.text
    body = resp.json()
    assert body["ok"] is False, body
    assert isinstance(body["detail"], str) and body["detail"], body
    assert isinstance(body["errors"], list) and body["errors"], body
    for err in body["errors"]:
        assert set(err) == {"code", "key", "message"}, err
        assert isinstance(err["code"], str) and err["code"]
        assert err["key"] is None or isinstance(err["key"], str)
        assert isinstance(err["message"], str) and err["message"]
    if code is not None:
        assert code in [e["code"] for e in body["errors"]], body
    if key != "<any>":
        assert key in [e["key"] for e in body["errors"]], body
    return body


def assert_ok(resp) -> dict:
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True and body["errors"] == [], body
    return body


def _no_leftovers(path: Path) -> None:
    others = sorted(p.name for p in path.parent.iterdir() if p != path)
    assert not others, f"transaction leftovers: {others}"


_SCHEMA = {
    "fake:title": {"field_type": "str"},
    "fake:count": {"field_type": "int"},
    "fake:tags": {"field_type": "str", "multi": True},
    "fake:extra": {"field_type": "str"},          # addable only
}


class FakeHandler(BaseMetadataHandler):
    """New-contract handler: metadata is the JSON content of a ``.mlfake`` file."""
    NAME = "fake"
    EXTENSIONS = frozenset({".mlfake"})
    PRIORITY = 1

    def __init__(self) -> None:
        self.calls: list[tuple[str, Path]] = []
        self.supports = True
        self.reason: str | None = None
        self.duplicate = False

    @staticmethod
    def load(path: Path) -> dict:
        return json.loads(Path(path).read_text(encoding="utf-8"))

    @staticmethod
    def save(path: Path, data: dict) -> None:
        Path(path).write_text(json.dumps(data, sort_keys=True), encoding="utf-8")

    def _field(self, key: str, value: Any) -> MetadataField:
        spec = _SCHEMA[key]
        return MetadataField(key, key, copy.deepcopy(value), editable=self.supports,
                             deletable=self.supports, field_type=spec["field_type"],
                             multi=spec.get("multi", False), source="fake")

    def read(self, path: Path) -> MetadataRecord:
        record = self._make_record(path, supports_write=self.supports,
                                   supports_delete=self.supports)
        record.read_only_reason = self.reason
        data = self.load(path)
        record.fields = [self._field(k, v) for k, v in data.items()]
        record.fields.append(MetadataField("fake:size", "Size", path.stat().st_size,
                                           editable=False, deletable=False, field_type="int", computed=True))
        if self.duplicate:
            record.fields.append(self._field("fake:title", "duplicate"))
        return record

    def writable_fields(self, path: Path, record: MetadataRecord) -> list[MetadataField]:
        present = {f.key for f in record.fields}
        return [MetadataField(k, k, None, field_type=s["field_type"], multi=s.get("multi", False))
                for k, s in _SCHEMA.items() if k not in present]

    def apply_write(self, tmp: Path, fields: list[MetadataField]) -> None:
        self.calls.append(("apply_write", tmp))
        data = self.load(tmp)
        for f in fields:
            data[f.key] = self.canonical(f, f.value)
        self.save(tmp, data)

    def apply_delete(self, tmp: Path, keys: list[str]) -> None:
        self.calls.append(("apply_delete", tmp))
        data = self.load(tmp)
        for key in keys:
            data.pop(key, None)
        self.save(tmp, data)

    def snapshot(self, tmp: Path, keys: list[str]) -> dict:
        data = self.load(tmp)
        return {k: copy.deepcopy(data[k]) if k in data else ABSENT for k in keys}

    def restore(self, tmp: Path, snap: dict) -> None:
        data = self.load(tmp)
        for k, raw in snap.items():
            if raw is ABSENT:
                data.pop(k, None)
            else:
                data[k] = raw
        self.save(tmp, data)

    def write(self, path, fields):  # the route must not use the nested wrapper
        raise AssertionError("legacy write wrapper used by the route")

    def delete(self, path, keys):
        raise AssertionError("legacy delete wrapper used by the route")


class LegacyHandler(BaseMetadataHandler):
    """Handler not migrated yet: only read/write/delete (wrappers with own transaction)."""
    NAME = "legacy"
    EXTENSIONS = frozenset({".mllegacy"})
    PRIORITY = 1

    def read(self, path: Path) -> MetadataRecord:
        record = self._make_record(path, supports_write=True, supports_delete=True)
        data = FakeHandler.load(path)
        record.fields = [MetadataField(k, k, v, source="legacy") for k, v in data.items()]
        return record

    def write(self, path, fields):
        def mutate(tmp):
            data = FakeHandler.load(tmp)
            data.update({f.key: f.value for f in fields})
            FakeHandler.save(tmp, data)
        self._atomic_write(path, mutate)

    def delete(self, path, keys):
        def mutate(tmp):
            data = FakeHandler.load(tmp)
            for k in keys:
                data.pop(k, None)
            FakeHandler.save(tmp, data)
        self._atomic_write(path, mutate)


@pytest.fixture
def fake(monkeypatch):
    handler = FakeHandler()
    legacy = LegacyHandler()
    handlers = sorted([*HandlerRegistry._handlers, handler, legacy], key=lambda h: h.PRIORITY)
    monkeypatch.setattr(HandlerRegistry, "_handlers", handlers)
    return handler


def _fake_file(tmp_path: Path, data: dict | None = None, ext: str = ".mlfake") -> Path:
    path = tmp_path / ("sample" + ext)
    FakeHandler.save(path, {"fake:title": "Old", "fake:count": 1, "fake:tags": ["a", "b"]}
                     if data is None else data)
    return path


# ──────────────────────── error body and codes ───────────────────────────────

@pytest.mark.asyncio
async def test_pydantic_errors_use_uniform_body_with_string_detail(client, tmp_path):
    target = tmp_path / "file.txt"
    target.write_text("ok")
    resp = await client.post("/write", json={"path": str(target), "fields": [{"key": "fs:mtime"}]})
    body = assert_error(resp, 422, "invalid_request")
    assert any("value" in (e["key"] or "") for e in body["errors"]), body
    resp = await client.post("/write", json={"path": str(target)})
    assert_error(resp, 422, "invalid_request")
    resp = await client.post("/delete", json={"path": str(target), "keys": []})
    assert_error(resp, 422, "invalid_request")
    resp = await client.get("/read")
    assert_error(resp, 422, "invalid_request", key="query.path")


@pytest.mark.asyncio
async def test_invalid_path_is_400(client, tmp_path):
    missing = tmp_path / "missing.txt"
    for resp in (await client.get("/read", params={"path": str(missing)}),
                 await client.get("/hash", params={"path": str(missing)}),
                 await client.get("/list", params={"path": str(missing)}),
                 await client.get("/diff", params={"a": str(missing), "b": str(missing)}),
                 await client.post("/write", json={"path": str(missing),
                                                   "fields": [{"key": "fs:mtime", "value": "x"}]}),
                 await client.post("/delete", json={"path": str(missing), "keys": ["fs:mtime"]})):
        assert_error(resp, 400, "invalid_path", key=None)


@pytest.mark.asyncio
async def test_hash_without_algorithms_is_400(client, tmp_path):
    target = tmp_path / "file.bin"
    target.write_bytes(b"abc")
    for algorithms in ("", " , ,"):
        resp = await client.get("/hash", params={"path": str(target), "algorithms": algorithms})
        assert_error(resp, 400, "invalid_parameter", key="algorithms")
    resp = await client.get("/hash", params={"path": str(target), "algorithms": "sha256,nope"})
    assert_error(resp, 400, "invalid_parameter", key="algorithms")
    resp = await client.get("/hash", params={"path": str(target), "algorithms": "SHA256"})
    assert resp.status_code == 200
    assert resp.json()["hashes"] == {
        "sha256": "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"}


@pytest.mark.asyncio
async def test_auth_errors_use_uniform_body():
    # assegnazione diretta: la fixture autouse di conftest ripristina il token
    app.state.sidecar_token = "secret"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        assert_error(await c.get("/health"), 401, "unauthorized", key=None)
        assert_error(await c.get("/health", headers={"Origin": "https://evil.example",
                                                     "Authorization": "Bearer secret"}),
                     403, "origin_not_allowed", key=None)
    app.state.sidecar_token = None
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        assert_error(await c.get("/health"), 503, "auth_not_configured", key=None)


@pytest.mark.asyncio
async def test_unknown_route_uses_uniform_body(client):
    assert_error(await client.get("/does-not-exist"), 404, "not_found", key=None)


@pytest.mark.asyncio
async def test_read_only_file_is_403_and_untouched(client, tmp_path):
    if sys.platform != "win32" and hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("root ignores POSIX write permissions")
    target = tmp_path / "file.txt"
    target.write_text("original")
    os.chmod(target, stat.S_IREAD)  # Windows: READONLY attribute; POSIX: 0o400
    try:
        resp = await client.post("/write", json={"path": str(target), "fields": [
            {"key": "fs:mtime", "value": "2020-01-01T00:00:00+00:00"}]})
        assert_error(resp, 403, ErrorCode.NOT_WRITABLE, key=None)
        assert target.read_text() == "original"
        _no_leftovers(target)
    finally:
        os.chmod(target, stat.S_IREAD | stat.S_IWRITE)


@pytest.mark.asyncio
async def test_replace_permission_error_is_403(client, tmp_path, monkeypatch):
    import core.path_security as security
    target = tmp_path / "file.txt"
    target.write_text("original")

    def denied(*args):
        raise PermissionError("access denied")
    monkeypatch.setattr(security, "_replace_file", denied)
    resp = await client.post("/write", json={"path": str(target), "fields": [
        {"key": "fs:mtime", "value": "2020-01-01T00:00:00+00:00"}]})
    assert_error(resp, 403, ErrorCode.NOT_WRITABLE)
    assert target.read_text() == "original"
    _no_leftovers(target)


@pytest.mark.asyncio
async def test_concurrent_modification_is_409(client, tmp_path, fake, monkeypatch):
    target = _fake_file(tmp_path)
    original_apply = FakeHandler.apply_write

    def racing(self, tmp, fields):
        original_apply(self, tmp, fields)
        target.write_text(json.dumps({"fake:title": "external"}), encoding="utf-8")
    monkeypatch.setattr(FakeHandler, "apply_write", racing)
    resp = await client.post("/write", json={"path": str(target), "fields": [
        {"key": "fake:title", "value": "New"}]})
    assert_error(resp, 409, ErrorCode.CONCURRENT_MODIFICATION, key=None)
    assert FakeHandler.load(target) == {"fake:title": "external"}
    _no_leftovers(target)


@pytest.mark.asyncio
async def test_unexpected_handler_error_is_500_with_uniform_body(client, tmp_path, fake, monkeypatch):
    target = _fake_file(tmp_path)
    before = target.read_bytes()

    def boom(self, tmp, fields):
        raise RuntimeError("bug in handler")
    monkeypatch.setattr(FakeHandler, "apply_write", boom)
    resp = await client.post("/write", json={"path": str(target), "fields": [
        {"key": "fake:title", "value": "New"}]})
    assert_error(resp, 500, "internal_error", key=None)
    assert target.read_bytes() == before
    _no_leftovers(target)


@pytest.mark.asyncio
@pytest.mark.parametrize("exc", [struct.error("bad pack"), UnicodeEncodeError("latin-1", "€", 0, 1, "no"),
                                 OverflowError("too big"), ValueError("bad value")])
async def test_library_value_errors_are_422_never_500(client, tmp_path, fake, monkeypatch, exc):
    target = _fake_file(tmp_path)
    before = target.read_bytes()

    def failing(self, tmp, fields):
        raise exc
    monkeypatch.setattr(FakeHandler, "apply_write", failing)
    resp = await client.post("/write", json={"path": str(target), "fields": [
        {"key": "fake:title", "value": "€"}]})
    assert_error(resp, 422, ErrorCode.INVALID_VALUE)
    assert target.read_bytes() == before


@pytest.mark.asyncio
@pytest.mark.parametrize("hook", ["apply_write", "canonical"])
async def test_internal_type_error_is_logged_500_not_422(client, tmp_path, fake, monkeypatch, caplog, hook):
    """TypeError used to be mapped to 422 as if the user had sent a bad value. A value
    of the wrong type is rejected by ``canonical`` (wrong_type, test below); a
    TypeError raised later is a bug: 500 internal_error, logged, file intact."""
    target = _fake_file(tmp_path)
    before = target.read_bytes()

    def buggy(self, *args):
        raise TypeError("unsupported operand type(s) for +: 'int' and 'str'")
    monkeypatch.setattr(FakeHandler, hook, buggy)
    with caplog.at_level("ERROR", logger="api.routes"):
        resp = await client.post("/write", json={"path": str(target), "fields": [
            {"key": "fake:title", "value": "New"}]})
    assert_error(resp, 500, ApiCodeNames.INTERNAL_ERROR, key=None)
    assert any(r.exc_info and r.exc_info[0] is TypeError for r in caplog.records), caplog.records
    assert target.read_bytes() == before
    _no_leftovers(target)


@pytest.mark.asyncio
async def test_wrong_value_type_from_the_user_is_422_wrong_type(client, tmp_path, fake):
    target = _fake_file(tmp_path)
    before = target.read_bytes()
    resp = await client.post("/write", json={"path": str(target), "fields": [
        {"key": "fake:tags", "value": {"not": "a list"}}, {"key": "fake:count", "value": [1]}]})
    body = assert_error(resp, 422)
    assert {e["key"]: e["code"] for e in body["errors"]} == {
        "fake:tags": ErrorCode.WRONG_TYPE, "fake:count": ErrorCode.INVALID_VALUE}
    assert target.read_bytes() == before


# ──────────────────────── validation before mutation ─────────────────────────

@pytest.mark.asyncio
async def test_all_validation_errors_collected_before_touching_file(client, tmp_path, fake):
    target = _fake_file(tmp_path)
    before = target.read_bytes()
    resp = await client.post("/write", json={"path": str(target), "fields": [
        {"key": "fake:count", "value": "not a number"},
        {"key": "fake:title", "value": None},
        {"key": "fake:tags", "value": "not a list"},
        {"key": "fake:size", "value": 3},
        {"key": "nope:unknown", "value": "x"},
        {"key": "fs:mtime", "value": "2020-01-01T00:00:00"},
    ]})
    body = assert_error(resp, 422)
    by_key = {e["key"]: e["code"] for e in body["errors"]}
    assert by_key == {
        "fake:count": ErrorCode.INVALID_VALUE,
        "fake:title": ErrorCode.NULL_VALUE,
        "fake:tags": ErrorCode.WRONG_TYPE,
        "fake:size": ErrorCode.READ_ONLY,
        "nope:unknown": ErrorCode.UNKNOWN_FIELD,
        "fs:mtime": ErrorCode.MISSING_OFFSET,
    }, body
    assert all(e["message"] in body["detail"] for e in body["errors"])
    assert fake.calls == []
    assert target.read_bytes() == before
    _no_leftovers(target)


@pytest.mark.asyncio
async def test_delete_validation_errors_collected(client, tmp_path, fake):
    target = _fake_file(tmp_path)
    before = target.read_bytes()
    resp = await client.post("/delete", json={"path": str(target), "keys": [
        "fake:size", "nope:unknown", "fs:size", "fake:title"]})
    body = assert_error(resp, 422)
    by_key = {e["key"]: e["code"] for e in body["errors"]}
    assert by_key == {"fake:size": ErrorCode.NOT_DELETABLE, "nope:unknown": ErrorCode.UNKNOWN_FIELD,
                      "fs:size": ErrorCode.NOT_DELETABLE}, body
    assert fake.calls == []
    assert target.read_bytes() == before


@pytest.mark.asyncio
async def test_duplicate_request_keys_are_422(client, tmp_path, fake):
    target = _fake_file(tmp_path)
    resp = await client.post("/write", json={"path": str(target), "fields": [
        {"key": "fake:title", "value": "a"}, {"key": "fake:title", "value": "b"}]})
    assert_error(resp, 422, "duplicate_key", key="fake:title")
    resp = await client.post("/delete", json={"path": str(target), "keys": ["fake:title", "fake:title"]})
    assert_error(resp, 422, "duplicate_key", key="fake:title")
    resp = await client.post("/write", json={"path": str(target), "fields": [
        {"key": "fake:title", "value": "a"}], "delete": ["fake:title"]})
    assert_error(resp, 422, "duplicate_key", key="fake:title")
    assert fake.calls == []


@pytest.mark.asyncio
async def test_client_permission_flags_are_ignored(client, tmp_path, fake):
    fake.supports = False
    target = _fake_file(tmp_path)
    before = target.read_bytes()
    resp = await client.post("/write", json={"path": str(target), "fields": [
        {"key": "fake:title", "value": "x", "editable": True, "field_type": "str"}]})
    assert_error(resp, 422, ErrorCode.READ_ONLY, key="fake:title")
    resp = await client.post("/delete", json={"path": str(target), "keys": ["fake:title"],
                                              "deletable": True})
    assert_error(resp, 422, ErrorCode.NOT_DELETABLE, key="fake:title")
    assert target.read_bytes() == before


@pytest.mark.asyncio
async def test_canonical_values_are_written(client, tmp_path, fake):
    target = _fake_file(tmp_path)
    resp = await client.post("/write", json={"path": str(target), "fields": [
        {"key": "fake:count", "value": "+7"}, {"key": "fake:tags", "value": ["x"]}]})
    assert_ok(resp)
    assert FakeHandler.load(target) == {"fake:title": "Old", "fake:count": 7, "fake:tags": ["x"]}
    _no_leftovers(target)


@pytest.mark.asyncio
async def test_read_only_field_changed_by_a_save_is_a_lossy_write(client, tmp_path, fake, monkeypatch):
    """B5: only fields flagged ``computed`` may change with a save; any other read-only field
    that moves (here the size shown by the fake handler, no longer computed) is a lossy write."""
    real_read = FakeHandler.read

    def read(self, path):
        record = real_read(self, path)
        for f in record.fields:
            f.computed = False
        return record

    monkeypatch.setattr(FakeHandler, "read", read)
    target = _fake_file(tmp_path)
    before = target.read_bytes()
    resp = await client.post("/write", json={"path": str(target), "fields": [
        {"key": "fake:title", "value": "A much longer title than before"}]})
    assert_error(resp, 422, ErrorCode.LOSSY_WRITE, key="fake:size")
    assert target.read_bytes() == before


# ──────────────────────── single transaction (N22, D2) ───────────────────────

@pytest.mark.asyncio
async def test_route_uses_hooks_in_one_transaction(client, tmp_path, fake, monkeypatch):
    target = _fake_file(tmp_path)
    count = []
    real = routes.secure_atomic_write

    def counting(*args, **kwargs):
        count.append(args[0])
        return real(*args, **kwargs)
    monkeypatch.setattr(routes, "secure_atomic_write", counting)
    import core.base_handler as base
    monkeypatch.setattr(base, "secure_atomic_write",
                        lambda *a, **k: pytest.fail("nested transaction opened"))
    resp = await client.post("/write", json={"path": str(target), "fields": [
        {"key": "fake:title", "value": "New"}, {"key": "fs:mtime", "value": "2020-01-01T00:00:00+00:00"}],
        "delete": ["fake:tags"]})
    assert_ok(resp)
    assert len(count) == 1
    assert [c[0] for c in fake.calls] == ["apply_delete", "apply_write"]
    assert all(c[1] != target and c[1].parent.name.startswith(".metalens-") for c in fake.calls)
    assert FakeHandler.load(target) == {"fake:title": "New", "fake:count": 1}
    assert target.stat().st_mtime == pytest.approx(1577836800, abs=1)
    _no_leftovers(target)


@pytest.mark.asyncio
async def test_combined_save_failure_changes_nothing(client, tmp_path, fake):
    target = _fake_file(tmp_path)
    before = target.read_bytes()
    resp = await client.post("/write", json={"path": str(target), "fields": [
        {"key": "fake:title", "value": "New"}], "delete": ["fake:size"]})
    assert_error(resp, 422, ErrorCode.NOT_DELETABLE, key="fake:size")
    assert target.read_bytes() == before
    assert fake.calls == []


@pytest.mark.asyncio
async def test_legacy_handler_without_hooks_is_read_only(client, tmp_path, fake):
    # F3B removed the transitional legacy branch: a handler without the mutation
    # hooks (apply_* + snapshot/restore) cannot offer an exact Undo, so its format
    # layer is read-only (honest flags on /read, 422 on /write and /delete).
    target = _fake_file(tmp_path, {"legacy:a": "1", "legacy:b": "2"}, ext=".mllegacy")
    before = target.read_bytes()
    body = (await client.get("/read", params={"path": str(target)})).json()
    assert body["format_supports_write"] is False and body["format_supports_delete"] is False
    assert body["read_only_reason"]
    fields = {f["key"]: f for f in body["fields"]}
    assert not fields["legacy:a"]["editable"] and not fields["legacy:b"]["deletable"]
    assert fields["fs:mtime"]["editable"] is True
    resp = await client.post("/write", json={"path": str(target), "fields": [
        {"key": "legacy:a", "value": "changed"}], "delete": ["legacy:b"]})
    body = assert_error(resp, 422)
    assert {(e["key"], e["code"]) for e in body["errors"]} == {
        ("legacy:a", ErrorCode.READ_ONLY), ("legacy:b", ErrorCode.NOT_DELETABLE)}
    assert target.read_bytes() == before
    _no_leftovers(target)


# ──────────────────────── read-after-write verification (G4) ─────────────────

@pytest.mark.asyncio
async def test_lossy_write_is_422_and_file_intact(client, tmp_path, fake, monkeypatch):
    target = _fake_file(tmp_path)
    before = target.read_bytes()

    def lossy(self, tmp, fields):  # silently stores a different value
        data = self.load(tmp)
        for f in fields:
            data[f.key] = "truncated"
        self.save(tmp, data)
    monkeypatch.setattr(FakeHandler, "apply_write", lossy)
    resp = await client.post("/write", json={"path": str(target), "fields": [
        {"key": "fake:title", "value": "MetaLens è 日本 €"}]})
    assert_error(resp, 422, ErrorCode.LOSSY_WRITE, key="fake:title")
    assert target.read_bytes() == before
    _no_leftovers(target)


@pytest.mark.asyncio
async def test_write_altering_other_keys_is_422(client, tmp_path, fake, monkeypatch):
    target = _fake_file(tmp_path)
    before = target.read_bytes()

    def clobber(self, tmp, fields):
        data = {f.key: self.canonical(f, f.value) for f in fields}  # drops the other keys
        self.save(tmp, data)
    monkeypatch.setattr(FakeHandler, "apply_write", clobber)
    resp = await client.post("/write", json={"path": str(target), "fields": [
        {"key": "fake:title", "value": "New"}]})
    body = assert_error(resp, 422, ErrorCode.LOSSY_WRITE)
    assert {"fake:count", "fake:tags"} <= {e["key"] for e in body["errors"]}
    assert target.read_bytes() == before


@pytest.mark.asyncio
async def test_delete_that_does_not_remove_is_422(client, tmp_path, fake, monkeypatch):
    target = _fake_file(tmp_path)
    before = target.read_bytes()
    monkeypatch.setattr(FakeHandler, "apply_delete", lambda self, tmp, keys: None)
    resp = await client.post("/delete", json={"path": str(target), "keys": ["fake:title"]})
    assert_error(resp, 422, ErrorCode.LOSSY_WRITE, key="fake:title")
    assert target.read_bytes() == before


# ──────────────────────── shared record (/read, /diff) ───────────────────────

@pytest.mark.asyncio
async def test_read_exposes_format_level_flags(client, tmp_path, fake):
    fake.supports = False
    fake.reason = "Signed document"
    target = _fake_file(tmp_path)
    body = (await client.get("/read", params={"path": str(target)})).json()
    assert body["format_supports_write"] is False
    assert body["format_supports_delete"] is False
    assert body["read_only_reason"] == "Signed document"
    assert body["supports_write"] is True  # fs:mtime keeps the legacy OR flag true
    assert body["addable_fields"] == []
    fields = {f["key"]: f for f in body["fields"]}
    assert fields["fs:mtime"]["editable"] is True
    assert not any(f["editable"] for k, f in fields.items() if k.startswith("fake:"))


@pytest.mark.asyncio
async def test_read_unknown_format_has_no_format_layer(client, tmp_path):
    target = tmp_path / "file.mlnothing"
    target.write_bytes(os.urandom(64))
    body = (await client.get("/read", params={"path": str(target)})).json()
    assert body["format_supports_write"] is False
    assert body["format_supports_delete"] is False
    assert body["supports_write"] is True
    assert {f["key"]: f["editable"] for f in body["fields"]}["fs:mtime"] is True


@pytest.mark.asyncio
async def test_read_lists_addable_fields_and_they_can_be_written(client, tmp_path, fake):
    target = _fake_file(tmp_path)
    body = (await client.get("/read", params={"path": str(target)})).json()
    assert [f["key"] for f in body["addable_fields"]] == ["fake:extra"]
    assert body["format_supports_write"] is True
    assert_ok(await client.post("/write", json={"path": str(target), "fields": [
        {"key": "fake:extra", "value": "added"}]}))
    assert FakeHandler.load(target)["fake:extra"] == "added"


@pytest.mark.asyncio
async def test_read_errors_block_only_the_format_layer(client, tmp_path, fake, monkeypatch):
    target = _fake_file(tmp_path)
    original_read = FakeHandler.read

    def broken(self, path):
        record = original_read(self, path)
        record.read_errors.append("partial read")
        return record
    monkeypatch.setattr(FakeHandler, "read", broken)
    body = (await client.get("/read", params={"path": str(target)})).json()
    assert body["format_supports_write"] is False and body["format_supports_delete"] is False
    assert body["read_only_reason"]
    assert body["read_errors"] == ["partial read"]
    resp = await client.post("/write", json={"path": str(target), "fields": [
        {"key": "fake:title", "value": "x"}]})
    assert_error(resp, 422, ErrorCode.READ_ONLY, key="fake:title")
    assert_ok(await client.post("/write", json={"path": str(target), "fields": [
        {"key": "fs:mtime", "value": "2020-01-01T00:00:00+00:00"}]}))


@pytest.mark.asyncio
async def test_duplicate_handler_keys_are_made_unique(client, tmp_path, fake):
    fake.duplicate = True
    target = _fake_file(tmp_path)
    body = (await client.get("/read", params={"path": str(target)})).json()
    keys = [f["key"] for f in body["fields"]]
    assert len(keys) == len(set(keys))
    dup = next(f for f in body["fields"] if f["key"] == "fake:title#2")
    assert dup["editable"] is False and dup["deletable"] is False
    assert any("fake:title" in w for w in body["warnings"])
    resp = await client.post("/write", json={"path": str(target), "fields": [
        {"key": "fake:title#2", "value": "x"}]})
    assert_error(resp, 422, ErrorCode.READ_ONLY, key="fake:title#2")


@pytest.mark.asyncio
async def test_diff_includes_filesystem_layer(client, tmp_path):
    a = tmp_path / "a.txt"
    b = tmp_path / "b.txt"
    a.write_text("one")
    b.write_text("three")
    body = (await client.get("/diff", params={"a": str(a), "b": str(b)})).json()
    changed = {c["field_a"]["key"] for c in body["changed"]}
    assert "fs:size" in changed


@pytest.mark.asyncio
async def test_diff_uses_same_record_as_read(client, tmp_path, fake):
    a = _fake_file(tmp_path)
    b = tmp_path / "other.mlfake"
    FakeHandler.save(b, {"fake:title": "Other", "fake:count": 1})
    body = (await client.get("/diff", params={"a": str(a), "b": str(b)})).json()
    assert [c["field_a"]["key"] for c in body["changed"] if c["field_a"]["key"].startswith("fake:t")] \
        == ["fake:title"]
    assert [f["key"] for f in body["only_in_a"]] == ["fake:tags"]
    assert "fs:mtime" in {f["key"] for f in body["identical"]} | {c["field_a"]["key"] for c in body["changed"]}


@pytest.mark.asyncio
async def test_success_body_shape(client, tmp_path):
    target = tmp_path / "file.txt"
    target.write_text("ok")
    body = assert_ok(await client.post("/write", json={"path": str(target), "fields": [
        {"key": "fs:mtime", "value": "2020-01-01T00:00:00Z"}]}))
    assert set(body) == {"ok", "errors", "undo_token", "undo_unavailable", "warnings"}
    assert isinstance(body["undo_token"], str) and body["undo_unavailable"] is None and body["warnings"] == []
    assert target.stat().st_mtime == pytest.approx(1577836800, abs=1)
    _no_leftovers(target)


# ──────────────────────── /diff: volatile filesystem times (9) ───────────────

@pytest.mark.asyncio
async def test_diff_of_a_file_with_itself_or_a_copy_reports_no_change(client, tmp_path):
    """fs:atime moves when the file is read (between the two reads of /diff) and
    fs:ctime differs for every copy: they used to show up as "changed"."""
    import shutil
    a = tmp_path / "a.txt"
    a.write_text("same content")
    old = 1_500_000_000 * 10**9          # old atime: NTFS/relatime update it on read
    os.utime(a, ns=(old, old))
    b = tmp_path / "b.txt"
    shutil.copy2(a, b)                   # same content, size and mtime; new ctime
    for other in (a, b):
        body = (await client.get("/diff", params={"a": str(a), "b": str(other)})).json()
        assert body["changed"] == [], body["changed"]
        assert body["only_in_a"] == [] and body["only_in_b"] == []
        keys = {f["key"] for f in body["identical"]}
        assert {"fs:mtime", "fs:size"} <= keys
        assert not keys & routes.DIFF_IGNORED_KEYS
    assert routes.DIFF_IGNORED_KEYS == {"fs:atime", "fs:ctime"}
    os.utime(b, ns=(old, old + 10**9))   # mtime is still compared
    body = (await client.get("/diff", params={"a": str(a), "b": str(b)})).json()
    assert [c["field_a"]["key"] for c in body["changed"]] == ["fs:mtime"]


# ──────────────────────── /list never shows a save in progress ───────────────

@pytest.mark.asyncio
async def test_list_hides_the_private_folder_of_a_running_save(client, tmp_path, fake, monkeypatch):
    target = _fake_file(tmp_path)
    original_apply = FakeHandler.apply_write
    seen = []

    def listing_during_save(self, tmp, fields):
        assert tmp.parent.name.startswith(".metalens-") and tmp.parent.exists()
        seen.append([item["name"] for item in routes.list_directory(str(tmp_path))["items"]])
        original_apply(self, tmp, fields)
    monkeypatch.setattr(FakeHandler, "apply_write", listing_during_save)
    assert_ok(await client.post("/write", json={"path": str(target), "fields": [
        {"key": "fake:title", "value": "New"}]}))
    assert seen == [["sample.mlfake"]]


@pytest.mark.asyncio
async def test_list_of_a_folder_removed_meanwhile_is_404(client, tmp_path, monkeypatch):
    folder = tmp_path / "gone"
    folder.mkdir()

    def vanished(self):
        raise FileNotFoundError(2, "No such file or directory", str(self))
    monkeypatch.setattr(Path, "iterdir", vanished)
    assert_error(await client.get("/list", params={"path": str(folder)}), 404, "file_not_found", key=None)


# ──────────────────────── reads never 500 on transient errors (10a) ──────────

@pytest.mark.asyncio
async def test_read_of_a_file_vanishing_during_the_read_is_404(client, tmp_path, fake, monkeypatch):
    """A FileNotFoundError while reading (file deleted/replaced meanwhile) was a 500."""
    monkeypatch.setattr(routes, "READ_RETRY_SECONDS", 0.2)
    target = _fake_file(tmp_path)

    def vanishing(self, path):
        Path(path).unlink(missing_ok=True)
        raise FileNotFoundError(2, "No such file or directory", str(path))
    monkeypatch.setattr(FakeHandler, "read", vanishing)
    assert_error(await client.get("/read", params={"path": str(target)}), 404, "file_not_found", key=None)


@pytest.mark.asyncio
async def test_transient_read_errors_are_retried(client, tmp_path, fake, monkeypatch):
    target = _fake_file(tmp_path)
    original_read = FakeHandler.read
    failures = [FileNotFoundError(2, "replaced meanwhile"), PermissionError(13, "delete pending")]

    def flaky(self, path):
        if failures:
            raise failures.pop()
        return original_read(self, path)
    monkeypatch.setattr(FakeHandler, "read", flaky)
    body = (await client.get("/read", params={"path": str(target)}))
    assert body.status_code == 200, body.text
    assert failures == [] and {f["key"] for f in body.json()["fields"]} >= {"fake:title"}


@pytest.mark.asyncio
async def test_read_of_a_file_that_keeps_changing_is_503(client, tmp_path, fake, monkeypatch):
    monkeypatch.setattr(routes, "READ_RETRY_SECONDS", 0.2)
    target = _fake_file(tmp_path)
    original_read = FakeHandler.read

    def changing(self, path):
        record = original_read(self, path)
        st = os.stat(path)
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))   # another writer
        return record
    monkeypatch.setattr(FakeHandler, "read", changing)
    resp = await client.get("/read", params={"path": str(target)})
    assert_error(resp, 503, "file_busy", key=None)
    assert resp.headers.get("retry-after") == "1"


def _exclusive(path: Path):
    from tests.test_atomic_windows import _ExclusiveHandle
    return _ExclusiveHandle(path)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows sharing modes")
@pytest.mark.asyncio
async def test_reads_of_a_locked_file_are_503_never_500(client, tmp_path, monkeypatch):
    monkeypatch.setattr(routes, "READ_RETRY_SECONDS", 0.2)
    target = tmp_path / "file.txt"
    target.write_text("content")
    lock = _exclusive(target)
    try:
        for resp in (await client.get("/read", params={"path": str(target)}),
                     await client.get("/hash", params={"path": str(target), "algorithms": "sha256"}),
                     await client.get("/diff", params={"a": str(target), "b": str(target)})):
            assert_error(resp, 503, "file_busy", key=None)
    finally:
        lock.close()
    assert (await client.get("/read", params={"path": str(target)})).status_code == 200


@pytest.mark.skipif(sys.platform != "win32", reason="Windows sharing modes")
@pytest.mark.asyncio
async def test_read_waits_for_a_short_lock(client, tmp_path):
    import threading
    target = tmp_path / "file.txt"
    target.write_text("content")
    lock = _exclusive(target)
    threading.Timer(0.3, lock.close).start()
    try:
        resp = await client.get("/read", params={"path": str(target)})
    finally:
        lock.close()
    assert resp.status_code == 200, resp.text
    assert resp.json()["read_errors"] == []


# ──────────────────────── reads and writes of one file are serialized (10c) ──

@pytest.mark.asyncio
async def test_reads_during_writes_never_fail(client, tmp_path):
    """Overlapping /read and /write of the same file: reads used to see the file
    missing or locked during ReplaceFileW (400/500/read_errors) and most writes
    failed with 403 (WinError 32) because of the readers' open handles."""
    import asyncio
    from tests.factories import by_name
    directory = tmp_path / "jpeg"
    directory.mkdir()
    path = by_name("jpeg_camera").build(directory)
    done = asyncio.Event()
    failures: list[str] = []
    reads: list[int] = []

    async def reader():
        while not done.is_set():
            resp = await client.get("/read", params={"path": str(path)})
            reads.append(resp.status_code)
            if resp.status_code != 200 or resp.json()["read_errors"]:
                failures.append(resp.text[:300])

    async def writer():
        statuses = []
        try:
            for i in range(12):
                resp = await client.post("/write", json={"path": str(path), "fields": [
                    {"key": "exif:Artist", "value": f"artist {i}"}]})
                statuses.append(resp.status_code)
                if resp.status_code != 200:
                    failures.append(resp.text[:300])
        finally:
            done.set()
        return statuses

    statuses, *_ = await asyncio.gather(writer(), reader(), reader(), reader())
    assert statuses == [200] * 12, failures
    assert not failures, failures
    assert reads and set(reads) == {200}
    fields = {f["key"]: f for f in (await client.get("/read", params={"path": str(path)})).json()["fields"]}
    assert fields["exif:Artist"]["value"] == "artist 11"
    assert len(routes.PATH_LOCKS) == 0          # no lock left behind
    others = sorted(p.name for p in directory.iterdir() if p != path)
    assert not others, others


@pytest.mark.asyncio
async def test_write_during_hash_of_the_same_file_waits(client, tmp_path, monkeypatch):
    """A /write while /hash had the file open used to fail with 403 WinError 32."""
    import asyncio
    import hashlib
    import threading
    import time
    target = tmp_path / "big.bin"
    data = os.urandom(3 * 1024 * 1024)
    target.write_bytes(data)
    real_new = hashlib.new
    hashing_started = threading.Event()

    class SlowHasher:          # keeps /hash reading (file open) while the write arrives
        def __init__(self, name):
            self._hash = real_new(name)

        def update(self, chunk):
            if not hashing_started.is_set():
                hashing_started.set()
                time.sleep(0.5)
            self._hash.update(chunk)

        def hexdigest(self):
            return self._hash.hexdigest()
    monkeypatch.setattr(routes.hashlib, "new", SlowHasher)
    hashing = asyncio.create_task(client.get("/hash", params={"path": str(target), "algorithms": "sha256"}))
    while not hashing_started.is_set():
        await asyncio.sleep(0.01)
    write = await client.post("/write", json={"path": str(target), "fields": [
        {"key": "fs:mtime", "value": "2021-01-01T00:00:00+00:00"}]})
    hashed = await hashing
    assert hashed.status_code == 200, hashed.text
    assert write.status_code == 200, write.text
    assert hashed.json()["size"] == len(data)
    assert hashed.json()["hashes"]["sha256"] == real_new("sha256", data).hexdigest()
    _no_leftovers(target)


def test_file_locks_are_readers_writer_and_fair():
    """Readers share a file; a mutation waits for them and blocks new readers."""
    import threading
    import time
    locks = routes.FileLocks()
    path = Path("C:/x/file.txt") if sys.platform == "win32" else Path("/x/file.txt")
    events: list[str] = []
    with locks.reading(path):
        with locks.reading(path):            # another reader enters at once
            events.append("two readers")

        def write():
            with locks.writing(path):
                events.append("writer")
        writer = threading.Thread(target=write)
        writer.start()
        time.sleep(0.1)
        assert events == ["two readers"]     # the writer waits for the reader

        def late_reader():
            with locks.reading(path):
                events.append("late reader")
        reader = threading.Thread(target=late_reader)
        reader.start()
        time.sleep(0.1)
        assert events == ["two readers"]     # a waiting writer blocks new readers
    writer.join(2)
    reader.join(2)
    assert events == ["two readers", "writer", "late reader"]
    assert len(locks) == 0
    with locks.reading(path, path):          # the same file twice: taken once
        pass


# ──────────────────────── read-only files: nothing editable (14c) ────────────

def _make_read_only(path: Path) -> None:
    os.chmod(path, stat.S_IREAD)             # Windows: READONLY attribute; POSIX: 0o400


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["unknown_format", "writable_format"])
async def test_read_only_file_is_served_read_only_with_reason(client, tmp_path, fake, kind):
    """fs:mtime (and the format fields) were served editable on a READONLY file
    while every write answered 403."""
    if sys.platform != "win32" and hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("root ignores POSIX write permissions")
    if kind == "unknown_format":
        target = tmp_path / "file.txt"
        target.write_text("original")
    else:
        target = _fake_file(tmp_path)
    before = target.read_bytes()
    _make_read_only(target)
    try:
        body = (await client.get("/read", params={"path": str(target)})).json()
        fields = {f["key"]: f for f in body["fields"]}
        assert fields["fs:mtime"]["editable"] is False, fields["fs:mtime"]
        assert not any(f["editable"] or f["deletable"] for f in fields.values())
        assert body["format_supports_write"] is False and body["format_supports_delete"] is False
        assert body["supports_write"] is False and body["supports_delete"] is False
        assert body["addable_fields"] == []
        reason = body["read_only_reason"]
        assert reason and ("read-only" in reason.lower() or "permission" in reason.lower()), reason
        resp = await client.post("/write", json={"path": str(target), "fields": [
            {"key": "fs:mtime", "value": "2020-01-01T00:00:00+00:00"}]})
        assert_error(resp, 403, ErrorCode.NOT_WRITABLE, key=None)
        resp = await client.post("/delete", json={"path": str(target), "keys": ["fake:title"]})
        assert_error(resp, 403, ErrorCode.NOT_WRITABLE, key=None)
        assert target.read_bytes() == before
        _no_leftovers(target)
    finally:
        os.chmod(target, stat.S_IREAD | stat.S_IWRITE)
    body = (await client.get("/read", params={"path": str(target)})).json()
    assert {f["key"]: f for f in body["fields"]}["fs:mtime"]["editable"] is True
    if kind == "writable_format":
        assert body["read_only_reason"] is None and body["format_supports_write"] is True
