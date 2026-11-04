"""Office Open XML handler (phase F2E): typed core properties, D7 offsets,
read-only app.xml/custom.xml (D16), exact snapshot/restore, signed packages,
document content preserved."""
from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path
from zipfile import ZipFile

import pytest

from core.base_handler import ABSENT
from core.errors import MetadataValueError
from core.handlers.office_handler import OfficeHandler
from core.models import MetadataField
from core.path_security import secure_atomic_write
from tests.factories import office as factory

CORE = "docProps/core.xml"


def _fields(path: Path) -> dict[str, MetadataField]:
    return {f.key: f for f in OfficeHandler().read(path).fields}


def _members(path: Path) -> dict[str, bytes]:
    with ZipFile(path) as z:
        return {i.filename: z.read(i) for i in z.infolist()}


def _txn(path, fn):
    out = []
    secure_atomic_write(path, lambda tmp: out.append(fn(tmp)))
    return out[0] if out else None


def _docx_body(path):
    from docx import Document
    d = Document(path)
    return ([p.text for p in d.paragraphs],
            [[c.text for c in row.cells] for t in d.tables for row in t.rows])


def _xlsx_body(path):
    from openpyxl import load_workbook
    wb = load_workbook(path)
    return {ws.title: [[c.value for c in row] for row in ws.iter_rows()] for ws in wb.worksheets}


def _pptx_body(path):
    from pptx import Presentation
    prs = Presentation(path)
    return [[(s.shape_type, s.name, s.has_text_frame and s.text_frame.text) for s in slide.shapes]
            for slide in prs.slides]


# ──────────────────────── typed read ─────────────────────────────────────────

def test_core_types(tmp_path):
    path = factory.docx(tmp_path)
    f = _fields(path)
    assert (f["office:created"].field_type, f["office:created"].value) == ("datetime", "2024-01-02T03:04:05+00:00")
    assert f["office:modified"].value == "2024-02-03T04:05:06+00:00"
    assert (f["office:revision"].field_type, f["office:revision"].value) == ("int", 3)
    assert f["office:title"].value == "Quarterly report" and f["office:title"].editable
    record = OfficeHandler().read(path)
    assert record.format_supports_write and record.read_only_reason is None


def test_non_utc_offset_preserved(tmp_path):
    path = factory.docx(tmp_path)
    handler = OfficeHandler()
    handler.write(path, [MetadataField("office:created", "C", "2025-05-06T07:08:09+02:00")])
    assert _fields(path)["office:created"].value == "2025-05-06T07:08:09+02:00"
    assert b"2025-05-06T07:08:09+02:00" in _members(path)[CORE]
    handler.write(path, [MetadataField("office:modified", "M", "2025-05-06T07:08:09+00:00")])
    assert b"2025-05-06T07:08:09Z" in _members(path)[CORE]


@pytest.mark.parametrize("key, value", [
    ("office:created", "2025-01-01T00:00:00"),     # D7: no offset -> 422 (was treated as UTC)
    ("office:created", "garbage"), ("office:created", None), ("office:title", None),
    ("office:revision", 0), ("office:revision", "x"), ("office:title", 5),
    ("office:title", "bad \x01 char"), ("office:title", "bad \ud800"),
])
def test_invalid_values_rejected(tmp_path, key, value):
    path = factory.docx(tmp_path)
    before = path.read_bytes()
    with pytest.raises(MetadataValueError) as exc:
        OfficeHandler().write(path, [MetadataField(key, key, value)])
    assert exc.value.key == key
    assert path.read_bytes() == before


@pytest.mark.asyncio
async def test_api_naive_datetime_is_422(client, tmp_path):
    path = factory.docx(tmp_path)
    before = path.read_bytes()
    resp = await client.post("/write", json={"path": str(path), "fields": [
        {"key": "office:created", "value": "2025-01-01T00:00:00"}]})
    assert resp.status_code == 422, resp.text
    assert path.read_bytes() == before


def test_offsetless_date_in_file_is_read_only(tmp_path):
    path = factory.docx(tmp_path)
    core = _members(path)[CORE].replace(b"2024-01-02T03:04:05Z", b"2024-01-02T03:04:05")
    factory.rewrite_zip(path, replace={CORE: core})
    record = OfficeHandler().read(path)
    f = {x.key: x for x in record.fields}["office:created"]
    assert (f.field_type, f.value, f.editable, f.deletable) == ("localdatetime", "2024-01-02T03:04:05", False, True)
    assert record.read_errors == [] and any("created" in w for w in record.warnings)


# ──────────────────────── app.xml / custom.xml (D16) ─────────────────────────

def test_app_and_custom_properties_are_read_only_values_that_can_be_removed(tmp_path):
    path = factory.docx_app_custom(tmp_path)
    f = _fields(path)
    expected = {
        "office:app.Company": ("str", "ACME Srl è"), "office:app.Manager": ("str", "Grace"),
        "office:app.Pages": ("int", 3), "office:app.TotalTime": ("int", 42),
        "office:app.ScaleCrop": ("bool", False), "office:app.Application": ("str", "Microsoft Office Word"),
        "office:app.TitlesOfParts": ("str", ["Part One", "Part Two"]),
        "office:custom.Project": ("str", "MetaLens 日本"), "office:custom.Count": ("int", 7),
        "office:custom.Approved": ("bool", True), "office:custom.Due": ("datetime", "2026-03-04T05:06:07+00:00"),
        "office:custom.Ratio": ("float", 0.25),
    }
    for key, (ftype, value) in expected.items():
        assert (f[key].field_type, f[key].value) == (ftype, value), key
        assert not f[key].editable and f[key].deletable, key        # never edited, but removable
    assert f["office:app.TitlesOfParts"].multi
    assert "office:app.HeadingPairs" not in f


