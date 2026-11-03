"""Put a position on pictures: fixed coordinates, or the time of each picture looked up along a GPX track."""
from __future__ import annotations

import bisect
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from api import routes
from api.operations import _item, _paths
from api.routes import ApiCode, ApiError, PATH_LOCKS, stable_read
from core import values
from core.path_security import PathSecurityError, validate_file_path

router = APIRouter()
MAX_GPX_BYTES = 20 * 1024 * 1024


class GeotagRequest(BaseModel):
    paths: list[str] = Field(min_length=1)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    altitude: float | None = Field(default=None, ge=-1000, le=100000)
    gpx: str | None = None                       # a GPX track: the position is taken from the time of each picture
    utc_offset_minutes: int = Field(default=0, ge=-840, le=840)   # the camera clock minus UTC
    max_gap_seconds: int = Field(default=600, ge=1, le=86400)
    preview: bool = True


def _read_track(gpx_path: str) -> list[tuple[float, float, float, float | None]]:
    """``(epoch seconds, latitude, longitude, altitude)`` of the track points of a GPX file, in time order."""
    try:
        path = validate_file_path(gpx_path, must_exist=True)
    except PathSecurityError as exc:
        raise routes._path_error(exc) from exc
    if path.stat().st_size > MAX_GPX_BYTES:
        raise ApiError.one(422, ApiCode.INVALID_REQUEST, "the GPX file is too large")
    raw = path.read_bytes()
    if b"<!DOCTYPE" in raw[:4096].upper() or b"<!ENTITY" in raw.upper():
        raise ApiError.one(422, ApiCode.INVALID_REQUEST, "the GPX file declares a DTD")
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise ApiError.one(422, ApiCode.INVALID_REQUEST, f"the GPX file is not valid XML ({exc})") from exc
    points = []
    for node in root.iter():
        if node.tag.rsplit("}", 1)[-1] != "trkpt":
            continue
        time_node = next((c for c in node if c.tag.rsplit("}", 1)[-1] == "time"), None)
        ele_node = next((c for c in node if c.tag.rsplit("}", 1)[-1] == "ele"), None)
        try:
            stamp = datetime.fromisoformat((time_node.text or "").strip().replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            points.append((stamp.timestamp(), float(node.get("lat")), float(node.get("lon")),
                           float(ele_node.text) if ele_node is not None and ele_node.text else None))
        except (AttributeError, TypeError, ValueError):
            continue
    if not points:
        raise ApiError.one(422, ApiCode.INVALID_REQUEST, "the GPX file has no track points with a time")
    return sorted(points)


def _position_at(track, when: float, max_gap: int):
    """Position at ``when``: interpolated between two track points, or the nearest within ``max_gap`` seconds."""
    times = [p[0] for p in track]
    i = bisect.bisect_left(times, when)
    before = track[i - 1] if i > 0 else None
    after = track[i] if i < len(track) else None
    if before and after and after[0] > before[0] and after[0] - before[0] <= 2 * max_gap and before[0] <= when <= after[0]:
        f = (when - before[0]) / (after[0] - before[0])
        alt = None if before[3] is None or after[3] is None else before[3] + f * (after[3] - before[3])
        return before[1] + f * (after[1] - before[1]), before[2] + f * (after[2] - before[2]), alt
    near = min((p for p in (before, after) if p), key=lambda p: abs(p[0] - when), default=None)
    if near is not None and abs(near[0] - when) <= max_gap:
        return near[1], near[2], near[3]
    return None


def _dms(value: float) -> list[str]:
    value = abs(value)
    degrees = int(value)
    minutes = int((value - degrees) * 60)
    seconds = round((value - degrees - minutes / 60) * 3600 * 10000)
    if seconds >= 600000:                        # rounding reached a whole minute
        seconds, minutes = 0, minutes + 1
    if minutes >= 60:
        minutes, degrees = 0, degrees + 1
    return [f"{degrees}/1", f"{minutes}/1", f"{seconds}/10000"]


def _gps_writes(latitude: float, longitude: float, altitude: float | None) -> list[dict]:
    writes = [{"key": "gps:LatitudeRef", "value": "N" if latitude >= 0 else "S"},
              {"key": "gps:Latitude", "value": _dms(latitude)},
              {"key": "gps:LongitudeRef", "value": "E" if longitude >= 0 else "W"},
              {"key": "gps:Longitude", "value": _dms(longitude)}]
    if altitude is not None:
        writes += [{"key": "gps:AltitudeRef", "value": 0 if altitude >= 0 else 1},
                   {"key": "gps:Altitude", "value": f"{round(abs(altitude) * 100)}/100"}]
    return writes


def _geotag_one(path: str, req: GeotagRequest, track) -> dict[str, Any]:
    fpath = routes._valid_file(path)
    with PATH_LOCKS.reading(fpath):
        ctx = stable_read(fpath, lambda: routes._read_full_record(fpath))
    latitude, longitude, altitude = req.latitude, req.longitude, req.altitude
    if track is not None:
        shot = next((f for f in ctx.record.fields if f.key == "exif:DateTimeOriginal" and f.value), None)
        if shot is None:
            return {"position": None, "will_write": [], "reason": "the picture has no capture time"}
        when = values.parse_localdatetime(shot.value).replace(tzinfo=timezone.utc).timestamp() \
            - req.utc_offset_minutes * 60
        found = _position_at(track, when, req.max_gap_seconds)
        if found is None:
            return {"position": None, "will_write": [], "reason": "no track point near the time of the picture"}
        latitude, longitude, altitude = found
    writes = _gps_writes(latitude, longitude, altitude)
    handler = ctx.handler_of(routes.FORMAT)
    usable = {f.key for f in ctx.record.fields if f.editable} | {f.key for f in handler.writable_fields(fpath, ctx.record)} \
        | {f.key for f in ctx.record.addable_fields}
    if any(w["key"] not in usable for w in writes):
        return {"position": [latitude, longitude, altitude], "will_write": [],
                "reason": "this kind of file cannot take a position"}
    out: dict[str, Any] = {"position": [round(latitude, 6), round(longitude, 6), altitude],
                           "will_write": [w["key"] for w in writes], "undo_token": None}
    if not req.preview:
        saved = routes._save(path, [routes.WriteField(key=w["key"], value=w["value"]) for w in writes], [])
        out.update(undo_token=saved["undo_token"], undo_unavailable=saved["undo_unavailable"],
                   warnings=saved["warnings"])
    return out


@router.post("/geotag")
def geotag(req: GeotagRequest):
    track = None
    if req.gpx:
        track = _read_track(req.gpx)
    elif req.latitude is None or req.longitude is None:
        raise ApiError.one(422, ApiCode.INVALID_REQUEST, "give a latitude and a longitude, or a GPX file")
    results = [_item(p, lambda p=p: _geotag_one(p, req, track)) for p in _paths(req.paths)]
    return {"ok": all(r["ok"] for r in results), "preview": req.preview, "results": results,
            "undo_tokens": [r["undo_token"] for r in results if r.get("undo_token")]}
