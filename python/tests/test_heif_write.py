"""Editing and adding metadata in HEIC / AVIF files."""
import pytest

from core.handlers.heif_handler import HeifHandler
from core.heif import metadata_layout
from core.registry import HandlerRegistry
from tests.factories import heif


def _keys(path):
    return {f.key: f for f in HandlerRegistry.get(path).read(path).fields}


async def _write(client, path, fields=(), delete=()):
    return await client.post("/write", json={"path": str(path), "fields": list(fields), "delete": list(delete)})


@pytest.mark.asyncio
async def test_a_short_value_is_written_where_it_is(client, tmp_path):
    path = heif.write_heif(tmp_path / "a.heic")
    original, media = path.read_bytes(), HeifHandler().content_digest(path)
    resp = await _write(client, path, [{"key": "exif:Artist", "value": "Al"}])
    assert resp.status_code == 200, resp.text
    assert _keys(path)["exif:Artist"].value == "Al"
    assert len(path.read_bytes()) == len(original) and HeifHandler().content_digest(path) == media
    undo = await client.post("/undo", json={"token": resp.json()["undo_token"]})
    assert undo.status_code == 200 and path.read_bytes() == original


@pytest.mark.asyncio
@pytest.mark.parametrize("name, major", [("a.heic", b"heic"), ("b.avif", b"avif")])
async def test_a_longer_exif_moves_the_item_to_a_new_atom_at_the_end(client, tmp_path, name, major):
    path = heif.write_heif(tmp_path / name, major=major)
    original, media = path.read_bytes(), HeifHandler().content_digest(path)
    name_long = "Grace Brewster Murray Hopper, rear admiral of the navy"
    resp = await _write(client, path, [{"key": "exif:Artist", "value": name_long},
                                       {"key": "exif:Copyright", "value": "(c) 2026 Ada"}])
    assert resp.status_code == 200, resp.text
    keys = _keys(path)
    assert keys["exif:Artist"].value == name_long and keys["exif:Copyright"].value == "(c) 2026 Ada"
    data = path.read_bytes()
    assert len(data) > len(original) and HeifHandler().content_digest(path) == media
    assert b"EOS Test" in data                                         # unchanged entries are carried over
    assert metadata_layout(data)["exif"]["item"][0] > len(original)    # the item now lives at the end
    undo = await client.post("/undo", json={"token": resp.json()["undo_token"]})
    assert undo.status_code == 200
    assert path.read_bytes() == original


@pytest.mark.asyncio
async def test_the_old_exif_bytes_do_not_remain_after_the_item_moves(client, tmp_path):
    path = heif.write_heif(tmp_path / "a.heic")
    resp = await _write(client, path, [{"key": "exif:Artist", "value": "x" * 300}])
    assert resp.status_code == 200, resp.text
    layout = metadata_layout(path.read_bytes())
    data = path.read_bytes()
    first = data.index(b"mdat")
    assert b"EOS Test" not in data[first:layout["exif"]["item"][0] - 8]


@pytest.mark.asyncio
async def test_xmp_is_edited_and_grows_into_a_new_item(client, tmp_path):
    path = heif.write_heif(tmp_path / "a.heic")
    media = HeifHandler().content_digest(path)
    resp = await _write(client, path, [{"key": "xmp:dc:subject", "value": ["uno", "due"]}])
    assert resp.status_code == 200, resp.text
    assert _keys(path)["xmp:dc:subject"].value == ["uno", "due"]
    big = ["parola" + str(i) for i in range(400)]
    resp2 = await _write(client, path, [{"key": "xmp:dc:subject", "value": big}])
    assert resp2.status_code == 200, resp2.text
    assert _keys(path)["xmp:dc:subject"].value == big and HeifHandler().content_digest(path) == media


@pytest.mark.asyncio
async def test_picture_description_cannot_be_edited_and_missing_xmp_cannot_be_created(client, tmp_path):
    path = heif.write_heif(tmp_path / "a.heic")
    before = path.read_bytes()
    assert (await _write(client, path, [{"key": "img:width", "value": 5}])).status_code == 422
    assert path.read_bytes() == before
    record = HandlerRegistry.get(path).read(path)
    assert {"exif:Copyright"} <= {f.key for f in record.addable_fields}
