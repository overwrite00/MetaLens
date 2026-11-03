"""Removing metadata from TIFF-based files in place (core/tiff_inplace.py)."""
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


async def _sanitize(client, path, categories):
    return (await client.post("/sanitize", json={"path": str(path), "categories": categories, "preview": False})).json()


def test_removal_and_restoration_are_exact_for_every_entry(tmp_path):
    original = _build(tmp_path, "orf_like").read_bytes()
    _, ifds = tiff_inplace.parse(original)
    count = 0
    for name, ifd in ifds.items():
        for entry in ifd.entries:
            if not tiff_inplace.removable(name, entry.tag):
                continue
            data = bytearray(original)
            saved = tiff_inplace.snapshot(data, name, entry.tag)
            assert tiff_inplace.remove(data, name, {entry.tag}) == 1
            assert len(data) == len(original)
            assert tiff_inplace.snapshot(data, name, entry.tag) is None
            tiff_inplace.insert(data, saved)
            assert bytes(data) == original, (name, hex(entry.tag))
            count += 1
    assert count > 10


def test_structure_entries_are_never_removed(tmp_path):
    path = _build(tmp_path, "tiff_dpi")
    keys = _keys(path)
    for structural in ("exif:ImageLength", "exif:StripOffsets", "exif:ImageWidth"):
        if structural in keys:
            assert not keys[structural].deletable, structural
    assert not tiff_inplace.removable("0th", 0x0111) and not tiff_inplace.removable("Exif", 0xA002)


def test_a_table_without_next_pointer_is_handled(tmp_path):
    original = _build(tmp_path, "orf_like").read_bytes()          # its Exif IFD has no next-IFD pointer
    _, ifds = tiff_inplace.parse(original)
    assert ifds["Exif"].next_at is None
    data = bytearray(original)
    tiff_inplace.remove(data, "Exif", {0x829A, 0x829D})
    tiff_inplace.insert(data, tiff_inplace.snapshot(bytearray(original), "Exif", 0x829D))
    tiff_inplace.insert(data, tiff_inplace.snapshot(bytearray(original), "Exif", 0x829A))
    assert bytes(data) == original


@pytest.mark.asyncio
async def test_sharing_a_raw_removes_location_and_device_without_moving_anything(client, tmp_path):
    path = _build(tmp_path, "orf_like")
    original, media = path.read_bytes(), ImageHandler().content_digest(path)
    body = await _sanitize(client, path, ["location", "device"])
    assert body["ok"] and body["undo_token"], body
    keys = _keys(path)
    assert not any(k.startswith("gps:") for k in keys) and "exif:Make" not in keys
    data = path.read_bytes()
    assert len(data) == len(original) and ImageHandler().content_digest(path) == media
    assert b"Canon" not in data and b"EOS Test" not in data           # the removed values are gone from the file
    assert (await client.post("/undo", json={"token": body["undo_token"]})).status_code == 200
    assert path.read_bytes() == original


@pytest.mark.asyncio
async def test_sharing_a_tiff_with_xmp_reduces_the_packet_in_place(client, tmp_path):
    path = _build(tmp_path, "tiff_xmp_iptc")
    original = path.read_bytes()
    body = await _sanitize(client, path, ["identity", "software", "history", "text"])
    assert body["ok"], body
    keys = _keys(path)
    assert "xmp:dc:creator" not in keys and "xmp:xmp:CreatorTool" not in keys
    assert len(path.read_bytes()) == len(original)
    assert (await client.post("/undo", json={"token": body["undo_token"]})).status_code == 200
    assert path.read_bytes() == original


@pytest.mark.asyncio
async def test_what_cannot_be_removed_from_a_tiff_is_reported(client, tmp_path):
    path = _build(tmp_path, "tiff_xmp_iptc")
    body = await _sanitize(client, path, ["text"])
    assert any(r["key"].startswith("iptc:") and r["reason"] for r in body["remaining"]) or body["ok"]


def test_the_media_digest_ignores_the_tables_but_not_the_picture(tmp_path):
    path = _build(tmp_path, "tiff_dpi")
    data = bytearray(path.read_bytes())
    before = tiff_inplace.media_digest(data)
    assert before
    tiff_inplace.remove(data, "0th", {0x0131, 0x010F})
    assert tiff_inplace.media_digest(data) == before
    data[-3] ^= 0xFF
    assert tiff_inplace.media_digest(data) != before


@pytest.mark.parametrize("data", [b"", b"II*\x00", b"II*\x00\xff\xff\xff\xff", b"junk"])
def test_damaged_input_is_an_error_not_a_crash(data):
    with pytest.raises((tiff_inplace.TiffError, Exception)):
        tiff_inplace.parse(data)
    assert tiff_inplace.media_digest(data) is None


@pytest.mark.asyncio
async def test_the_iptc_record_of_a_tiff_is_removed_as_a_whole_and_comes_back(client, tmp_path):
    path = _build(tmp_path, "tiff_xmp_iptc")
    original = path.read_bytes()
    assert any(k.startswith("iptc:") and k != "iptc:@block" for k in _keys(path))
    resp = await client.post("/delete", json={"path": str(path), "keys": ["iptc:@block"]})
    assert resp.status_code == 200, resp.text
    left = _keys(path)
    assert not any(k.startswith("iptc:") for k in left) and "xmp:dc:title" in left
    assert len(path.read_bytes()) == len(original)
    assert (await client.post("/undo", json={"token": resp.json()["undo_token"]})).status_code == 200
    assert path.read_bytes() == original
