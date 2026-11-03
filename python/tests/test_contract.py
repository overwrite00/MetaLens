"""Generic metadata contract (plan B1/B2, section E), run on every registered sample.

Cases = the in-test ``contract_demo`` sample (a reference handler that honours the
whole contract, so the harness is always exercised) + every ``tests.factories.ALL``
sample. Format phases only register samples; they never edit this file to add one.

Per sample:
  * read invariants: unique, JSON-native keys/values; field_type/multi/length/binary
    coherent; binaries never editable; canonical values are fixed points of
    ``handler.canonical``; capability flags coherent; no read_errors for valid samples;
  * G1 idempotence: apply_write(current values) leaves every field identical;
  * mutation: generic new value per type -> canonical form read back, nothing else
    changes; restore(snapshot) gives back the exact raw snapshot (G3);
  * delete each deletable field, then bulk delete, each followed by restore (G3);
  * addable fields: write then restore(ABSENT) removes them again;
  * API: /read mirrors the handler, /write of read-only and /delete of
    non-deletable fields -> 422 with a string ``detail`` and the file untouched,
    /write of the current values -> 200 and identical re-read.

The self-tests at the bottom run the same checkers on deliberately broken handlers
and require each one to be caught.
"""
from __future__ import annotations

import copy
import fnmatch
import importlib
import json
import math
import re
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from core import values
from core.base_handler import ABSENT, BaseMetadataHandler
from core.errors import ErrorCode, MetadataValueError
from core.models import MetadataField, MetadataRecord
from core.path_security import secure_atomic_write
from core.registry import HandlerRegistry
from tests import factories
from tests.factories import Sample, registrar

importlib.import_module("core.handlers")

GENERIC_TEXT = "MetaLens è 日本 €"
_RATIONAL = re.compile(r"^-?[0-9]+/[0-9]+$")
_DESCRIPTOR_KEYS = {"size", "sha256", "mime"}


# ──────────────────────── reference handler (contract_demo) ──────────────────

_DEMO_SCHEMA: dict[str, dict[str, Any]] = {
    "demo:title": {"label": "Title", "field_type": "str"},
    "demo:notes": {"label": "Notes", "field_type": "text"},
    "demo:artists": {"label": "Artists", "field_type": "str", "multi": True},
    "demo:track": {"label": "Track", "field_type": "int", "multi": True, "length": 2},
    "demo:count": {"label": "Count", "field_type": "int"},
    "demo:ratio": {"label": "Ratio", "field_type": "rational"},
    "demo:exposure": {"label": "Exposure", "field_type": "float"},
    "demo:flag": {"label": "Flag", "field_type": "bool"},
    "demo:when": {"label": "When", "field_type": "datetime"},
    "demo:taken": {"label": "Taken", "field_type": "localdatetime"},
    "demo:length": {"label": "Length", "field_type": "duration"},
    "demo:mode": {"label": "Mode", "field_type": "enum", "choices": ["auto", "manual"]},
    "demo:subject": {"label": "Subject", "field_type": "str"},  # addable: absent in the sample
    "demo:cover": {"label": "Cover", "field_type": "binary"},
    "demo:version": {"label": "Version", "field_type": "int", "computed": True},
}

_DEMO_CONTENT = {
    "demo:title": "Hello", "demo:notes": "a\nb", "demo:artists": ["A", "B"],
    "demo:track": [3, 10], "demo:count": 7, "demo:ratio": "1234/100", "demo:exposure": 0.25,
    "demo:flag": False, "demo:when": "2026-01-02T03:04:05+02:00",
    "demo:taken": "2026-01-02T03:04:05", "demo:length": 12.5, "demo:mode": "auto",
    "demo:cover": "89504e470d0a1a0a", "demo:version": 1,
}


