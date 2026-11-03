"""Geotag: fixed coordinates or a GPX track (api/geotag.py)."""
import pytest

from core.registry import HandlerRegistry
from tests.factories import by_name

GPX = """<?xml version="1.0"?>
<gpx version="1.1" xmlns="http://www.topografix.com/GPX/1/1"><trk><trkseg>
<trkpt lat="45.0000" lon="9.0000"><ele>100</ele><time>2026-01-02T03:00:00Z</time></trkpt>
<trkpt lat="45.1000" lon="9.2000"><ele>200</ele><time>2026-01-02T05:00:00Z</time></trkpt>
</trkseg></trk></gpx>
"""


def _build(tmp_path, name):
    folder = tmp_path / name
    folder.mkdir()
    return by_name(name).build(folder)


def _keys(path):
    return {f.key: f for f in HandlerRegistry.get(path).read(path).fields}


@pytest.mark.asyncio
async def test_a_position_is_written_to_files_that_have_none(client, tmp_path):
    jpeg, tiff, bmp = _build(tmp_path, "jpeg_no_exif"), _build(tmp_path, "tiff_dpi"), _build(tmp_path, "bmp")
    preview = (await client.post("/geotag", json={"paths": [str(jpeg), str(tiff), str(bmp)], "latitude": -33.8688,
                                                  "longitude": 151.2093, "altitude": 58.5})).json()
    assert [bool(r.get("will_write")) for r in preview["results"]] == [True, True, False]
    body = (await client.post("/geotag", json={"paths": [str(jpeg), str(tiff)], "latitude": -33.8688,
                                               "longitude": 151.2093, "altitude": 58.5, "preview": False})).json()
    assert body["ok"] and len(body["undo_tokens"]) == 2
    for path in (jpeg, tiff):
        keys = _keys(path)
        assert keys["gps:LatitudeRef"].value == "S" and keys["gps:LongitudeRef"].value == "E"
        assert keys["gps:Latitude"].value[0] == "33/1" and keys["gps:Longitude"].value[0] == "151/1"
        assert keys["gps:Altitude"].value == "5850/100"
    for token in body["undo_tokens"]:
        assert (await client.post("/undo", json={"token": token})).status_code == 200
    assert not any(k.startswith("gps:") for k in _keys(jpeg))


@pytest.mark.asyncio
async def test_the_position_follows_the_time_of_each_picture_along_a_gpx_track(client, tmp_path):
    path = _build(tmp_path, "jpeg_no_exif")
    await client.post("/write", json={"path": str(path), "fields": [
        {"key": "exif:DateTimeOriginal", "value": "2026-01-02T04:00:00"}]})
    gpx = tmp_path / "track.gpx"
    gpx.write_text(GPX)
    body = (await client.post("/geotag", json={"paths": [str(path)], "gpx": str(gpx), "preview": False,
                                               "max_gap_seconds": 7200})).json()
    assert body["ok"], body
    lat, lon = _keys(path)["gps:Latitude"].value, _keys(path)["gps:Longitude"].value
    assert lat[0] == "45/1" and lat[1] == "3/1"                       # half way: 45.05 degrees
    assert lon[0] == "9/1" and lon[1] == "6/1"                        # 9.1 degrees
    far = (await client.post("/geotag", json={"paths": [str(path)], "gpx": str(gpx), "utc_offset_minutes": -600,
                                              "max_gap_seconds": 60})).json()
    assert far["results"][0]["will_write"] == [] and "no track point" in far["results"][0]["reason"]


@pytest.mark.asyncio
async def test_geotag_refuses_bad_requests(client, tmp_path):
    path = _build(tmp_path, "jpeg_no_exif")
    assert (await client.post("/geotag", json={"paths": [str(path)]})).status_code == 422
    assert (await client.post("/geotag", json={"paths": [str(path)], "latitude": 95, "longitude": 0})).status_code == 422
    bad = tmp_path / "bad.gpx"
    bad.write_text('<!DOCTYPE x [<!ENTITY a "b">]><gpx/>')
    assert (await client.post("/geotag", json={"paths": [str(path)], "gpx": str(bad)})).status_code == 422
    empty = tmp_path / "empty.gpx"
    empty.write_text("<gpx/>")
    assert (await client.post("/geotag", json={"paths": [str(path)], "gpx": str(empty)})).status_code == 422
