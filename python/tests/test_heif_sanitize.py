"""Removing EXIF / GPS / XMP from HEIC and AVIF files in place."""
import pytest

from core.handlers.heif_handler import HeifHandler
from core.heif import metadata_ranges
from core.registry import HandlerRegistry
from tests.factories import heif


def _keys(path):
    return {f.key: f for f in HandlerRegistry.get(path).read(path).fields}


async def _sanitize(client, path, categories):
    return (await client.post("/sanitize", json={"path": str(path), "categories": categories, "preview": False})).json()


@pytest.mark.asyncio
@pytest.mark.parametrize("name, major", [("a.heic", b"heic"), ("b.avif", b"avif")])
async def test_location_and_device_are_removed_in_place_and_undo_is_exact(client, tmp_path, name, major):
    path = heif.write_heif(tmp_path / name, major=major)
    original, media = path.read_bytes(), HeifHandler().content_digest(path)
    body = await _sanitize(client, path, ["location", "device", "identity"])
    assert body["ok"] and body["undo_token"], body
    keys = _keys(path)
    assert not any(k.startswith("gps:") for k in keys) and "exif:Make" not in keys and "exif:Artist" not in keys
    data = path.read_bytes()
    assert len(data) == len(original) and HeifHandler().content_digest(path) == media
    assert b"Canon" not in data and b"EOS Test" not in data
    assert (await client.post("/undo", json={"token": body["undo_token"]})).status_code == 200
    assert path.read_bytes() == original


@pytest.mark.asyncio
async def test_xmp_item_is_reduced_without_moving_anything(client, tmp_path):
    path = heif.write_heif(tmp_path / "x.heic")
    original = path.read_bytes()
    body = await _sanitize(client, path, ["identity", "software", "text", "history"])
    assert body["ok"], body
    assert "xmp:dc:creator" not in _keys(path) and len(path.read_bytes()) == len(original)
    assert (await client.post("/undo", json={"token": body["undo_token"]})).status_code == 200
    assert path.read_bytes() == original


def test_the_picture_and_its_description_are_not_removable(tmp_path):
    keys = _keys(heif.write_heif(tmp_path / "x.heic"))
    for key in ("img:width", "heif:MajorBrand", "heif:Rotation", "exif:PixelXDimension", "exif:ExifVersion"):
        assert key not in keys or not keys[key].deletable, key


def test_items_outside_the_file_are_not_edited(tmp_path):
    path = heif.write_heif(tmp_path / "x.heic")
    data = path.read_bytes()
    assert set(metadata_ranges(data)) == {"exif", "xmp"}
    assert metadata_ranges(data[:40]) == {}
    broken = tmp_path / "broken.heic"
    broken.write_bytes(data[:data.index(b"mdat") + 20])          # the items were stored after this point
    record = HandlerRegistry.get(broken).read(broken)
    assert not any(f.deletable for f in record.fields)