class DemoHandler(BaseMetadataHandler):
    """Stores metadata as JSON; honours every hook of the mutation contract."""
    NAME = "contract_demo"
    EXTENSIONS = frozenset({".mlcontract"})
    PRIORITY = 1

    @staticmethod
    def _load(path: Path) -> dict:
        return json.loads(Path(path).read_text(encoding="utf-8"))

    @staticmethod
    def _save(path: Path, data: dict) -> None:
        Path(path).write_text(json.dumps(data, sort_keys=True, ensure_ascii=False), encoding="utf-8")

    def _field(self, key: str, raw: Any) -> MetadataField:
        spec = _DEMO_SCHEMA[key]
        if spec["field_type"] == "binary":
            return MetadataField.binary_field(key, spec["label"], bytes.fromhex(raw),
                                              mime="image/png", source="demo")
        computed = spec.get("computed", False)
        return MetadataField(key, spec["label"], copy.deepcopy(raw), raw_value=copy.deepcopy(raw),
                             editable=not computed, deletable=not computed,
                             field_type=spec["field_type"], source="demo",
                             choices=list(spec.get("choices", [])),
                             multi=spec.get("multi", False), length=spec.get("length"))

    def read(self, path: Path) -> MetadataRecord:
        record = self._make_record(path, supports_write=True, supports_delete=True)
        try:
            data = self._load(path)
            record.fields = [self._field(k, v) for k, v in data.items()]
            record.fields.append(MetadataField("demo:size", "Size", path.stat().st_size,
                                               editable=False, deletable=False,
                                               field_type="int", source="demo"))
        except Exception as exc:  # read never raises
            record.read_errors.append(f"demo read error: {exc}")
        return record

    def writable_fields(self, path: Path, record: MetadataRecord) -> list[MetadataField]:
        present = {f.key for f in record.fields}
        return [MetadataField(k, s["label"], None, field_type=s["field_type"], source="demo")
                for k, s in _DEMO_SCHEMA.items()
                if k not in present and s["field_type"] != "binary" and not s.get("computed")]

    def _spec(self, key: str) -> dict:
        if key not in _DEMO_SCHEMA:
            raise MetadataValueError(key, ErrorCode.UNKNOWN_FIELD)
        return _DEMO_SCHEMA[key]

    def apply_write(self, tmp: Path, fields: list[MetadataField]) -> None:
        data = self._load(tmp)
        before = copy.deepcopy(data)
        for f in fields:
            spec = self._spec(f.key)
            if spec["field_type"] == "binary" or spec.get("computed"):
                raise MetadataValueError(f.key, ErrorCode.READ_ONLY)
            data[f.key] = self.canonical(f, f.value)
        if data != before:      # G1: writing the stored values leaves the file alone
            self._save(tmp, data)

    def apply_delete(self, tmp: Path, keys: list[str]) -> None:
        data = self._load(tmp)
        for key in keys:
            if self._spec(key).get("computed"):
                raise MetadataValueError(key, ErrorCode.NOT_DELETABLE)
            data.pop(key, None)
        self._save(tmp, data)

    def snapshot(self, tmp: Path, keys: list[str]) -> dict[str, Any]:
        data = self._load(tmp)
        return {k: copy.deepcopy(data[k]) if k in data else ABSENT for k in keys}

    def restore(self, tmp: Path, snap: dict[str, Any]) -> None:
        data = self._load(tmp)
        for key, raw in snap.items():
            if raw is ABSENT:
                data.pop(key, None)
            else:
                data[key] = copy.deepcopy(raw)
        self._save(tmp, data)

    def write(self, path: Path, fields: list[MetadataField]) -> None:
        self._atomic_write(path, lambda tmp: self.apply_write(tmp, fields))

    def delete(self, path: Path, keys: list[str]) -> None:
        self._atomic_write(path, lambda tmp: self.apply_delete(tmp, keys))


_DEMO_SAMPLES: list[Sample] = []
_demo_sample = registrar(_DEMO_SAMPLES, "test_contract")


@_demo_sample("contract_demo", handler=DemoHandler(), volatile=("demo:size",))
def _build_demo(tmp_path: Path) -> Path:
    path = tmp_path / "demo.mlcontract"
    DemoHandler._save(path, _DEMO_CONTENT)
    return path


CASES = [*_DEMO_SAMPLES, *factories.ALL]


def _params():
    return [pytest.param(s, id=s.name,
                         marks=() if s.available else pytest.mark.skip(
                             reason=f"{s.name} runs only on {', '.join(s.platforms)}"))
            for s in CASES]


# ──────────────────────── helpers ────────────────────────────────────────────

def _json_native(v: Any) -> bool:
    if v is None or isinstance(v, (bool, int, str)):
        return True
    if isinstance(v, float):
        return math.isfinite(v)
    if isinstance(v, list):
        return all(_json_native(x) for x in v)
    if isinstance(v, dict):
        return all(isinstance(k, str) and _json_native(x) for k, x in v.items())
    return False  # tuple, bytes, datetime, Fraction, ...


def _canon(v: Any) -> str:
    """Type-sensitive comparison key: 1, 1.0 and True differ."""
    return json.dumps(v, sort_keys=True, ensure_ascii=False, allow_nan=False)


def _is_volatile(key: str, sample: Sample) -> bool:
    return any(fnmatch.fnmatchcase(key, p) for p in sample.volatile)


