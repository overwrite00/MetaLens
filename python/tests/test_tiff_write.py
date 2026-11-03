"""Editing and adding metadata in TIFF-based files (nothing existing is moved)."""
import pytest

from core import tiff_inplace
from core.handlers.image_handler import ImageHandler
from core.registry import HandlerRegistry
from tests.factories import by_name


def _build(tmp_path, name):
    folder = tmp_path / name
    folder.mkdir()
    return by_name(name).build(folder)


def _keys(path):
    return {f.key: f for f in HandlerRegistry.get(path).read(path).fields}


async def _write(client, path, fields=(), delete=()):
    return await client.post("/write", json={"path": str(path), "fields": list(fields), "delete": list(delete)})


async def _undo(client, resp):
    return await client.post("/undo", json={"token": resp.json()["undo_token"]})


@pytest.mark.asyncio
async def test_a_shorter_value_is_written_over_the_old_one(client, tmp_path):
    path = _build(tmp_path, "orf_like")
    original, media = path.read_bytes(), ImageHandler().content_digest(path)
    assert _keys(path)["exif:Artist"].editable
    resp = await _write(client, path, [{"key": "exif:Artist", "value": "Al"}])
    assert resp.status_code == 200, resp.text
    assert _keys(path)["exif:Artist"].value == "Al"
    assert len(path.read_bytes()) == len(original) and ImageHandler().content_digest(path) == media
    assert (await _undo(client, resp)).status_code == 200 and path.read_bytes() == original


@pytest.mark.asyncio
async def test_a_longer_value_is_appended_and_the_old_one_is_zeroed(client, tmp_path):
    path = _build(tmp_path, "orf_like")
    original, media = path.read_bytes(), ImageHandler().content_digest(path)
    long_name = "Grace Brewster Murray Hopper, rear admiral"
    resp = await _write(client, path, [{"key": "exif:Artist", "value": long_name}])
    assert resp.status_code == 200, resp.text
    assert _keys(path)["exif:Artist"].value == long_name
    data = path.read_bytes()
    assert len(data) > len(original) and ImageHandler().content_digest(path) == media
    assert data[:len(original)].count("José".encode("latin-1")) == 0           # the old value does not remain
    assert (await _undo(client, resp)).status_code == 200
    assert path.read_bytes()[:len(original)] == original


@pytest.mark.asyncio
async def test_new_entries_are_added_and_missing_ifds_are_created(client, tmp_path):
    path = _build(tmp_path, "tiff_dpi")                       # has neither an Exif IFD nor a copyright
    original, media = path.read_bytes(), ImageHandler().content_digest(path)
    record = HandlerRegistry.get(path).read(path)
    offered = {f.key for f in record.addable_fields}
    assert {"exif:Copyright", "exif:DateTimeOriginal"} <= offered
    resp = await _write(client, path, [{"key": "exif:Copyright", "value": "(c) Ada"},
                                       {"key": "exif:DateTimeOriginal", "value": "2026-02-03T04:05:06"}])
    assert resp.status_code == 200, resp.text
    keys = _keys(path)
    assert keys["exif:Copyright"].value == "(c) Ada" and keys["exif:DateTimeOriginal"].value == "2026-02-03T04:05:06"
    assert ImageHandler().content_digest(path) == media
    tiff_inplace.parse(path.read_bytes())                      # still a well-formed TIFF
    from PIL import Image
    with Image.open(path) as img:
        img.load()
    assert (await _undo(client, resp)).status_code == 200
    assert "exif:Copyright" not in _keys(path) and "exif:DateTimeOriginal" not in _keys(path)
    assert ImageHandler().content_digest(path) == media


@pytest.mark.asyncio
async def test_xmp_is_added_to_a_tiff_and_then_edited(client, tmp_path):
    path = _build(tmp_path, "tiff_dpi")
    media = ImageHandler().content_digest(path)
    resp = await _write(client, path, [{"key": "xmp:dc:title", "value": "Un titolo"}])
    assert resp.status_code == 200, resp.text
    assert _keys(path)["xmp:dc:title"].value == "Un titolo"
    longer = "Un titolo molto piu lungo " * 40
    resp2 = await _write(client, path, [{"key": "xmp:dc:title", "value": longer}])
    assert resp2.status_code == 200, resp2.text
    assert _keys(path)["xmp:dc:title"].value == longer and ImageHandler().content_digest(path) == media


@pytest.mark.asyncio
async def test_picture_structure_cannot_be_edited(client, tmp_path):
    path = _build(tmp_path, "tiff_dpi")
    before = path.read_bytes()
    resp = await _write(client, path, [{"key": "exif:ImageWidth", "value": 5}])
    assert resp.status_code == 422 and path.read_bytes() == before


@pytest.mark.asyncio
async def test_a_raw_edit_keeps_the_maker_note_where_it_is(client, tmp_path):
    path = _build(tmp_path, "orf_like")
    before = path.read_bytes()
    _, ifds = tiff_inplace.parse(before)
    note = next(e for e in ifds["Exif"].entries if e.tag == 0x927C)
    resp = await _write(client, path, [{"key": "exif:Artist", "value": "A much longer artist name than before"}])
    assert resp.status_code == 200, resp.text
    after = path.read_bytes()
    _, ifds_after = tiff_inplace.parse(after)
    moved = next(e for e in ifds_after["Exif"].entries if e.tag == 0x927C)
    assert moved.data_at == note.data_at and after[note.data_at:note.data_at + note.size] == \
        before[note.data_at:note.data_at + note.size]
