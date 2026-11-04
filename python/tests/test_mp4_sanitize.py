"""Removing QuickTime structure fields and XMP from MP4-family files, in place."""
import pytest

from core.handlers import qt_patch
from core.handlers.video_handler import VideoHandler
from core.registry import HandlerRegistry
from tests.factories import mp4


@pytest.fixture
def phone(tmp_path):
    return mp4.write_phone_movie(tmp_path / "phone.mov")


def _keys(path):
    return {f.key: f for f in HandlerRegistry.get(path).read(path).fields}


def _digest(path):
    return VideoHandler().content_digest(path)


async def _sanitize(client, path, categories):
    return (await client.post("/sanitize", json={"path": str(path), "categories": categories, "preview": False})).json()


def test_removable_fields_are_marked(phone):
    keys = _keys(phone)
    for key in ("qt:CreateDate", "qt:track1:CreateDate", "qt:udta:©xyz",
                "qt:mdta:com.apple.quicktime.location.ISO6709", "xmp:xmp:CreatorTool"):
        assert keys[key].deletable, key
    assert not keys["qt:track1:Width"].deletable and not keys["qt:MajorBrand"].deletable
    assert keys["xmp:xmp:CreatorTool"].editable


@pytest.mark.asyncio
async def test_location_is_removed_in_place_and_undo_gives_the_exact_bytes_back(client, phone):
    original, media = phone.read_bytes(), _digest(phone)
    body = await _sanitize(client, phone, ["location"])
    assert body["ok"] and body["undo_token"] and body["remaining"] == []
    keys = _keys(phone)
    assert "qt:udta:©xyz" not in keys and "qt:mdta:com.apple.quicktime.location.ISO6709" not in keys
    assert "qt:mdta:com.apple.quicktime.model" in keys                    # other categories untouched
    after = phone.read_bytes()
    assert len(after) == len(original) and _digest(phone) == media        # nothing moved, media untouched
    assert b"+45.4642" not in after
    assert (await client.post("/undo", json={"token": body["undo_token"]})).status_code == 200
    assert phone.read_bytes() == original


@pytest.mark.asyncio
async def test_removed_values_do_not_remain_in_the_file(client, phone):
    await _sanitize(client, phone, ["location", "device"])
    data = phone.read_bytes()
    for secret in (b"+45.4642+009.1900", b"iPhone 15 Pro", b"Apple", b"com.apple.quicktime.location"):
        assert secret not in data, secret


@pytest.mark.asyncio
async def test_dates_are_cleared(client, phone):
    body = await _sanitize(client, phone, ["datetime"])
    keys = _keys(phone)
    assert body["ok"] and "qt:CreateDate" not in keys and "qt:ModifyDate" not in keys
    assert "qt:track1:CreateDate" not in keys and "qt:track2:CreateDate" not in keys


@pytest.mark.asyncio
async def test_xmp_is_reduced_in_place(client, phone):
    original = phone.read_bytes()
    body = await _sanitize(client, phone, ["identity", "software", "text", "history"])
    keys = _keys(phone)
    assert body["ok"] and "xmp:dc:creator" not in keys and "xmp:xmp:CreatorTool" not in keys
    assert len(phone.read_bytes()) == len(original)
    assert (await client.post("/undo", json={"token": body["undo_token"]})).status_code == 200
    assert phone.read_bytes() == original


@pytest.mark.asyncio
async def test_tags_and_structure_can_be_removed_together(client, phone):
    mp4.add_full_tags(phone)
    media = _digest(phone)
    original = phone.read_bytes()
    resp = await client.post("/write", json={"path": str(phone), "fields": [], "delete": [
        "mp4:©nam", "qt:udta:©xyz", "xmp:xmp:Rating"]})
    assert resp.status_code == 200, resp.text
    keys = _keys(phone)
    assert "mp4:©nam" not in keys and "qt:udta:©xyz" not in keys and "xmp:xmp:Rating" not in keys
    assert _digest(phone) == media
    assert (await client.post("/undo", json={"token": resp.json()["undo_token"]})).status_code == 200
    assert "mp4:©nam" in _keys(phone)
    assert phone.read_bytes() == original


def test_plan_ignores_files_it_cannot_patch(tmp_path):
    path = tmp_path / "x.mov"
    path.write_bytes(b"")
    assert qt_patch.plan(path) == {}
    path.write_bytes(mp4.atom(b"ftyp", b"qt  " + bytes(8)))
    assert qt_patch.plan(path) == {}