def _projection(record: MetadataRecord, sample: Sample, exclude=()) -> dict[str, tuple]:
    return {f.key: (_canon(f.value), f.field_type, f.multi, f.length, f.editable, f.deletable)
            for f in record.fields if f.key not in exclude and not _is_volatile(f.key, sample)
            and not (f.computed and f.key.partition("@")[0] in exclude)}


def _assert_same(before: dict, after: dict, context: str) -> None:
    if before == after:
        return
    lines = [f"{context}: fields changed"]
    for key in sorted(set(before) | set(after)):
        if before.get(key) != after.get(key):
            lines.append(f"  {key}: {before.get(key, '<absent>')} -> {after.get(key, '<absent>')}")
    raise AssertionError("\n".join(lines))


def _txn(path: Path, fn):
    out = []
    secure_atomic_write(path, lambda tmp: out.append(fn(tmp)))
    return out[0]


def _by_key(record: MetadataRecord) -> dict[str, MetadataField]:
    return {f.key: f for f in record.fields}


def _is_descriptor(v: Any) -> bool:
    return (isinstance(v, dict) and set(v) == _DESCRIPTOR_KEYS
            and type(v["size"]) is int and v["size"] >= 0
            and isinstance(v["sha256"], str) and re.fullmatch(r"[0-9a-f]{64}", v["sha256"]) is not None
            and (v["mime"] is None or isinstance(v["mime"], str)))


def _item_problems(f: MetadataField, item: Any) -> list[str]:
    t = f.field_type
    ok = {
        "str": lambda: isinstance(item, str),
        "text": lambda: isinstance(item, str),
        "int": lambda: type(item) is int,
        "float": lambda: type(item) is float and math.isfinite(item),
        "duration": lambda: type(item) in (int, float) and math.isfinite(item) and item >= 0,
        "bool": lambda: type(item) is bool,
        "rational": lambda: isinstance(item, str) and _RATIONAL.match(item) is not None,
        "enum": lambda: bool(f.choices) and item in f.choices,
        "binary": lambda: _is_descriptor(item),
    }
    if t in ("datetime", "localdatetime"):
        parse = values.parse_datetime if t == "datetime" else values.parse_localdatetime
        try:
            good = isinstance(item, str) and values.format_datetime(parse(item)) == item
        except MetadataValueError:
            good = False
    else:
        good = ok[t]()
    return [] if good else [f"item {item!r} is not a canonical {t}"]


def _field_problems(f: MetadataField, handler: BaseMetadataHandler) -> list[str]:
    p: list[str] = []
    if f.field_type not in values.FIELD_TYPES:
        return [f"{f.key}: unknown field_type {f.field_type!r}"]
    if f.value is None:
        p.append("value is null")
    elif not _json_native(f.value):
        p.append(f"value {f.value!r} is not JSON-native")
    if f.length is not None and (not f.multi or type(f.length) is not int or f.length < 1):
        p.append(f"length={f.length!r} requires multi=True and a positive int")
    items = [f.value]
    if f.multi:
        if not isinstance(f.value, list) or not f.value:
            p.append("multi field value must be a non-empty list")
            items = []
        else:
            items = f.value
            if f.length is not None and len(f.value) != f.length:
                p.append(f"list of {len(f.value)} items, length={f.length}")
    if f.field_type == "binary":
        if f.editable:
            p.append("binary field is editable")
        if f.binary is None or f.binary != f.value:
            p.append("binary descriptor must mirror value")
    elif f.binary is not None:
        p.append("binary descriptor on a non-binary field")
    if f.field_type == "enum" and not f.choices:
        p.append("enum without choices")
    if not p:
        for item in items:
            p += _item_problems(f, item)
    if f.editable and not p:
        try:
            canonical = handler.canonical(f, copy.deepcopy(f.value))
        except MetadataValueError as exc:
            p.append(f"current value rejected by canonical(): {exc}")
        else:
            if _canon(canonical) != _canon(f.value):
                p.append(f"value is not canonical: {f.value!r} -> {canonical!r}")
    return [f"{f.key}: {m}" for m in p]


def _default_item(f: MetadataField) -> Any:
    return {
        "enum": lambda: f.choices[0], "bool": lambda: True, "int": lambda: 1,
        "float": lambda: 1.5, "duration": lambda: 1.0, "rational": lambda: "7/3",
        "datetime": lambda: "2026-01-02T03:04:05+01:00",
        "localdatetime": lambda: "2026-01-02T03:04:05",
    }.get(f.field_type, lambda: GENERIC_TEXT)()


def default_value(f: MetadataField) -> Any:
    """Generic value for a field being added."""
    item = _default_item(f)
    return [item] * (f.length or 1) if f.multi else item


