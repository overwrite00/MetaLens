"""Matroska / WebM: fields, removal, in-place edit (core/ebml.py, handlers/mkv_fields.py)."""
import pytest

from core import ebml, sensitivity
from core.registry import HandlerRegistry
from tests.factories import by_name, mkv


def _build(tmp_path, name="mkv_full"):
    folder = tmp_path / name
    folder.mkdir()
    return by_name(name).build(folder)


def _keys(path):
    return {f.key: f for f in HandlerRegistry.get(path).read(path).fields}


def _media(path):
    return ebml.media_digest(path)


async def _write(client, path, fields=(), delete=()):
    return await client.post("/write", json={"path": str(path), "fields": list(fields), "delete": list(delete)})


def test_fields_of_a_muxed_file(tmp_path):
    path = _build(tmp_path)
    record = HandlerRegistry.get(path).read(path)
    keys = {f.key: f for f in record.fields}
    assert record.read_errors == [] and record.handler_name == "video"
    assert keys["mkv:DocType"].value == "matroska"
    assert keys["mkv:Info:Title"].value == "Vacanze \u00e8 2026" and keys["mkv:Info:Title"].editable
    assert keys["mkv:Info:WritingApp"].value.startswith("mkvmerge")
    assert keys["mkv:Info:Duration"].value == 2.5 and keys["mkv:Info:Duration"].computed
    assert keys["mkv:tag:ARTIST"].value == "Ada Lovelace" and keys["mkv:tag:ARTIST"].deletable
    assert keys["mkv:attachment:cover.jpg"].binary["mime"] == "image/jpeg"
    assert keys["mkv:track1:CodecID"].value == "V_MPEG4/ISO/AVC" and not keys["mkv:track1:CodecID"].deletable


def test_the_fields_are_classified(tmp_path):
    for key, category in (("mkv:Info:WritingApp", "software"), ("mkv:Info:DateUTC", "datetime"),
                          ("mkv:tag:ARTIST", "identity"), ("mkv:attachment:cover.jpg", "thumbnail"),
                          ("mkv:tag:TITLE", "text")):
        assert sensitivity.classify(key) == category


@pytest.mark.asyncio
async def test_everything_private_is_removed_in_place_and_undo_is_exact(client, tmp_path):
    path = _build(tmp_path)
    original, media = path.read_bytes(), _media(path)
    body = (await client.post("/sanitize", json={"path": str(path), "preview": False,
                                                 "categories": ["identity", "software", "datetime", "thumbnail", "text"]})).json()
    assert body["ok"] and body["undo_token"], body
    keys = _keys(path)
    for gone in ("mkv:tag:ARTIST", "mkv:Info:WritingApp", "mkv:Info:MuxingApp", "mkv:Info:DateUTC", "mkv:Info:Title",
                 "mkv:attachment:cover.jpg"):
        assert gone not in keys, gone
    assert "mkv:track1:CodecID" in keys
    data = path.read_bytes()
    assert len(data) == len(original) and _media(path) == media
    for secret in (b"Ada Lovelace", b"mkvmerge", b"libebml", b"cover.jpg"):
        assert secret not in data, secret
    assert (await client.post("/undo", json={"token": body["undo_token"]})).status_code == 200
    assert path.read_bytes() == original


@pytest.mark.asyncio
async def test_a_text_is_written_over_the_old_one_when_it_fits(client, tmp_path):
    path = _build(tmp_path)
    original = path.read_bytes()
    resp = await _write(client, path, [{"key": "mkv:tag:ARTIST", "value": "Al"}, {"key": "mkv:Info:Title", "value": "Mare"}])
    assert resp.status_code == 200, resp.text
    keys = _keys(path)
    assert keys["mkv:tag:ARTIST"].value == "Al" and keys["mkv:Info:Title"].value == "Mare"
    assert len(path.read_bytes()) == len(original) and ebml.Segment(path.read_bytes())
    assert (await client.post("/undo", json={"token": resp.json()["undo_token"]})).status_code == 200
    assert path.read_bytes() == original


@pytest.mark.asyncio
async def test_a_text_that_does_not_fit_is_refused(client, tmp_path):
    path = _build(tmp_path)
    before = path.read_bytes()
    resp = await _write(client, path, [{"key": "mkv:tag:ARTIST", "value": "x" * 200}])
    assert resp.status_code == 422 and path.read_bytes() == before


def test_a_recording_without_a_final_size_is_read_only(tmp_path):
    data = bytearray(mkv.mkv_full_bytes())
    at = data.index(b"\x18\x53\x80\x67") + 4
    size_len = 1
    while not data[at] & (0x80 >> (size_len - 1)):
        size_len += 1
    data[at] = 0xFF >> (size_len - 1)                       # "unknown size": every value bit set
    data[at + 1:at + size_len] = b"\xff" * (size_len - 1)
    path = tmp_path / "live.mkv"
    path.write_bytes(bytes(data))
    record = HandlerRegistry.get(path).read(path)
    assert record.read_errors == [] and not record.format_supports_write
    assert not any(f.editable or f.deletable for f in record.fields)


def test_damaged_files_never_raise(tmp_path):
    data = mkv.mkv_full_bytes()
    for cut in range(0, len(data), 23):
        path = tmp_path / "cut.mkv"
        path.write_bytes(data[:cut])
        HandlerRegistry.get(path).read(path)


def test_matroska_without_extension_is_recognized(tmp_path):
    path = tmp_path / "movie"
    path.write_bytes(mkv.mkv_full_bytes())
    assert HandlerRegistry.get(path).NAME == "video"
    assert "mkv:Info:Title" in _keys(path)