@pytest.mark.asyncio
async def test_api_rejects_app_custom_edits_and_unknown_removals(client, tmp_path):
    path = factory.docx_app_custom(tmp_path)
    before = path.read_bytes()
    for key in ("office:app.Company", "office:custom.Project"):
        resp = await client.post("/write", json={"path": str(path), "fields": [{"key": key, "value": "x"}]})
        assert resp.status_code == 422                    # values are never edited
    assert path.read_bytes() == before
    resp = await client.post("/delete", json={"path": str(path), "keys": ["office:app.NoSuchProperty"]})
    assert resp.status_code == 422 and path.read_bytes() == before


def test_core_edit_keeps_app_and_custom_parts(tmp_path):
    path = factory.docx_app_custom(tmp_path)
    before = _members(path)
    OfficeHandler().write(path, [MetadataField("office:title", "T", "New")])
    after = _members(path)
    assert set(after) == set(before)
    assert all(after[k] == v for k, v in before.items() if k != CORE)


# ──────────────────────── snapshot / restore ─────────────────────────────────

def test_snapshot_restore_exact_node(tmp_path):
    path = factory.docx(tmp_path)
    handler = OfficeHandler()
    keys = ["office:created", "office:title", "office:revision"]
    snap = _txn(path, lambda tmp: (handler.snapshot(tmp, keys), handler.apply_delete(tmp, keys))[0])
    assert all(v is not ABSENT for v in snap.values())
    assert not set(keys) & set(_fields(path))
    _txn(path, lambda tmp: handler.restore(tmp, snap))
    assert handler.snapshot(path, keys) == snap
    assert b'xsi:type="dcterms:W3CDTF"' in _members(path)[CORE]
    assert _fields(path)["office:created"].value == "2024-01-02T03:04:05+00:00"


def test_snapshot_of_absent_and_restore_removes(tmp_path):
    path = factory.docx(tmp_path)
    handler = OfficeHandler()
    handler.delete(path, ["office:category"])
    snap = handler.snapshot(path, ["office:category"])
    assert snap == {"office:category": ABSENT}
    handler.write(path, [MetadataField("office:category", "C", "added")])
    _txn(path, lambda tmp: handler.restore(tmp, snap))
    assert "office:category" not in _fields(path)


def test_write_of_current_values_does_not_touch_the_file(tmp_path):
    path = factory.docx(tmp_path)
    before = path.read_bytes()
    handler = OfficeHandler()
    handler.write(path, [f for f in handler.read(path).fields if f.editable])
    assert path.read_bytes() == before


def test_writable_fields_schema(tmp_path):
    path = factory.xlsx(tmp_path)
    handler = OfficeHandler()
    record = handler.read(path)
    addable = {f.key: f for f in handler.writable_fields(path, record)}
    present = {f.key for f in record.fields}
    assert not set(addable) & present
    assert "office:revision" in addable and addable["office:revision"].field_type == "int"
    handler.write(path, [replace(addable["office:revision"], value=5)])
    assert _fields(path)["office:revision"].value == 5


# ──────────────────────── content preservation ───────────────────────────────

@pytest.mark.parametrize("name, body", [("docx", _docx_body), ("xlsx", _xlsx_body),
                                        ("pptx", _pptx_body), ("xlsm_macro", _xlsx_body)])
def test_document_content_preserved(tmp_path, name, body):
    path = getattr(factory, name)(tmp_path)
    content = body(path)
    members = _members(path)
    handler = OfficeHandler()
    handler.write(path, [MetadataField("office:title", "T", "MetaLens è 日本 €"),
                         MetadataField("office:modified", "M", "2026-01-01T00:00:00+01:00")])
    handler.delete(path, ["office:author"])
    assert body(path) == content
    after = _members(path)
    assert set(after) == set(members)
    assert all(after[k] == v for k, v in members.items() if k != CORE)
    f = _fields(path)
    assert f["office:title"].value == "MetaLens è 日本 €" and "office:author" not in f


# ──────────────────────── signed packages ────────────────────────────────────

def test_signed_package_read_only(tmp_path):
    path = factory.docx_signed_like(tmp_path)
    handler = OfficeHandler()
    record = handler.read(path)
    assert not record.format_supports_write and not record.format_supports_delete
    assert record.read_only_reason and "sign" in record.read_only_reason.lower()
    assert all(not f.editable and not f.deletable for f in record.fields)
    assert handler.writable_fields(path, record) == []
    before = path.read_bytes()
    with pytest.raises(ValueError, match="signed"):
        handler.write(path, [MetadataField("office:title", "T", "x")])
    with pytest.raises(ValueError, match="signed"):
        _txn(path, lambda tmp: handler.restore(tmp, {"office:title": ABSENT}))
    assert path.read_bytes() == before


# ──────────────────────── Windows hidden files ───────────────────────────────

@pytest.mark.skipif(sys.platform != "win32", reason="hidden attribute is Windows-specific")
def test_apply_write_on_hidden_file(tmp_path):
    import ctypes
    FILE_ATTRIBUTE_HIDDEN = 0x2
    path = factory.docx(tmp_path)
    assert ctypes.windll.kernel32.SetFileAttributesW(str(path), FILE_ATTRIBUTE_HIDDEN)
    handler = OfficeHandler()
    handler.apply_write(path, [MetadataField("office:title", "T", "hidden ok")])
    handler.apply_delete(path, ["office:subject"])
    f = _fields(path)
    assert f["office:title"].value == "hidden ok" and "office:subject" not in f
    assert ctypes.windll.kernel32.GetFileAttributesW(str(path)) & FILE_ATTRIBUTE_HIDDEN