def _new_item(f: MetadataField, current: Any, taken=()) -> Any:
    t = f.field_type
    if t == "enum":
        for choice in f.choices:
            if choice != current and choice not in taken:
                return choice
        pytest.fail(f"{f.key}: enum has no alternative choice; register an explicit mutation")
    if t == "bool":
        return not current
    if t == "int":
        return current + 1
    if t == "float":
        return current + 1.5
    if t == "duration":
        return float(current) + 1.0
    if t == "rational":
        return "7/3" if current != "7/3" else "5/2"
    if t in ("datetime", "localdatetime"):
        parse = values.parse_datetime if t == "datetime" else values.parse_localdatetime
        return values.format_datetime(parse(current) + timedelta(days=1, hours=1))
    text = GENERIC_TEXT
    while text == current or text in taken:
        text += " 2"
    return text


def mutated_value(f: MetadataField) -> Any:
    """Generic new value: text, int+1, list+1 item, "7/3", negated bool, shifted date."""
    if not f.multi:
        return _new_item(f, f.value)
    item_field = replace(f, multi=False, length=None)
    if f.length is None:
        return [*f.value, _new_item(item_field, f.value[-1], taken=f.value)]
    return [_new_item(item_field, f.value[0]), *f.value[1:]]


# ──────────────────────── checkers ───────────────────────────────────────────

def check_record(record: MetadataRecord, sample: Sample, handler: BaseMetadataHandler,
                 path: Path) -> None:
    problems: list[str] = []
    keys = [f.key for f in record.fields]
    duplicates = sorted({k for k in keys if keys.count(k) > 1})
    if duplicates:
        problems.append(f"duplicate keys: {duplicates}")
    try:
        json.dumps(record.to_dict(), allow_nan=False)
    except (TypeError, ValueError) as exc:
        problems.append(f"record is not JSON-serializable: {exc}")
    if sample.valid and record.read_errors:
        problems.append(f"read_errors on a valid sample: {record.read_errors}")
    for f in record.fields:
        problems += _field_problems(f, handler)
    editable = any(f.editable for f in record.fields)
    deletable = any(f.deletable for f in record.fields)
    if editable and not record.format_supports_write:
        problems.append("editable fields but format_supports_write is False")
    if deletable and not record.format_supports_delete:
        problems.append("deletable fields but format_supports_delete is False")
    if record.read_only_reason is not None and (editable or deletable or record.format_supports_write):
        problems.append("read_only_reason set on a modifiable record")
    needed = set()
    if editable:
        needed |= {"apply_write", "snapshot", "restore"}
    if deletable:
        needed |= {"apply_delete", "snapshot", "restore"}
    missing = sorted(h for h in needed if not handler.overrides(h))
    if missing:
        problems.append(f"{handler.NAME} exposes modifiable fields without hooks {missing}")
    addable = handler.writable_fields(path, record)
    addable_keys = [f.key for f in addable]
    if len(set(addable_keys)) != len(addable_keys) or set(addable_keys) & set(keys):
        problems.append(f"addable fields must be unique and absent: {addable_keys}")
    for f in addable:
        if not f.editable or f.field_type not in values.FIELD_TYPES - {"binary"}:
            problems.append(f"addable field {f.key} must be editable and non-binary")
    if addable and not all(handler.overrides(h) for h in ("apply_write", "snapshot", "restore")):
        problems.append("addable fields without apply_write/snapshot/restore hooks")
    assert not problems, f"{sample.name}:\n  " + "\n  ".join(problems)


def check_idempotent(handler, path: Path, sample: Sample) -> int:
    """G1: writing back the values just read changes nothing."""
    before = handler.read(path)
    editable = [replace(f, value=copy.deepcopy(f.value)) for f in before.fields if f.editable]
    if not editable:
        return 0
    _txn(path, lambda tmp: handler.apply_write(tmp, editable))
    _assert_same(_projection(before, sample), _projection(handler.read(path), sample),
                 f"{sample.name}: write of the current values")
    return len(editable)


def _restore_and_compare(handler, path, sample, snap, before, context) -> None:
    _txn(path, lambda tmp: handler.restore(tmp, snap))
    after_snap = handler.snapshot(path, list(snap))
    assert after_snap == snap, f"{context}: snapshot after restore differs:\n{after_snap!r}\n!=\n{snap!r}"
    _assert_same(_projection(before, sample), _projection(handler.read(path), sample),
                 f"{context}: after restore")


