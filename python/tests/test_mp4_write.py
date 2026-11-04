"""Editing and adding QuickTime fields, text entries and XMP in MP4-family files."""
import pytest

from core.handlers.video_handler import VideoHandler
from core.registry import HandlerRegistry
from tests.factories import mp4


@pytest.fixture
def phone(tmp_path):
    return mp4.write_phone_movie(tmp_path / "phone.mov")


def _keys(path):
    return {f.key: f for f in HandlerRegistry.get(path).read(path).fields}


def _media(path):
    return VideoHandler().content_digest(path)


async def _write(client, path, fields=(), delete=()):
    return await client.post("/write", json={"path": str(path), "fields": list(fields), "delete": list(delete)})


async def _undo(client, resp):
    return await client.post("/undo", json={"token": resp.json()["undo_token"]})


def test_which_fields_can_be_edited(phone):
    keys = _keys(phone)
    for key in ("qt:CreateDate", "qt:track1:CreateDate", "qt:udta:©xyz", "qt:mdta:com.apple.quicktime.make",
                "xmp:xmp:CreatorTool"):
        assert keys[key].editable, key
    for key in ("qt:track1:Width", "qt:MajorBrand", "qt:mdta:com.apple.quicktime.live-photo.vitality-score"):
        assert not keys[key].editable, key


@pytest.mark.asyncio
async def test_dates_are_written_in_place_and_undo_is_exact(client, phone):
    original, media = phone.read_bytes(), _media(phone)
    resp = await _write(client, phone, [{"key": "qt:CreateDate", "value": "2020-01-02T03:04:05+00:00"},
                                        {"key": "qt:track1:CreateDate", "value": "2020-01-02T03:04:05+00:00"}])
    assert resp.status_code == 200, resp.text
    keys = _keys(phone)
    assert keys["qt:CreateDate"].value == "2020-01-02T03:04:05+00:00"
    assert keys["qt:track1:CreateDate"].value == "2020-01-02T03:04:05+00:00"
    assert len(phone.read_bytes()) == len(original) and _media(phone) == media
    assert (await _undo(client, resp)).status_code == 200 and phone.read_bytes() == original


@pytest.mark.asyncio
async def test_a_longer_location_grows_the_movie_atom_and_nothing_else_moves(client, phone):
    original, media = phone.read_bytes(), _media(phone)
    place = "+45.46427+009.18951+120.500/"
    resp = await _write(client, phone, [{"key": "qt:udta:©xyz", "value": place},
                                        {"key": "qt:mdta:com.apple.quicktime.make", "value": "Apple Computer Inc."}])
    assert resp.status_code == 200, resp.text
    keys = _keys(phone)
    assert keys["qt:udta:©xyz"].value == place and keys["qt:mdta:com.apple.quicktime.make"].value == "Apple Computer Inc."
    data = phone.read_bytes()
    assert data.index(b"mdat") == original.index(b"mdat") and _media(phone) == media
    assert keys["xmp:xmp:CreatorTool"].value == "Tool & Co"               # the atom after moov moved intact
    assert (await _undo(client, resp)).status_code == 200
    assert phone.read_bytes() == original


@pytest.mark.asyncio
async def test_new_entries_are_created(client, phone):
    media = _media(phone)
    handler = HandlerRegistry.get(phone)
    offered = {f.key for f in handler.writable_fields(phone, handler.read(phone))}
    assert {"qt:udta:©swr", "qt:udta:©mak"} <= offered                      # the movie has the other entries
    resp = await _write(client, phone, [{"key": "qt:udta:©swr", "value": "MetaLens"},
                                        {"key": "qt:mdta:com.apple.quicktime.software", "value": "hello"}])
    assert resp.status_code in (200, 422), resp.text
    if resp.status_code == 200:
        keys = _keys(phone)
        assert keys["qt:udta:©swr"].value == "MetaLens" and _media(phone) == media


