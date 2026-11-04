"""Prepare a file for sharing (POST /sanitize)."""
import pytest

from core.registry import HandlerRegistry
from tests.factories import by_name

PRIVATE = ["location", "identity", "device", "thumbnail", "history"]


def _build(tmp_path, name):
    folder = tmp_path / name
    folder.mkdir()
    return by_name(name).build(folder)


def _keys(path):
    return {f.key: f for f in HandlerRegistry.get(path).read(path).fields}


async def _sanitize(client, path, categories, preview=False, keep=()):
    resp = await client.post("/sanitize", json={"path": str(path), "categories": categories,
                                                "preview": preview, "keep": list(keep)})
    return resp


@pytest.mark.asyncio
async def test_preview_reports_without_touching_the_file(client, tmp_path):
    path = _build(tmp_path, "jpeg_camera")
    before = path.read_bytes()
    body = (await _sanitize(client, path, ["location"], preview=True)).json()
    assert body["ok"] and body["preview"]
    assert {"gps:Latitude", "gps:Longitude"} <= {e["key"] for e in body["will_remove"]}
    assert all(e["category"] == "location" for e in body["will_remove"])
    assert path.read_bytes() == before and body["undo_token"] is None


@pytest.mark.asyncio
async def test_location_is_removed_and_undo_restores_it(client, tmp_path):
    path = _build(tmp_path, "jpeg_camera")
    original = path.read_bytes()
    resp = await _sanitize(client, path, ["location"])
    body = resp.json()
    assert resp.status_code == 200 and body["undo_token"]
    assert not any(k.startswith("gps:") for k in _keys(path))
    assert "exif:Make" in _keys(path)                       # other categories are untouched
    assert body["remaining"] == [] and body["removed"]
    undo = await client.post("/undo", json={"token": body["undo_token"]})
    assert undo.status_code == 200
    assert any(k.startswith("gps:") for k in _keys(path))
    assert path.read_bytes() == original                       # the Undo gives the original bytes back


@pytest.mark.asyncio
async def test_several_categories_in_one_save(client, tmp_path):
    path = _build(tmp_path, "jpeg_camera")
    body = (await _sanitize(client, path, PRIVATE)).json()
    keys = _keys(path)
    assert not any(k.startswith("gps:") for k in keys) and "exif:Make" not in keys and "exif:Artist" not in keys
    assert body["undo_token"]
    assert body["remaining"] == [] or all(r["category"] in PRIVATE for r in body["remaining"])


@pytest.mark.asyncio
async def test_keep_leaves_chosen_fields(client, tmp_path):
    path = _build(tmp_path, "jpeg_camera")
    await _sanitize(client, path, ["identity", "device"], keep=["exif:Make"])
    keys = _keys(path)
    assert "exif:Make" in keys and "exif:Artist" not in keys


@pytest.mark.asyncio
async def test_what_cannot_be_removed_is_reported(client, tmp_path):
    path = _build(tmp_path, "gif_comment")                  # a read-only format
    body = (await _sanitize(client, path, ["text"])).json()
    assert body["will_remove"] == [] and body["undo_token"] is None
    assert body["remaining"] and all(r["reason"] for r in body["remaining"])


@pytest.mark.asyncio
async def test_works_on_other_writable_formats(client, tmp_path):
    for name, category, gone in (("pdf_basic", "identity", "pdf:Author"), ("docx", "identity", "office:author"),
                                 ("mp3_v24_full", "text", "tag:COMM:note:eng")):
        path = _build(tmp_path, name)
        if gone not in _keys(path):
            continue
        body = (await _sanitize(client, path, [category])).json()
        assert body["ok"] and gone not in _keys(path), name


@pytest.mark.asyncio
async def test_unknown_category_and_bad_path_are_rejected(client, tmp_path):
    path = _build(tmp_path, "jpeg_camera")
    assert (await _sanitize(client, path, ["nonsense"])).status_code == 422
    assert (await _sanitize(client, tmp_path / "missing.jpg", ["location"])).status_code in (400, 404)
    resp = await client.post("/sanitize", json={"path": str(path), "categories": []})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_nothing_to_remove_is_not_an_error(client, tmp_path):
    path = _build(tmp_path, "jpeg_no_exif")
    body = (await _sanitize(client, path, ["location"])).json()
    assert body["ok"] and body["will_remove"] == [] and body["undo_token"] is None


@pytest.mark.asyncio
async def test_hidden_xmp_data_is_listed_removed_and_restored_by_undo(client, tmp_path):
    path = _build(tmp_path, "jpeg_xmp_first")
    original = path.read_bytes()
    keys = _keys(path)
    assert "xmp:@hidden" in keys and keys["xmp:@hidden"].deletable
    body = (await _sanitize(client, path, ["history"])).json()
    assert [e["key"] for e in body["will_remove"]] == ["xmp:@hidden"] or body["removed"]
    assert "xmp:@hidden" not in _keys(path) and body["remaining"] == []
    undo = await client.post("/undo", json={"token": body["undo_token"]})
    assert undo.status_code == 200 and "xmp:@hidden" in _keys(path)
    assert path.read_bytes() == original