def check_mutations(handler, path: Path, sample: Sample) -> int:
    before = handler.read(path)
    count = 0
    for f in [f for f in before.fields if f.editable]:
        context = f"{sample.name}: mutate {f.key}"
        new = copy.deepcopy(sample.mutations[f.key]) if f.key in sample.mutations else mutated_value(f)
        expected = handler.canonical(f, copy.deepcopy(new))
        assert _canon(expected) != _canon(f.value), f"{context}: mutation {new!r} does not change the value"

        def mutate(tmp, f=f, new=new):
            snap = handler.snapshot(tmp, [f.key])
            handler.apply_write(tmp, [replace(f, value=new)])
            return snap
        snap = _txn(path, mutate)
        assert set(snap) == {f.key} and snap[f.key] is not ABSENT, f"{context}: bad snapshot {snap!r}"
        mid = handler.read(path)
        got = _by_key(mid).get(f.key)
        assert got is not None, f"{context}: field disappeared"
        assert (_canon(got.value), got.field_type, got.multi, got.length) == \
            (_canon(expected), f.field_type, f.multi, f.length), \
            f"{context}: read back {got.value!r}, expected canonical {expected!r}"
        _assert_same(_projection(before, sample, exclude={f.key}),
                     _projection(mid, sample, exclude={f.key}), context)
        _restore_and_compare(handler, path, sample, snap, before, context)
        count += 1
    return count


def _check_delete(handler, path: Path, sample: Sample, keys: list[str], context: str) -> None:
    before = handler.read(path)

    def delete(tmp):
        snap = handler.snapshot(tmp, keys)
        handler.apply_delete(tmp, keys)
        return snap
    snap = _txn(path, delete)
    assert set(snap) == set(keys) and all(v is not ABSENT for v in snap.values()), \
        f"{context}: bad snapshot {snap!r}"
    mid = handler.read(path)
    still = sorted(set(keys) & set(_by_key(mid)))
    assert not still, f"{context}: still present after delete: {still}"
    _assert_same(_projection(before, sample, exclude=set(keys)),
                 _projection(mid, sample, exclude=set(keys)), context)
    _restore_and_compare(handler, path, sample, snap, before, context)


def check_delete_each(handler, path: Path, sample: Sample) -> int:
    keys = [f.key for f in handler.read(path).fields if f.deletable]
    for key in keys:
        _check_delete(handler, path, sample, [key], f"{sample.name}: delete {key}")
    return len(keys)


def check_bulk_delete(handler, path: Path, sample: Sample) -> int:
    keys = [f.key for f in handler.read(path).fields if f.deletable]
    if keys:
        _check_delete(handler, path, sample, keys, f"{sample.name}: bulk delete")
    return len(keys)


def check_addable(handler, path: Path, sample: Sample) -> int:
    before = handler.read(path)
    addable = handler.writable_fields(path, before)
    for f in addable:
        context = f"{sample.name}: add {f.key}"
        new = copy.deepcopy(sample.mutations[f.key]) if f.key in sample.mutations else default_value(f)
        expected = handler.canonical(f, copy.deepcopy(new))

        def add(tmp, f=f, new=new):
            snap = handler.snapshot(tmp, [f.key])
            handler.apply_write(tmp, [replace(f, value=new)])
            return snap
        snap = _txn(path, add)
        assert snap == {f.key: ABSENT}, f"{context}: snapshot of an absent field must be ABSENT"
        got = _by_key(handler.read(path)).get(f.key)
        assert got is not None and _canon(got.value) == _canon(expected), \
            f"{context}: read back {got and got.value!r}, expected {expected!r}"
        _restore_and_compare(handler, path, sample, snap, before, context)
    return len(addable)


# ──────────────────────── parametrized contract ──────────────────────────────

def _prepare(sample: Sample, tmp_path: Path, monkeypatch) -> tuple[Path, BaseMetadataHandler]:
    path = sample.make(tmp_path / "sample")
    if sample.handler is not None:
        handlers = sorted([*HandlerRegistry._handlers, sample.handler], key=lambda h: h.PRIORITY)
        monkeypatch.setattr(HandlerRegistry, "_handlers", handlers)
    handler = HandlerRegistry.get(path)
    if sample.handler is not None:
        assert handler is sample.handler
    return path, handler


def _assert_no_leftovers(path: Path) -> None:
    others = sorted(p.name for p in path.parent.iterdir() if p != path)
    assert not others, f"transaction leftovers next to the sample: {others}"


@pytest.mark.parametrize("sample", _params())
def test_read_invariants(sample, tmp_path, monkeypatch):
    path, handler = _prepare(sample, tmp_path, monkeypatch)
    check_record(handler.read(path), sample, handler, path)


@pytest.mark.parametrize("sample", _params())
def test_idempotent_write(sample, tmp_path, monkeypatch):
    path, handler = _prepare(sample, tmp_path, monkeypatch)
    check_idempotent(handler, path, sample)
    _assert_no_leftovers(path)


