"""Batch preparation, date shift, tag copy, export and rename (api/operations.py)."""
import csv
import io

import pytest

from core.registry import HandlerRegistry
from tests.factories import by_name


def _build(tmp_path, name, folder=None):
    base = (folder or tmp_path) / name
    base.mkdir(parents=True)
    return by_name(name).build(base)


def _keys(path):
    return {f.key: f for f in HandlerRegistry.get(path).read(path).fields}


@pytest.mark.asyncio
async def test_batch_preparation_reports_every_file_and_keeps_going(client, tmp_path):
    good = [_build(tmp_path, "jpeg_camera"), _build(tmp_path, "pdf_basic")]
    paths = [str(good[0]), str(tmp_path / "missing.jpg"), str(good[1])]
    preview = (await client.post("/sanitize-many", json={"paths": paths, "categories": ["identity", "location"]})).json()
    assert [r["ok"] for r in preview["results"]] == [True, False, True] and preview["undo_tokens"] == []
    failed = preview["results"][1]                   # a failed file carries structured errors, never exception text
    assert "detail" not in failed and failed["errors"] and set(failed["errors"][0]) == {"code", "key", "message"}
    body = (await client.post("/sanitize-many", json={"paths": paths, "categories": ["location", "identity"],
                                                      "preview": False})).json()
    assert [r["ok"] for r in body["results"]] == [True, False, True]
    assert len(body["undo_tokens"]) >= 1 and not any(k.startswith("gps:") for k in _keys(good[0]))
    for token in body["undo_tokens"]:
        assert (await client.post("/undo", json={"token": token})).status_code == 200
    assert any(k.startswith("gps:") for k in _keys(good[0]))


@pytest.mark.asyncio
async def test_the_batch_size_is_limited(client):
    resp = await client.post("/sanitize-many", json={"paths": ["x"] * 501, "categories": ["location"]})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_dates_are_shifted_with_a_preview_and_an_undo(client, tmp_path):
    path = _build(tmp_path, "jpeg_camera")
    before = _keys(path)["exif:DateTimeOriginal"].value
    preview = (await client.post("/shift-dates", json={"paths": [str(path)], "seconds": 3600})).json()
    change = next(c for c in preview["results"][0]["changes"] if c["key"] == "exif:DateTimeOriginal")
    assert change["from"] == before and change["to"] == "2026-01-02T04:04:05"
    assert _keys(path)["exif:DateTimeOriginal"].value == before                    # a preview changes nothing
    body = (await client.post("/shift-dates", json={"paths": [str(path)], "seconds": -86400, "preview": False})).json()
    assert body["ok"] and body["undo_tokens"]
    assert _keys(path)["exif:DateTimeOriginal"].value == "2026-01-01T03:04:05"
    assert (await client.post("/undo", json={"token": body["undo_tokens"][0]})).status_code == 200
    assert _keys(path)["exif:DateTimeOriginal"].value == before


@pytest.mark.asyncio
async def test_shift_can_be_limited_to_some_keys_and_keeps_offsets(client, tmp_path):
    path = _build(tmp_path, "jpeg_xmp_first")
    body = (await client.post("/shift-dates", json={"paths": [str(path)], "seconds": 60, "preview": False,
                                                    "keys": ["xmp:xmp:CreateDate"]})).json()
    assert body["ok"], body
    keys = _keys(path)
    assert keys["xmp:xmp:CreateDate"].value == "2026-01-02T03:05:05+02:00"
    assert keys["exif:DateTimeOriginal"].value == "2026-01-02T03:04:05"


@pytest.mark.asyncio
async def test_tags_are_copied_to_other_files(client, tmp_path):
    source = _build(tmp_path, "jpeg_camera")
    target = _build(tmp_path, "jpeg_no_exif")
    pdf = _build(tmp_path, "pdf_basic")
    preview = (await client.post("/copy-tags", json={"source": str(source), "targets": [str(target), str(pdf)],
                                                     "keys": ["exif:Artist", "exif:Copyright"]})).json()
    assert preview["results"][0]["will_copy"] == ["exif:Artist"] or "exif:Artist" in preview["results"][0]["will_copy"]
    assert preview["results"][1]["will_copy"] == [] and preview["results"][1]["skipped"]
    body = (await client.post("/copy-tags", json={"source": str(source), "targets": [str(target)], "preview": False,
                                                  "keys": ["exif:Artist"]})).json()
    assert body["ok"] and _keys(target)["exif:Artist"].value == _keys(source)["exif:Artist"].value


@pytest.mark.asyncio
async def test_export_json_and_csv_with_protection_from_formulas(client, tmp_path):
    folder = tmp_path / "pics"
    folder.mkdir()
    sample = _build(tmp_path, "jpeg_camera", folder=tmp_path / "src")
    (folder / "=cmd.jpg").write_bytes(sample.read_bytes())
    (folder / "b.jpg").write_bytes(sample.read_bytes())
    data = (await client.get("/export", params={"path": str(folder), "format": "json"})).json()
    assert [r["file"] for r in data] == ["=cmd.jpg", "b.jpg"] and "exif:Make" in data[0]["fields"]
    resp = await client.get("/export", params={"path": str(folder), "format": "csv"})
    assert resp.headers["content-type"].startswith("text/csv") and "attachment" in resp.headers["content-disposition"]
    rows = list(csv.reader(io.StringIO(resp.text.lstrip("﻿"))))
    assert rows[0][:2] == ["file", "handler"] and "exif:Make" in rows[0]
    assert rows[1][0] == "'=cmd.jpg"                                   # never starts with a formula character
    assert (await client.get("/export", params={"path": str(folder), "format": "xml"})).status_code == 422


@pytest.mark.asyncio
async def test_rename_from_metadata(client, tmp_path):
    path = _build(tmp_path, "jpeg_camera")
    preview = (await client.post("/rename", json={"path": str(path),
                                                  "template": "{exif:DateTimeOriginal|%Y%m%d_%H%M%S}_{name}"})).json()
    assert preview["new"].endswith("20260102_030405_camera.jpg") and not preview["renamed"] and path.exists()
    body = (await client.post("/rename", json={"path": str(path), "preview": False,
                                               "template": "{exif:DateTimeOriginal|%Y-%m-%d} {exif:Make}"})).json()
    assert body["renamed"] and not path.exists() and (path.parent / "2026-01-02 Canon.jpg").exists()


@pytest.mark.asyncio
async def test_rename_refuses_collisions_unknown_fields_and_unsafe_names(client, tmp_path):
    path = _build(tmp_path, "jpeg_camera")
    other = path.parent / "taken.jpg"
    other.write_bytes(b"x")
    assert (await client.post("/rename", json={"path": str(path), "preview": False, "template": "taken"})).status_code == 409
    assert (await client.post("/rename", json={"path": str(path), "template": "{gps:Nothing}"})).status_code == 422
    assert (await client.post("/rename", json={"path": str(path), "template": "{oops"})).status_code == 422
    safe = (await client.post("/rename", json={"path": str(path), "template": "a/b\\c:d"})).json()
    assert path.parent == type(path)(safe["new"]).parent
    assert (await client.post("/rename", json={"path": str(path), "template": "CON"})).status_code == 422