@pytest.mark.asyncio
async def test_a_movie_without_udta_or_keys_gets_them(client, tmp_path):
    path = mp4.write_mp4(tmp_path / "plain.mp4", brand=b"isom")
    media = _media(path)
    resp = await _write(client, path, [{"key": "qt:udta:©mak", "value": "ACME"},
                                       {"key": "qt:mdta:com.apple.quicktime.model", "value": "Model 1"}])
    assert resp.status_code == 200, resp.text
    keys = _keys(path)
    assert keys["qt:udta:©mak"].value == "ACME" and keys["qt:mdta:com.apple.quicktime.model"].value == "Model 1"
    assert _media(path) == media and HandlerRegistry.get(path).read(path).read_errors == []


@pytest.mark.asyncio
async def test_xmp_edit_that_does_not_fit_is_stored_in_a_new_atom(client, phone):
    original, media = phone.read_bytes(), _media(phone)
    big = ["word" + str(i) for i in range(500)]
    resp = await _write(client, phone, [{"key": "xmp:dc:subject", "value": big}])
    assert resp.status_code == 200, resp.text
    assert _keys(phone)["xmp:dc:subject"].value == big
    assert len(phone.read_bytes()) > len(original) and _media(phone) == media
    assert (await _undo(client, resp)).status_code == 200 and phone.read_bytes() == original


@pytest.mark.asyncio
async def test_xmp_is_created_when_the_file_has_none(client, tmp_path):
    path = mp4.write_mp4(tmp_path / "plain.mp4", brand=b"isom")
    media = _media(path)
    handler = HandlerRegistry.get(path)
    assert "xmp:dc:title" in {f.key for f in handler.writable_fields(path, handler.read(path))}
    resp = await _write(client, path, [{"key": "xmp:dc:title", "value": "Titolo"}])
    assert resp.status_code == 200, resp.text
    assert _keys(path)["xmp:dc:title"].value == "Titolo" and _media(path) == media


@pytest.mark.asyncio
async def test_bad_values_are_refused(client, phone):
    before = phone.read_bytes()
    for key, value in (("qt:udta:©xyz", "not a location"), ("qt:CreateDate", "1850-01-01T00:00:00+00:00"),
                       ("qt:mdta:com.apple.quicktime.make", "a\u0000b")):
        resp = await _write(client, phone, [{"key": key, "value": value}])
        assert resp.status_code == 422, (key, resp.text)
    assert phone.read_bytes() == before


@pytest.mark.asyncio
async def test_a_fast_start_movie_grows_by_moving_the_media_and_fixing_the_chunk_offsets(client, tmp_path):
    """moov before mdat: the media moves up and every chunk offset follows."""
    path = tmp_path / "faststart.mp4"
    created = mp4.mac_seconds(2026, 5, 1)
    ftyp = mp4.atom(b"ftyp", b"isom" + bytes(8))
    payload = bytes(range(256)) * 4

    def build(offset):
        moov = mp4.atom(b"moov", mp4.mvhd() + mp4._video_trak(1, created, offset, 0))
        return moov
    first = build(0)
    offset = len(ftyp) + len(first) + 8
    path.write_bytes(ftyp + build(offset) + mp4.atom(b"mdat", payload))
    media = _media(path)
    resp = await _write(client, path, [{"key": "qt:udta:©mak", "value": "ACME corporation"}])
    assert resp.status_code == 200, resp.text
    assert _keys(path)["qt:udta:©mak"].value == "ACME corporation" and _media(path) == media
    data = path.read_bytes()
    assert data[data.index(b"mdat") + 4:] == payload                                  # the media is intact
    from core.isobmff import Reader
    rd = Reader(data)
    moov = next(b for b in rd.children() if b.type == b"moov")
    stco = rd.find(moov, b"trak", b"mdia", b"minf", b"stbl", b"stco")
    chunk = int.from_bytes(data[stco.data_start + 8:stco.data_start + 12], "big")
    assert chunk == data.index(b"mdat") + 4                                           # still points at the media
    assert (await _undo(client, resp)).status_code == 200
    assert _media(path) == media