@pytest.mark.parametrize("sample", _params())
def test_mutation_and_restore(sample, tmp_path, monkeypatch):
    path, handler = _prepare(sample, tmp_path, monkeypatch)
    check_mutations(handler, path, sample)
    _assert_no_leftovers(path)


@pytest.mark.parametrize("sample", _params())
def test_delete_each_and_restore(sample, tmp_path, monkeypatch):
    path, handler = _prepare(sample, tmp_path, monkeypatch)
    check_delete_each(handler, path, sample)
    _assert_no_leftovers(path)


@pytest.mark.parametrize("sample", _params())
def test_bulk_delete_and_restore(sample, tmp_path, monkeypatch):
    path, handler = _prepare(sample, tmp_path, monkeypatch)
    check_bulk_delete(handler, path, sample)
    _assert_no_leftovers(path)


@pytest.mark.parametrize("sample", _params())
def test_add_and_restore(sample, tmp_path, monkeypatch):
    path, handler = _prepare(sample, tmp_path, monkeypatch)
    check_addable(handler, path, sample)
    _assert_no_leftovers(path)


@pytest.mark.asyncio
@pytest.mark.parametrize("sample", _params())
async def test_api_read_mirrors_handler(sample, tmp_path, monkeypatch, client):
    path, handler = _prepare(sample, tmp_path, monkeypatch)
    record = handler.read(path)
    resp = await client.get("/read", params={"path": str(path)})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    for key in ("warnings", "format_supports_write", "format_supports_delete",
                "read_only_reason", "addable_fields"):
        assert key in body
    keys = [f["key"] for f in body["fields"]]
    assert len(keys) == len(set(keys))
    served = {f["key"]: f for f in body["fields"]}
    for f in record.fields:
        if _is_volatile(f.key, sample):
            continue
        assert f.key in served, f"{f.key} missing from /read"
        assert _canon(served[f.key]["value"]) == _canon(f.to_dict()["value"])
        for attr in ("field_type", "multi", "length", "binary", "editable", "deletable"):
            assert served[f.key][attr] == f.to_dict()[attr], (f.key, attr)


@pytest.mark.asyncio
@pytest.mark.parametrize("sample", _params())
async def test_api_rejects_read_only_and_non_deletable(sample, tmp_path, monkeypatch, client):
    path, handler = _prepare(sample, tmp_path, monkeypatch)
    record = handler.read(path)
    original = path.read_bytes()
    checked = 0
    for f in record.fields:
        if not f.editable:
            resp = await client.post("/write", json={
                "path": str(path), "fields": [{"key": f.key, "value": f.to_dict()["value"]}]})
            assert resp.status_code == 422, (f.key, resp.status_code, resp.text)
            assert isinstance(resp.json().get("detail"), str), resp.text
            checked += 1
        if not f.deletable:
            resp = await client.post("/delete", json={"path": str(path), "keys": [f.key]})
            assert resp.status_code == 422, (f.key, resp.status_code, resp.text)
            assert isinstance(resp.json().get("detail"), str), resp.text
            checked += 1
    assert path.read_bytes() == original
    _assert_no_leftovers(path)


@pytest.mark.asyncio
@pytest.mark.parametrize("sample", _params())
async def test_api_idempotent_write(sample, tmp_path, monkeypatch, client):
    path, handler = _prepare(sample, tmp_path, monkeypatch)
    before = handler.read(path)
    editable = [f for f in before.fields if f.editable]
    if not editable:
        return
    stat, data = path.stat(), path.read_bytes()
    resp = await client.post("/write", json={
        "path": str(path), "fields": [{"key": f.key, "value": f.value} for f in editable]})
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True
    assert resp.json()["undo_token"] is None, f"{sample.name}: a write of the current values is not a change"
    after = path.stat()
    assert (after.st_ino, after.st_mtime_ns) == (stat.st_ino, stat.st_mtime_ns), f"{sample.name}: file replaced"
    assert path.read_bytes() == data, f"{sample.name}: bytes changed"
    _assert_same(_projection(before, sample), _projection(handler.read(path), sample),
                 f"{sample.name}: /write of the current values")
    _assert_no_leftovers(path)


# ──────────────────────── harness self-tests ─────────────────────────────────

def test_demo_exercises_every_check(tmp_path):
    """The reference sample really drives every checker (none is vacuous)."""
    sample = _DEMO_SAMPLES[0]
    handler = sample.handler
    path = sample.make(tmp_path)
    check_record(handler.read(path), sample, handler, path)
    types = {f.field_type for f in handler.read(path).fields}
    assert types == values.FIELD_TYPES, sorted(values.FIELD_TYPES - types)
    assert check_idempotent(handler, path, sample) == 12
    assert check_mutations(handler, path, sample) == 12
    assert check_delete_each(handler, path, sample) == 13  # 12 editable + cover
    assert check_bulk_delete(handler, path, sample) == 13
    assert check_addable(handler, path, sample) == 1
    assert DemoHandler._load(path) == _DEMO_CONTENT


def test_mutated_values_follow_the_plan():
    f = lambda t, v, **kw: MetadataField("k:k", "K", v, field_type=t, **kw)  # noqa: E731
    assert mutated_value(f("str", "x")) == GENERIC_TEXT
    assert mutated_value(f("int", 4)) == 5
    assert mutated_value(f("bool", False)) is True
    assert mutated_value(f("rational", "1/2")) == "7/3"
    assert mutated_value(f("str", ["a"], multi=True)) == ["a", GENERIC_TEXT]
    assert mutated_value(f("int", [3, 10], multi=True, length=2)) == [4, 10]
    assert mutated_value(f("datetime", "2026-01-01T00:00:00+01:00")) == "2026-01-02T01:00:00+01:00"
    assert mutated_value(f("enum", "N", choices=["N", "S"])) == "S"


class _DuplicateKeys(DemoHandler):
    def read(self, path):
        record = super().read(path)
        record.fields.append(replace(record.fields[0]))
        return record


class _EditableBinary(DemoHandler):
    def read(self, path):
        record = super().read(path)
        for f in record.fields:
            if f.field_type == "binary":
                f.editable = True
        return record


class _JoinedMulti(DemoHandler):
    def read(self, path):
        record = super().read(path)
        for f in record.fields:
            if f.key == "demo:artists":
                f.value = "; ".join(f.value)
        return record


class _TupleValue(DemoHandler):
    def read(self, path):
        record = super().read(path)
        for f in record.fields:
            if f.key == "demo:track":
                f.value = tuple(f.value)
        return record


class _NaiveDatetime(DemoHandler):
    def read(self, path):
        record = super().read(path)
        for f in record.fields:
            if f.key == "demo:when":
                f.value = f.value[:19]
        return record


class _NoHooks(BaseMetadataHandler):
    NAME = "nohooks"

    def read(self, path):
        record = self._make_record(path, supports_write=True)
        record.fields = [MetadataField("x:title", "Title", "t", deletable=False)]
        return record

    def write(self, path, fields):
        pass

    def delete(self, path, keys):
        pass


class _NotIdempotent(DemoHandler):
    def apply_write(self, tmp, fields):
        super().apply_write(tmp, fields)
        data = self._load(tmp)
        data["demo:title"] = data["demo:title"] + "!"
        self._save(tmp, data)


class _LeaksIntoOtherField(DemoHandler):
    def apply_write(self, tmp, fields):
        super().apply_write(tmp, fields)
        if any(f.key == "demo:notes" for f in fields):
            data = self._load(tmp)
            data["demo:count"] += 100
            self._save(tmp, data)


class _LossyRestore(DemoHandler):
    """Restores text case-folded: the value looks plausible but is not the original."""
    def restore(self, tmp, snap):
        super().restore(tmp, {k: v.lower() if isinstance(v, str) else v for k, v in snap.items()})


class _DeleteNoop(DemoHandler):
    def apply_delete(self, tmp, keys):
        pass


class _RestoreIgnoresAbsent(DemoHandler):
    def restore(self, tmp, snap):
        super().restore(tmp, {k: v for k, v in snap.items() if v is not ABSENT})


class _ReducesRationals(DemoHandler):
    def apply_write(self, tmp, fields):
        from fractions import Fraction
        super().apply_write(tmp, fields)
        data = self._load(tmp)
        r = Fraction(data["demo:ratio"])
        data["demo:ratio"] = f"{r.numerator}/{r.denominator}"
        self._save(tmp, data)


_BROKEN = [
    (_DuplicateKeys, check_record, "duplicate keys"),
    (_EditableBinary, check_record, "binary field is editable"),
    (_JoinedMulti, check_record, "multi field value must be a non-empty list"),
    (_TupleValue, check_record, "not JSON-native"),
    (_NaiveDatetime, check_record, "not a canonical datetime"),
    (_NotIdempotent, check_idempotent, "demo:title"),
    (_ReducesRationals, check_idempotent, "demo:ratio"),
    (_LeaksIntoOtherField, check_mutations, "demo:count"),
    (_LossyRestore, check_mutations, "snapshot after restore differs"),
    (_DeleteNoop, check_delete_each, "still present after delete"),
    (_DeleteNoop, check_bulk_delete, "still present after delete"),
    (_RestoreIgnoresAbsent, check_addable, "demo:subject"),
]


@pytest.mark.parametrize("broken, checker, message", _BROKEN,
                         ids=[f"{b.__name__}-{c.__name__}" for b, c, _ in _BROKEN])
def test_checkers_catch_broken_handlers(broken, checker, message, tmp_path):
    sample = replace(_DEMO_SAMPLES[0], handler=broken())
    path = sample.make(tmp_path)
    with pytest.raises(AssertionError, match=re.escape(message)):
        if checker is check_record:
            checker(sample.handler.read(path), sample, sample.handler, path)
        else:
            checker(sample.handler, path, sample)


def test_check_record_requires_hooks_for_modifiable_fields(tmp_path):
    path = tmp_path / "f.bin"
    path.write_bytes(b"x")
    handler = _NoHooks()
    sample = replace(_DEMO_SAMPLES[0], handler=handler)
    with pytest.raises(AssertionError, match="without hooks"):
        check_record(handler.read(path), sample, handler, path)


def test_check_record_flags_invalid_sample_errors(tmp_path):
    sample = replace(_DEMO_SAMPLES[0], valid=True)
    path = tmp_path / "bad.mlcontract"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(AssertionError, match="read_errors on a valid sample"):
        check_record(sample.handler.read(path), sample, sample.handler, path)
    lenient = replace(sample, valid=False)
    check_record(lenient.handler.read(path), lenient, lenient.handler, path)


# ──────────────────────── factory registry ───────────────────────────────────

def test_factory_registry_is_consistent():
    names = [s.name for s in factories.ALL]
    assert len(names) == len(set(names))
    assert [s.name for s in factories.collect()] == names
    for module_name in factories.MODULE_NAMES:
        module = importlib.import_module(f"tests.factories.{module_name}")
        assert isinstance(module.SAMPLES, list)
        assert all(s.module == module_name and s.handler is None for s in module.SAMPLES), \
            "factory samples must use the real registry, not an in-test handler"
    assert {s.name for s in CASES} >= {"contract_demo", *names}


def test_registrar_api(tmp_path):
    samples: list[Sample] = []
    sample = registrar(samples, "tests.factories.example")

    @sample("example_one", platforms=("win32", "linux"), volatile=["x:*"], mutations={"k": 1},
            tags={"read_only"})
    def build(directory: Path) -> Path:
        p = directory / "one.txt"
        p.write_text("1")
        return p

    assert build(tmp_path).read_text() == "1"  # decorator returns the builder unchanged
    (s,) = samples
    assert (s.name, s.module, s.platforms, s.volatile, dict(s.mutations), s.tags) == \
        ("example_one", "example", ("win32", "linux"), ("x:*",), {"k": 1}, frozenset({"read_only"}))
    assert s.make(tmp_path / "out").read_text() == "1"
    with pytest.raises(TypeError):
        s.mutations["k"] = 2  # read-only mapping
    with pytest.raises(ValueError):
        sample("example_one")(build)
    with pytest.raises(ValueError):
        sample("Bad-Name")(build)

    @sample("escapes")
    def escape(directory: Path) -> Path:
        p = directory.parent / "outside.txt"
        p.write_text("x")
        return p
    with pytest.raises(AssertionError, match="outside"):
        samples[-1].make(tmp_path / "jail")


def test_factory_cli_builds_every_available_sample(tmp_path, capsys):
    from tests.factories.__main__ import main
    assert main(["--out", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    for s in factories.ALL:
        assert s.name in out
        if s.available:
            assert (tmp_path / s.name).is_dir()
    if not factories.ALL:
        assert "No samples registered." in out


# ──────────────────────── shared hachoir reader (refactor) ───────────────────

def test_hachoir_extract_fields_is_shared_by_video_handler(tmp_path):
    import wave
    from core.handlers import hachoir_handler, video_handler
    assert video_handler._read_hachoir is hachoir_handler.extract_fields
    wav = tmp_path / "tone.wav"
    with wave.open(str(wav), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(bytes(16000))
    fields, errors = hachoir_handler.extract_fields(wav)
    assert errors == [] and fields
    assert all(f.key.startswith("hachoir:") and f.source == "hachoir"
               and not f.editable and not f.deletable for f in fields)
    noise = tmp_path / "noise.mkv"
    noise.write_bytes(bytes([0x13, 0x37]) * 64)
    assert hachoir_handler.extract_fields(noise) == ([], ["hachoir could not parse file"])
