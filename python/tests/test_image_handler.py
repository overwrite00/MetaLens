"""Phase F2A: images (EXIF codec, JPEG segment writer, XMP and IPTC read/write).

Samples come from ``tests/factories/image.py`` (also run by ``test_contract``).
"""
from __future__ import annotations

import hashlib
import importlib
import io
import json
import random
import struct
import zlib
from pathlib import Path

import pytest

from core import exif_codec, iptc, jpeg_segments, xmp
from core.base_handler import ABSENT
from core.errors import ErrorCode, MetadataValueError
from core.handlers.image_handler import IPTC_XMP_LINKS, ImageHandler
from core.models import MetadataField
from core.path_security import secure_atomic_write
from tests.factories import image as F

importlib.import_module("core.handlers")


# ──────────────────────── helpers ────────────────────────────────────────────

def _make(name: str, tmp_path: Path) -> Path:
    return next(s for s in F.SAMPLES if s.name == name).make(tmp_path / name)


def _fields(path: Path) -> dict[str, MetadataField]:
    return {f.key: f for f in ImageHandler().read(path).fields}


def _segments(path: Path) -> jpeg_segments.JpegFile:
    return jpeg_segments.parse(path.read_bytes())


def _pixels(path: Path) -> bytes:
    from PIL import Image
    with Image.open(path) as img:
        return img.convert("RGB").tobytes()


def _write(path: Path, **values) -> None:
    ImageHandler().write(path, [MetadataField(k.replace("__", ":"), k, v) for k, v in values.items()])


def _txn(path: Path, fn):
    out = []
    secure_atomic_write(path, lambda tmp: out.append(fn(tmp)))
    return out[0]


def _untouched_identical(before: jpeg_segments.JpegFile, after: jpeg_segments.JpegFile, touched) -> None:
    """Every segment not matching ``touched`` is byte-identical and in the same order."""
    b = [s.raw for s in before.segments if not touched(s)]
    a = [s.raw for s in after.segments if not touched(s)]
    assert a == b
    assert after.tail == before.tail
    assert after.soi == before.soi


# ──────────────────────── N1: camera JPEG / TIFF DPI ─────────────────────────

def test_camera_jpeg_reads_without_errors_and_dpi_is_float_pair(tmp_path):
    path = _make("jpeg_camera", tmp_path)
    record = ImageHandler().read(path)
    assert record.read_errors == []
    assert record.format_supports_write
    dpi = {f.key: f for f in record.fields}["img:dpi"]
    assert dpi.value == [72.0, 72.0] and dpi.length == 2 and dpi.field_type == "float"
    assert not dpi.editable and not dpi.deletable


@pytest.mark.asyncio
async def test_camera_jpeg_api_write_succeeds(client, tmp_path):
    path = _make("jpeg_camera", tmp_path)
    resp = await client.post("/write", json={"path": str(path),
                                              "fields": [{"key": "exif:Make", "value": "Nikon"}]})
    assert resp.status_code == 200, resp.text
    assert _fields(path)["exif:Make"].value == "Nikon"
    # Second write on the file MetaLens just wrote (N1 hit every JPEG after one write).
    resp = await client.post("/write", json={"path": str(path),
                                              "fields": [{"key": "exif:Model", "value": "Z"}]})
    assert resp.status_code == 200, resp.text


def test_tiff_with_dpi_reads_without_errors(tmp_path):
    path = _make("tiff_dpi", tmp_path)
    record = ImageHandler().read(path)
    assert record.read_errors == []
    fields = {f.key: f for f in record.fields}
    assert fields["img:dpi"].value == [300.0, 300.0]
    assert fields["exif:Make"].value == "ScannerCo"
    assert record.format_supports_write and fields["exif:Make"].editable      # TIFF metadata is editable
    assert not fields["exif:ImageWidth"].editable if "exif:ImageWidth" in fields else True


# ──────────────────────── N2: segments preserved byte for byte ───────────────

def test_write_keeps_app0_and_every_other_segment_identical(tmp_path):
    path = _make("jpeg_jfif_exif", tmp_path)
    before, pixels = _segments(path), _pixels(path)
    assert before.segments[0].is_jfif and before.segments[1].is_exif
    _write(path, exif__Make="Changed")
    after = _segments(path)
    assert after.segments[0].raw == before.segments[0].raw          # APP0 JFIF kept (N2)
    assert [s.marker for s in after.segments] == [s.marker for s in before.segments]
    _untouched_identical(before, after, lambda s: s.is_exif)
    assert _pixels(path) == pixels


def test_iptc_jpeg_write_preserves_com_app15_icc_and_scan(tmp_path):
    path = _make("jpeg_iptc", tmp_path)
    before, pixels = _segments(path), _pixels(path)
    _write(path, exif__Artist="Ann", iptc__City="Roma", **{"xmp__dc__title": "T"})
    after = _segments(path)
    _untouched_identical(before, after, lambda s: s.is_exif or s.is_photoshop or s.is_xmp)
    assert len(after.exif_indexes()) == len(after.photoshop_indexes()) == len(after.xmp_indexes()) == 1
    assert _pixels(path) == pixels


# ──────────────────────── N3: duplicate EXIF and GPS removal ─────────────────

def _xmp_first_with_duplicate_exif(tmp_path: Path) -> Path:
    path = _make("jpeg_xmp_first", tmp_path)
    segs, tail = F.split_jpeg(path.read_bytes())
    duplicate = F.segment(0xE1, F.camera_exif(thumbnail=False))
    path.write_bytes(F.assemble(segs + [duplicate], tail))
    return path


GPS_ALTITUDE_BYTES = struct.pack(">LL", 12345, 100)


def test_delete_gps_removes_duplicate_exif_and_every_old_gps_byte(tmp_path):
    path = _xmp_first_with_duplicate_exif(tmp_path)
    record = ImageHandler().read(path)
    assert any("2 EXIF segments" in w for w in record.warnings)
    before = _segments(path)
    xmp_before = before.segments[before.xmp_indexes()[0]].raw
    assert path.read_bytes().count(GPS_ALTITUDE_BYTES) == 2
    gps_keys = [f.key for f in record.fields if f.key.startswith("gps:")]
    assert "gps:Latitude" in gps_keys
    ImageHandler().delete(path, gps_keys)
    after = _segments(path)
    assert len(after.exif_indexes()) == 1
    assert GPS_ALTITUDE_BYTES not in path.read_bytes()
    assert not any(k.startswith("gps:") for k in _fields(path))
    assert after.segments[after.xmp_indexes()[0]].raw == xmp_before   # XMP untouched
    assert after.xmp_indexes()[0] < after.exif_indexes()[0]            # order kept
    assert not any("EXIF segments" in w for w in ImageHandler().read(path).warnings)


def test_bulk_delete_then_restore_brings_gps_back(tmp_path):
    path = _make("jpeg_camera", tmp_path)
    handler = ImageHandler()
    keys = ["exif:Make", "gps:Latitude", "exif:SubjectArea", "exif:MakerNote"]
    before = _fields(path)

    def delete(tmp):
        snap = handler.snapshot(tmp, keys)
        handler.apply_delete(tmp, keys)
        return snap
    snap = _txn(path, delete)
    assert not set(keys) & set(_fields(path))
    _txn(path, lambda tmp: handler.restore(tmp, snap))
    assert handler.snapshot(path, keys) == snap
    after = _fields(path)
    for key in keys:
        assert after[key].value == before[key].value


# ──────────────────────── EXIF typed codecs ──────────────────────────────────

def test_subject_area_is_a_fixed_length_int_list(tmp_path):
    path = _make("jpeg_camera", tmp_path)
    f = _fields(path)["exif:SubjectArea"]
    assert (f.value, f.field_type, f.multi, f.length, f.editable) == ([10, 20], "int", True, 2, True)
    _write(path, exif__SubjectArea=[11, 22])
    assert _fields(path)["exif:SubjectArea"].value == [11, 22]
    with pytest.raises(MetadataValueError) as exc:
        _write(path, exif__SubjectArea=[1, 2, 3])
    assert exc.value.key == "exif:SubjectArea" and exc.value.code == ErrorCode.WRONG_LENGTH


def test_rationals_are_exact_and_not_reduced(tmp_path):
    path = _make("jpeg_camera", tmp_path)
    assert _fields(path)["exif:FocalLength"].value == "1234/100"
    _write(path, exif__XResolution="1234/100", exif__ExposureBiasValue="-2/6")
    fields = _fields(path)
    assert fields["exif:XResolution"].value == "1234/100"
    assert fields["exif:ExposureBiasValue"].value == "-2/6"
    import piexif
    assert piexif.load(str(path))["0th"][piexif.ImageIFD.XResolution] == (1234, 100)
    _write(path, exif__XResolution="12.5")
    assert _fields(path)["exif:XResolution"].value == "125/10"


def test_zero_over_zero_rational_round_trips(tmp_path):
    path = _make("jpeg_camera", tmp_path)
    _write(path, exif__FocalLength="0/0")
    assert _fields(path)["exif:FocalLength"].value == "0/0"


def test_negative_unsigned_rational_is_rejected(tmp_path):
    path = _make("jpeg_camera", tmp_path)
    with pytest.raises(MetadataValueError) as exc:
        _write(path, exif__XResolution="-1/2")
    assert exc.value.key == "exif:XResolution"


@pytest.mark.parametrize("value", ["garbage", "2026:01:02 03:04:05", "2026-01-02T03:04:05+01:00",
                                   "2026-01-02T03:04:05.5", None])
def test_invalid_exif_datetime_is_rejected(tmp_path, value):
    path = _make("jpeg_camera", tmp_path)
    original = path.read_bytes()
    with pytest.raises(MetadataValueError) as exc:
        _write(path, exif__DateTimeOriginal=value)
    assert exc.value.key == "exif:DateTimeOriginal"
    assert path.read_bytes() == original


def test_exif_datetime_is_localdatetime_and_round_trips(tmp_path):
    path = _make("jpeg_camera", tmp_path)
    f = _fields(path)["exif:DateTimeOriginal"]
    assert (f.field_type, f.value) == ("localdatetime", "2026-01-02T03:04:05")
    _write(path, exif__DateTimeOriginal="2030-12-31T23:59:58")
    import piexif
    assert piexif.load(str(path))["Exif"][piexif.ExifIFD.DateTimeOriginal] == b"2030:12:31 23:59:58"


def test_blank_exif_date_is_shown_read_only_but_deletable(tmp_path):
    f = _fields(_make("jpeg_camera", tmp_path))["exif:DateTimeDigitized"]
    assert f.field_type == "str" and not f.editable and f.deletable


def test_latin1_ascii_is_shown_without_replacement_and_undo_is_exact(tmp_path):
    """N12: 'Jos\\xe9' is shown as 'José' and Undo writes back the original byte."""
    path = _make("jpeg_camera", tmp_path)
    handler = ImageHandler()
    f = _fields(path)["exif:Artist"]
    assert f.value == "José" and "�" not in f.value

    def edit(tmp):
        snap = handler.snapshot(tmp, ["exif:Artist"])
        handler.apply_write(tmp, [MetadataField("exif:Artist", "Artist", "Zoë")])
        return snap
    snap = _txn(path, edit)
    import piexif
    assert piexif.load(str(path))["0th"][piexif.ImageIFD.Artist] == "Zoë".encode("utf-8")
    _txn(path, lambda tmp: handler.restore(tmp, snap))
    assert piexif.load(str(path))["0th"][piexif.ImageIFD.Artist] == b"Jos\xe9"
    assert _fields(path)["exif:Artist"].value == "José"


def test_makernote_is_binary_read_only_with_warning_and_restorable(tmp_path):
    path = _make("jpeg_camera", tmp_path)
    handler = ImageHandler()
    record = handler.read(path)
    f = {x.key: x for x in record.fields}["exif:MakerNote"]
    assert f.field_type == "binary" and not f.editable and f.deletable
    assert f.value["size"] == 40
    assert any("MakerNote" in w for w in record.warnings)
    with pytest.raises(MetadataValueError):
        handler.write(path, [MetadataField("exif:MakerNote", "MakerNote", "x")])
    snap = handler.snapshot(path, ["exif:MakerNote"])
    handler.delete(path, ["exif:MakerNote"])
    assert "exif:MakerNote" not in _fields(path)
    _txn(path, lambda tmp: handler.restore(tmp, snap))
    assert _fields(path)["exif:MakerNote"].value == f.value


def test_user_comment_and_xp_tags_are_text(tmp_path):
    path = _make("jpeg_camera", tmp_path)
    assert _fields(path)["exif:UserComment"].value == "hello camera"
    _write(path, exif__UserComment="Commento è 日本", exif__XPTitle="Titolo €")
    fields = _fields(path)
    assert fields["exif:UserComment"].value == "Commento è 日本"
    assert fields["exif:XPTitle"].value == "Titolo €"
    import piexif
    exif = piexif.load(str(path))
    assert exif["Exif"][piexif.ExifIFD.UserComment].startswith(b"UNICODE\x00")
    assert bytes(exif["0th"][piexif.ImageIFD.XPTitle]) == "Titolo €".encode("utf-16-le") + b"\x00\x00"


def test_enums_and_gps_refs(tmp_path):
    path = _make("jpeg_camera", tmp_path)
    fields = _fields(path)
    assert fields["exif:Orientation"].choices == [1, 2, 3, 4, 5, 6, 7, 8]
    assert fields["gps:LatitudeRef"].choices == ["N", "S"]
    _write(path, exif__Orientation="6", gps__LatitudeRef="S")
    fields = _fields(path)
    assert fields["exif:Orientation"].value == 6 and fields["gps:LatitudeRef"].value == "S"
    with pytest.raises(MetadataValueError):
        _write(path, gps__LatitudeRef="X")


def test_int_range_follows_the_tiff_type(tmp_path):
    path = _make("jpeg_camera", tmp_path)
    with pytest.raises(MetadataValueError) as exc:
        _write(path, exif__ISOSpeedRatings=70000)   # SHORT
    assert exc.value.code == ErrorCode.OUT_OF_RANGE
    with pytest.raises(MetadataValueError):
        _write(path, gps__VersionID=[256, 2, 0, 0])  # BYTE


def test_exif_text_larger_than_a_segment_is_422_with_key(tmp_path):
    path = _make("jpeg_camera", tmp_path)
    original = path.read_bytes()
    with pytest.raises(MetadataValueError) as exc:
        _write(path, exif__ImageDescription="x" * 70_000)
    assert exc.value.key == "exif:ImageDescription" and exc.value.code == ErrorCode.TOO_LARGE
    assert path.read_bytes() == original


@pytest.mark.asyncio
async def test_api_oversized_exif_returns_422_naming_the_key(client, tmp_path):
    path = _make("jpeg_camera", tmp_path)
    original = path.read_bytes()
    resp = await client.post("/write", json={"path": str(path), "fields": [
        {"key": "exif:Make", "value": "x" * 70_000}]})
    assert resp.status_code == 422, resp.text
    assert "exif:Make" in resp.json()["detail"]
    assert path.read_bytes() == original


def _tiff_with_unknown_tags() -> bytes:
    """Little-endian TIFF: IFD0 = Make, private tag 0xC000 (ASCII), 0xEA1C (UNDEFINED)."""
    entries = [(0x010F, 2, b"Acme\x00"), (0xC000, 2, b"private value\x00"),
               (0xEA1C, 7, b"\x1c\xea" + bytes(30))]
    count = len(entries)
    data_off = 8 + 2 + 12 * count + 4
    table, blob = b"", b""
    for tag, typ, val in entries:
        if len(val) <= 4:
            table += struct.pack("<HHL", tag, typ, len(val)) + val.ljust(4, b"\x00")
        else:
            table += struct.pack("<HHLL", tag, typ, len(val), data_off + len(blob))
            blob += val + (b"\x00" if len(val) % 2 else b"")
    return b"II*\x00" + struct.pack("<L", 8) + struct.pack("<H", count) + table + b"\x00" * 4 + blob


def test_unknown_exif_tags_survive_a_rewrite(tmp_path):
    """piexif.load drops tags missing from its table; the codec keeps them (G4)."""
    path = tmp_path / "private.jpg"
    path.write_bytes(F.jpeg_with([F.segment(0xE1, b"Exif\x00\x00" + _tiff_with_unknown_tags())]))
    fields = _fields(path)
    assert fields["exif:tag_49152"].value == "private value"
    assert fields["exif:tag_59932"].field_type == "binary"
    _write(path, exif__Make="Other")
    block = exif_codec.parse(_segments(path).segments[1].payload[6:])
    assert block.byteorder == "<"
    assert block.ifds["0th"][0xC000].data == b"private value\x00"
    assert block.ifds["0th"][0xEA1C].data == b"\x1c\xea" + bytes(30)
    assert _fields(path)["exif:tag_49152"].value == "private value"


def test_exif_keys_are_unique_and_parse_back():
    import piexif
    seen = set()
    for ifd, table in (("0th", "Image"), ("Exif", "Exif"), ("GPS", "GPS"), ("Interop", "Interop"),
                       ("1st", "Image")):
        for tag in [*piexif.TAGS[table], 0xC000, 0xFFFF]:
            key = exif_codec.key_for(ifd, tag)
            assert key not in seen, key
            seen.add(key)
            assert exif_codec.parse_key(key) == (ifd, tag), key
    assert exif_codec.key_for("Exif", 0x829A) == "exif:ExposureTime"
    assert exif_codec.key_for("0th", 0x829A) == "exif:IFD0:ExposureTime"
    assert exif_codec.key_for("GPS", 2) == "gps:Latitude"
    assert exif_codec.key_for("0th", 0x0212) == "exif:YCbCrSubSampling"
    assert exif_codec.parse_key("exif:IFD0:Make") is None   # canonical form is exif:Make


def test_ycbcr_subsampling_is_named_and_read_only(tmp_path):
    import piexif
    path = tmp_path / "sub.jpg"
    exif = piexif.dump({"0th": {piexif.ImageIFD.YCbCrSubSampling: (2, 1)}})
    path.write_bytes(F.pillow_jpeg(exif=exif))
    f = _fields(path)["exif:YCbCrSubSampling"]
    assert (f.value, f.length, f.editable) == ([2, 1], 2, False)


def test_legacy_encoder_still_validates():
    from core.handlers.image_handler import _encode_exif_value
    assert _encode_exif_value("120/1", 5) == struct.pack(">LL", 120, 1)
    with pytest.raises(ValueError):
        _encode_exif_value("1/0", 5)


# ──────────────────────── read-only formats ──────────────────────────────────

def test_png_webp_expose_exif_and_gif_comment(tmp_path):
    png = _fields(_make("png_text_exif", tmp_path))
    assert png["exif:Make"].value == "Canon"
    assert png["png:Title"].value == "PNG title"
    assert png["png:Description@it"].value == "Descrizione è 日本"
    assert png["png:Comment"].value.startswith("compressed comment")
    webp = _fields(_make("webp_exif", tmp_path))
    assert webp["exif:Model"].value == "EOS Test"
    assert webp["xmp:dc:subject"].value == ["alpha", "beta ß"]
    gif = _fields(_make("gif_comment", tmp_path))
    assert gif["gif:comment"].value == "GIF comment è"
    assert all(not f.editable and not f.deletable for f in gif.values())
    # PNG and WebP metadata is editable since 0.5.0: the EXIF and XMP blocks, and PNG text can be removed.
    assert png["exif:Make"].editable and png["png:Title"].deletable and not png["png:Title"].editable
    assert webp["exif:Model"].editable and webp["xmp:dc:subject"].editable


# ──────────────────────── XMP ────────────────────────────────────────────────

def test_xmp_fields_are_read_with_namespaces(tmp_path):
    fields = _fields(_make("jpeg_xmp_first", tmp_path))
    assert fields["xmp:dc:title"].value == "Titolo è"
    assert fields["xmp:dc:subject"].value == ["alpha", "beta ß"] and fields["xmp:dc:subject"].multi
    assert fields["xmp:dc:creator"].value == ["Ada"]
    assert fields["xmp:xmp:CreatorTool"].value == "Tool & Co"
    assert fields["xmp:xmp:Rating"].value == 3 and fields["xmp:xmp:Rating"].field_type == "int"
    assert fields["xmp:photoshop:City"].value == "Zürich"
    assert fields["xmp:xmp:CreateDate"].value == "2026-01-02T03:04:05+02:00"
    unmapped = fields["xmp:exif:GPSLatitude"]
    assert not unmapped.editable and unmapped.deletable
    assert all(f.source == "xmp" for k, f in fields.items() if k.startswith("xmp:"))


def _xmp_packet(path: Path) -> xmp.XmpPacket:
    jpeg = _segments(path)
    return xmp.XmpPacket(jpeg.segments[jpeg.xmp_indexes()[0]].payload[len(jpeg_segments.XMP_HEADER):])


def test_xmp_write_keeps_everything_else(tmp_path):
    path = _make("jpeg_xmp_first", tmp_path)
    before, pixels = _segments(path), _pixels(path)
    _write(path, **{"xmp__dc__subject": ["uno", "due è", "三"], "xmp__dc__title": "Nuovo € 日本",
                    "xmp__xmp__Rating": 5, "xmp__photoshop__Headline": "Headline"})
    fields = _fields(path)
    assert fields["xmp:dc:subject"].value == ["uno", "due è", "三"]
    assert fields["xmp:dc:title"].value == "Nuovo € 日本"
    assert fields["xmp:xmp:Rating"].value == 5
    assert fields["xmp:photoshop:Headline"].value == "Headline"
    assert fields["xmp:dc:creator"].value == ["Ada"]
    text = _xmp_packet(path).to_bytes().decode("utf-8")
    assert '<rdf:li xml:lang="de">Titel</rdf:li>' in text                 # other language kept
    assert "<!-- preserved comment -->" in text
    assert "<stEvt:action>created</stEvt:action>" in text                 # struct untouched
    assert text.startswith('<?xpacket begin="﻿"') and text.endswith('<?xpacket end="w"?>')
    after = _segments(path)
    _untouched_identical(before, after, lambda s: s.is_xmp)
    assert _pixels(path) == pixels


def test_xmp_untouched_packet_serializes_equivalently():
    packet = xmp.XmpPacket(F.XMP_PACKET.encode("utf-8"))
    again = xmp.XmpPacket(packet.to_bytes())
    assert again.to_bytes() == packet.to_bytes()
    assert [(p.uri, p.local) for p in again.properties()] == [(p.uri, p.local) for p in packet.properties()]


def test_xmp_undo_is_exact(tmp_path):
    path = _make("jpeg_xmp_first", tmp_path)
    handler = ImageHandler()
    keys = ["xmp:dc:subject", "xmp:xmp:CreatorTool", "xmp:dc:description"]
    original = _xmp_packet(path).to_bytes()

    def edit(tmp):
        snap = handler.snapshot(tmp, keys)
        handler.apply_write(tmp, [MetadataField("xmp:dc:subject", "", ["x"]),
                                  MetadataField("xmp:xmp:CreatorTool", "", "New"),
                                  MetadataField("xmp:dc:description", "", "Added")])
        return snap
    snap = _txn(path, edit)
    assert snap["xmp:dc:description"] is ABSENT
    _txn(path, lambda tmp: handler.restore(tmp, snap))
    assert handler.snapshot(path, keys) == snap
    assert _xmp_packet(path).to_bytes() == original


def test_xmp_added_to_jpeg_without_xmp(tmp_path):
    path = _make("jpeg_no_exif", tmp_path)
    before = _segments(path)
    _write(path, **{"xmp__dc__creator": ["A", "B"], "xmp__xmp__CreateDate": "2026-03-04T05:06:07Z"})
    fields = _fields(path)
    assert fields["xmp:dc:creator"].value == ["A", "B"]
    assert fields["xmp:xmp:CreateDate"].value == "2026-03-04T05:06:07+00:00"
    assert fields["xmp:xmp:CreateDate"].field_type == "datetime"
    after = _segments(path)
    assert after.segments[0].raw == before.segments[0].raw and after.segments[1].is_xmp
    _untouched_identical(before, after, lambda s: s.is_xmp)


@pytest.mark.parametrize("key,value", [("xmp:xmp:CreateDate", "yesterday"), ("xmp:xmp:Rating", 9),
                                       ("xmp:dc:title", "bad \x01 char"), ("xmp:dc:subject", []),
                                       ("xmp:dc:title", None)])
def test_xmp_invalid_values_are_rejected(tmp_path, key, value):
    path = _make("jpeg_xmp_first", tmp_path)
    original = path.read_bytes()
    with pytest.raises(MetadataValueError) as exc:
        ImageHandler().write(path, [MetadataField(key, key, value)])
    assert exc.value.key == key
    assert path.read_bytes() == original


def test_xmp_too_large_for_a_segment_is_rejected(tmp_path):
    path = _make("jpeg_xmp_first", tmp_path)
    with pytest.raises(MetadataValueError) as exc:
        _write(path, **{"xmp__dc__description": "y" * 70_000})
    assert exc.value.key == "xmp:dc:description" and exc.value.code == ErrorCode.TOO_LARGE


def test_extended_xmp_is_read_only(tmp_path):
    path = _make("jpeg_xmp_first", tmp_path)
    segs, tail = F.split_jpeg(path.read_bytes())
    ext = F.segment(0xE1, jpeg_segments.XMP_EXT_HEADER + bytes(40))
    path.write_bytes(F.assemble(segs[:2] + [ext] + segs[2:], tail))
    record = ImageHandler().read(path)
    fields = {f.key: f for f in record.fields}
    assert not fields["xmp:dc:title"].editable and not fields["xmp:dc:title"].deletable
    assert fields["exif:Make"].editable
    assert any("Extended XMP" in w for w in record.warnings)


def test_xmp_with_doctype_is_rejected_and_preserved(tmp_path):
    packet = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><x:xmpmeta xmlns:x="adobe:ns:meta/"/>'
    path = tmp_path / "evil.jpg"
    path.write_bytes(F.jpeg_with([F.segment(0xE1, jpeg_segments.XMP_HEADER + packet)]))
    record = ImageHandler().read(path)
    assert record.read_errors == [] and any("XMP not readable" in w for w in record.warnings)
    _write(path, exif__Make="ok")
    jpeg = _segments(path)
    assert jpeg.segments[jpeg.xmp_indexes()[0]].payload == jpeg_segments.XMP_HEADER + packet


# ──────────────────────── IPTC ───────────────────────────────────────────────

def _iim(path: Path) -> list[iptc.Dataset]:
    jpeg = _segments(path)
    resources, _ = iptc.parse_resources(jpeg.segments[jpeg.photoshop_indexes()[0]].payload[14:])
    res = next(r for r in resources if r.rid == iptc.IPTC_RESOURCE)
    return iptc.parse_iim(res.data)[0]


def test_iptc_fields_are_read_multivalued_and_utf8(tmp_path):
    fields = _fields(_make("jpeg_iptc", tmp_path))
    assert fields["iptc:ObjectName"].value == "Oggetto è"
    kw = fields["iptc:Keywords"]
    assert kw.value == ["one", "due è", "三"] and kw.multi and kw.editable
    assert fields["iptc:By-line"].value == ["Ada Lovelace"]
    assert fields["iptc:Caption-Abstract"].field_type == "text"
    assert fields["iptc:DateCreated"].value == "20260102"
    assert not fields["iptc:2:135"].editable and fields["iptc:2:135"].deletable
    assert not fields["iptc:ApplicationRecordVersion"].editable
    assert "iptc:1:90" not in fields


def _digest_and_iim(path: Path):
    jpeg = _segments(path)
    resources, _ = iptc.parse_resources(jpeg.segments[jpeg.photoshop_indexes()[0]].payload[14:])
    by_id = {r.rid: r for r in resources}
    return by_id[0x0425].data, by_id[0x0404].data


def test_iptc_edit_keeps_the_old_digest_when_an_xmp_packet_is_not_updated(tmp_path):
    """MWG: a digest equal to the IIM's MD5 tells readers the XMP is in sync and preferred. Writing the
    IIM without updating the XMP must therefore NOT refresh the digest (a mismatch makes the IIM win)."""
    path = _make("jpeg_iptc", tmp_path)
    jpeg = _segments(path)
    jpeg.set_xmp(F.XMP_PACKET.encode("utf-8"))
    path.write_bytes(jpeg.to_bytes())
    digest_before, iim_before = _digest_and_iim(path)
    xmp_before = [s.raw for s in _segments(path).segments if s.is_xmp]
    _write(path, iptc__Keywords=["only", "in", "iptc"])
    digest_after, iim_after = _digest_and_iim(path)
    assert iim_after != iim_before
    assert digest_after == digest_before != hashlib.md5(iim_after).digest()
    assert [s.raw for s in _segments(path).segments if s.is_xmp] == xmp_before
    assert _fields(path)["iptc:Keywords"].value == ["only", "in", "iptc"]


def test_iptc_edit_refreshes_the_digest_when_the_xmp_is_written_too(tmp_path):
    path = _make("jpeg_iptc", tmp_path)
    jpeg = _segments(path)
    jpeg.set_xmp(F.XMP_PACKET.encode("utf-8"))
    path.write_bytes(jpeg.to_bytes())
    # Every mapped pair has to agree for the XMP to count as in sync: bring the others in line too.
    fields = _fields(path)
    sync = {"iptc__Keywords": ["a", "b"], "xmp__dc__subject": ["a", "b"]}
    for iptc_key, xmp_key in IPTC_XMP_LINKS.items():
        if iptc_key in fields and xmp_key in fields and iptc_key != "iptc:Keywords":
            sync[iptc_key.replace(":", "__")] = fields[iptc_key].value
            sync[xmp_key.replace(":", "__")] = fields[iptc_key].value
    _write(path, **sync)
    digest, iim = _digest_and_iim(path)
    assert digest == hashlib.md5(iim).digest()


def test_iptc_edit_keeps_the_digest_when_another_mapped_pair_differs(tmp_path):
    """B4: only the keywords are written to both sides; a differing title twin means the XMP is
    not in sync, so the digest is not refreshed."""
    path = _make("jpeg_iptc", tmp_path)
    jpeg = _segments(path)
    jpeg.set_xmp(F.XMP_PACKET.encode("utf-8"))
    path.write_bytes(jpeg.to_bytes())
    fields = _fields(path)
    assert fields["iptc:ObjectName"].value != fields["xmp:dc:title"].value
    digest_before, _ = _digest_and_iim(path)
    _write(path, iptc__Keywords=["a", "b"], xmp__dc__subject=["a", "b"])
    digest, iim = _digest_and_iim(path)
    assert digest == digest_before != hashlib.md5(iim).digest()


def test_snapshot_equality_ignores_positions_so_unrelated_external_edits_do_not_conflict(tmp_path):
    """The Undo journal compares snapshots to detect a conflicting change of the touched keys. Where a
    value sits (IPTC dataset index, XMP description/attribute/element position) is a restore hint, not
    part of the value: removing or inserting a NEIGHBOUR must not make the snapshots differ."""
    import copy
    import pickle
    handler = ImageHandler()
    path = _make("jpeg_iptc", tmp_path)
    jpeg = _segments(path)
    jpeg.set_xmp(F.XMP_PACKET.encode("utf-8"))
    path.write_bytes(jpeg.to_bytes())
    keys = ["iptc:Keywords", "xmp:xmp:Rating", "xmp:dc:subject"]
    first = _txn(path, lambda tmp: handler.snapshot(tmp, keys))
    assert all(raw is not ABSENT for raw in first.values())
    # Neighbours change: a dataset inserted BEFORE the keywords, an XMP attribute removed BEFORE the rating.
    _write(path, iptc__Category="Inserted before the keywords")
    handler.delete(path, ["xmp:xmp:CreatorTool"])
    second = _txn(path, lambda tmp: handler.snapshot(tmp, keys))
    assert second == first
    assert all(not (second[k] != first[k]) for k in keys)
    # ... yet the positions are really carried (restore uses them), also through copy/pickle.
    for clone in (copy.deepcopy(first), pickle.loads(pickle.dumps(first))):
        assert clone == first and clone["iptc:Keywords"].raw[1] == first["iptc:Keywords"].raw[1]
    assert second["iptc:Keywords"].raw[1] != first["iptc:Keywords"].raw[1]
    # A genuine change of the value is still a difference.
    _write(path, iptc__Keywords=["changed"])
    assert _txn(path, lambda tmp: handler.snapshot(tmp, keys)) != first


def test_iptc_write_preserves_other_resources_and_datasets(tmp_path):
    path = _make("jpeg_iptc", tmp_path)
    before_iim = _iim(path)
    before, pixels = _segments(path), _pixels(path)
    _write(path, iptc__Keywords=["k1", "k2 ü", "k3"], iptc__Headline="Titolo €")
    fields = _fields(path)
    assert fields["iptc:Keywords"].value == ["k1", "k2 ü", "k3"]
    assert fields["iptc:Headline"].value == "Titolo €"
    after_iim = _iim(path)
    untouched = lambda ds: [d for d in ds if d.number not in (25, 105)]  # noqa: E731
    assert untouched(after_iim) == untouched(before_iim)
    numbers = [(d.record, d.number) for d in after_iim]
    assert numbers == sorted(numbers)                                     # IIM order kept
    jpeg = _segments(path)
    resources, _ = iptc.parse_resources(jpeg.segments[jpeg.photoshop_indexes()[0]].payload[14:])
    by_id = {r.rid: r for r in resources}
    assert [r.rid for r in resources] == [0x03ED, 0x0404, 0x0425]
    assert by_id[0x03ED].data == F.RESOLUTION_INFO
    assert by_id[0x0425].data == hashlib.md5(by_id[0x0404].data).digest()   # digest refreshed
    _untouched_identical(before, jpeg, lambda s: s.is_photoshop)
    assert _pixels(path) == pixels


def test_iptc_undo_is_exact(tmp_path):
    path = _make("jpeg_iptc", tmp_path)
    handler = ImageHandler()
    keys = ["iptc:Keywords", "iptc:ObjectName", "iptc:City", "iptc:2:135"]
    original = _iim(path)

    def edit(tmp):
        snap = handler.snapshot(tmp, keys)
        handler.apply_write(tmp, [MetadataField("iptc:Keywords", "", ["solo"]),
                                  MetadataField("iptc:City", "", "Milano")])
        handler.apply_delete(tmp, ["iptc:ObjectName", "iptc:2:135"])
        return snap
    snap = _txn(path, edit)
    assert snap["iptc:City"] is ABSENT
    assert [d.data for d in _iim(path) if d.number == 25] == [b"solo"]
    _txn(path, lambda tmp: handler.restore(tmp, snap))
    assert _iim(path) == original
    assert handler.snapshot(path, keys) == snap


def test_iptc_added_without_charset_marker_gets_utf8_marker(tmp_path):
    path = tmp_path / "legacy.jpg"
    datasets = [(2, 0, b"\x00\x04"), (2, 5, b"ascii name")]
    path.write_bytes(F.jpeg_with([F.iptc_app13(datasets)]))
    _write(path, iptc__Keywords=["ä"])
    iim = _iim(path)
    assert iim[0] == iptc.Dataset(1, 90, b"\x1b%G")
    assert _fields(path)["iptc:Keywords"].value == ["ä"]
    assert _fields(path)["iptc:ObjectName"].value == "ascii name"


def test_iptc_legacy_charset_is_read_only_and_not_mangled(tmp_path):
    path = tmp_path / "latin1.jpg"
    path.write_bytes(F.jpeg_with([F.iptc_app13([(2, 5, b"Caf\xe9")])]))
    record = ImageHandler().read(path)
    f = {x.key: x for x in record.fields}["iptc:ObjectName"]
    assert f.value == "Café" and not f.editable and not f.deletable
    assert any("legacy character set" in w for w in record.warnings)
    assert record.read_errors == [] and record.format_supports_write
    original = path.read_bytes()
    with pytest.raises(MetadataValueError):
        _write(path, iptc__ObjectName="Cafe")
    assert path.read_bytes() == original


def test_iptc_created_from_scratch_and_removed_again(tmp_path):
    path = _make("jpeg_no_exif", tmp_path)
    original = path.read_bytes()
    handler = ImageHandler()

    def add(tmp):
        snap = handler.snapshot(tmp, ["iptc:Keywords"])
        handler.apply_write(tmp, [MetadataField("iptc:Keywords", "", ["a", "b"])])
        return snap
    snap = _txn(path, add)
    assert _fields(path)["iptc:Keywords"].value == ["a", "b"]
    _txn(path, lambda tmp: handler.restore(tmp, snap))
    assert path.read_bytes() == original           # empty IIM: APP13 removed again


def test_iptc_invalid_values(tmp_path):
    path = _make("jpeg_iptc", tmp_path)
    for key, value in [("iptc:DateCreated", "2026-01-02"), ("iptc:Keywords", "not a list"),
                       ("iptc:Headline", "x" * 40_000), ("iptc:ObjectName", None)]:
        with pytest.raises(MetadataValueError) as exc:
            ImageHandler().write(path, [MetadataField(key, key, value)])
        assert exc.value.key == key


# ──────────────────────── JPEG segment writer (unit) ─────────────────────────

def _synthetic_jpeg() -> bytes:
    """SOI, fill bytes, APP0, TEM, COM, APP1 XMP, APP1 Exif, APP1 Exif (dup), APP15, DQT, SOS..."""
    parts = [
        b"\xff\xd8",
        b"\xff\xff\xff" + F.segment(0xE0, b"JFIF\x00\x01\x02\x00\x00\x01\x00\x01\x00\x00")[1:],
        b"\xff\x01",                                          # TEM: no length
        F.segment(0xFE, b"comment"),
        F.segment(0xE1, jpeg_segments.XMP_HEADER + b"<x/>"),
        F.segment(0xE1, b"Exif\x00\x00old1"),
        F.segment(0xE1, b"Exif\x00\x00old2-with-gps"),
        F.segment(0xEF, b"APP15"),
        F.segment(0xDB, bytes(65)),
    ]
    tail = b"\xff\xda" + struct.pack(">H", 8) + bytes(6) + b"\x12\xff\x00\x34\xff\xd0\x56\xff\xd9TRAILER"
    return b"".join(parts) + tail, tail


def test_segment_parser_handles_fill_bytes_markers_without_length_and_tail():
    data, tail = _synthetic_jpeg()
    jpeg = jpeg_segments.parse(data)
    assert jpeg.to_bytes() == data
    assert [s.marker for s in jpeg.segments] == [0xE0, 0x01, 0xFE, 0xE1, 0xE1, 0xE1, 0xEF, 0xDB]
    assert jpeg.segments[0].raw.startswith(b"\xff\xff\xff\xe0")
    assert jpeg.segments[1].payload is None
    assert jpeg.tail == tail


def test_set_exif_replaces_first_in_place_and_drops_duplicates():
    data, tail = _synthetic_jpeg()
    jpeg = jpeg_segments.parse(data)
    others = [s.raw for s in jpeg.segments if not s.is_exif]
    assert jpeg.set_exif(b"NEW") == 1
    assert [s.raw for s in jpeg.segments if not s.is_exif] == others
    exif = jpeg.exif_indexes()
    assert exif == [4] and jpeg.segments[4].payload == b"Exif\x00\x00NEW"
    out = jpeg.to_bytes()
    assert b"old2-with-gps" not in out and out.endswith(tail)


def test_set_exif_inserts_after_jfif_or_soi():
    jpeg = jpeg_segments.parse(F.pillow_jpeg())
    assert jpeg.segments[0].is_jfif
    jpeg.set_exif(b"T")
    assert jpeg.segments[1].is_exif
    no_app0 = jpeg_segments.parse(F.jpeg_with([], keep_app0=False))
    first = no_app0.segments[0].raw
    no_app0.set_exif(b"T")
    assert no_app0.segments[0].is_exif and no_app0.segments[1].raw == first


def test_segment_payload_limit():
    jpeg = jpeg_segments.parse(F.pillow_jpeg())
    jpeg.set_exif(bytes(jpeg_segments.MAX_PAYLOAD - 6))      # exactly fits
    with pytest.raises(MetadataValueError) as exc:
        jpeg.set_exif(bytes(jpeg_segments.MAX_PAYLOAD - 5), key="exif:Make")
    assert exc.value.code == ErrorCode.TOO_LARGE and exc.value.key == "exif:Make"


@pytest.mark.parametrize("data", [b"\xff\xd8\xff\xe1\x00", b"\xff\xd8\xff\xe1\x00\x01xx",
                                  b"\xff\xd8\x00\x00", b"\xff\xd8\xff\xe0\x00\x10abc", b"GIF89a"])
def test_segment_parser_rejects_malformed_headers(data):
    with pytest.raises(jpeg_segments.JpegFormatError):
        jpeg_segments.parse(data)


def test_corrupt_jpeg_is_a_read_error_and_not_writable(tmp_path):
    path = tmp_path / "broken.jpg"
    path.write_bytes(b"\xff\xd8\xff\xe1\x00\x05Ex")
    record = ImageHandler().read(path)
    assert record.read_errors
    assert not any(f.editable or f.deletable for f in record.fields)


def test_progressive_jpeg_scan_data_is_untouched(tmp_path):
    path = tmp_path / "progressive.jpg"
    path.write_bytes(F.pillow_jpeg(size=(32, 32), progressive=True))
    tail = _segments(path).tail
    pixels = _pixels(path)
    _write(path, exif__Make="P", iptc__Keywords=["p"], **{"xmp__dc__title": "p"})
    assert _segments(path).tail == tail and _pixels(path) == pixels


# ──────────────────────── IPTC / 8BIM codec (unit) ───────────────────────────

def test_iim_extended_length_and_resource_padding_round_trip():
    big = iptc.Dataset(2, 202, bytes(40_000))
    data = iptc.serialize_iim([iptc.Dataset(2, 5, b"a"), big])
    assert iptc.parse_iim(data)[0] == [iptc.Dataset(2, 5, b"a"), big]
    raw = F.resource(0x0404, b"odd", name=b"nm") + F.resource(0x0400, b"ev")
    resources, trailer = iptc.parse_resources(raw)
    assert [r.raw for r in resources] == [F.resource(0x0404, b"odd", name=b"nm"), F.resource(0x0400, b"ev")]
    assert iptc.serialize_resources(resources, trailer) == raw
    assert resources[0].name == b"nm" and resources[0].data == b"odd"


def test_writable_field_schema_for_absent_keys(tmp_path):
    path = _make("jpeg_no_exif", tmp_path)
    f = ImageHandler().writable_field(path, "exif:Make")
    assert f is not None and f.editable and f.field_type == "str"
    assert ImageHandler().writable_field(path, "exif:MakerNote") is None
    assert ImageHandler().writable_field(_make("png_text_exif", tmp_path), "exif:Make") is None


# ════════════════════════ independent review fixes ═══════════════════════════
# Regression tests for the problems found by the independent review of the
# image part (MPF offsets, malformed IFD1 pointers, extraneous bytes between
# segments, MakerNote layout and no-op saves, exact Undo of IPTC/XMP, XMP
# padding, empty XMP arrays). Each one failed on the code before the fix.

async def _api_write(client, path: Path, fields: dict | None = None, delete: list | None = None):
    body = {"path": str(path), "fields": [{"key": k, "value": v} for k, v in (fields or {}).items()]}
    if delete:
        body["delete"] = delete
    return await client.post("/write", json=body)


async def _api_undo(client, token: str):
    return await client.post("/undo", json={"token": token})


def _token_of(resp) -> str:
    assert resp.status_code == 200, resp.text
    token = resp.json()["undo_token"]
    assert isinstance(token, str) and token, resp.text
    return token


# ──────────────────────── MPF (multi-picture) offsets ────────────────────────

def _mp_index(data: bytes) -> tuple[int, str, int, list[tuple]]:
    """(MP header position, byte order, position of the MP Entry array, entries) of the
    first APP2 MPF segment, parsed independently of ``core.jpeg_segments``."""
    pos = 2
    while data[pos + 1] != 0xDA:
        length = struct.unpack(">H", data[pos + 2:pos + 4])[0]
        if data[pos + 1] == 0xE2 and data[pos + 4:pos + 8] == b"MPF\x00":
            base = pos + 8
            bo = "<" if data[base:base + 2] == b"II" else ">"
            ifd = base + struct.unpack_from(bo + "L", data, base + 4)[0]
            for i in range(struct.unpack_from(bo + "H", data, ifd)[0]):
                tag, _typ, count, value = struct.unpack_from(bo + "HHLL", data, ifd + 2 + 12 * i)
                if tag == 0xB002:
                    at = base + value
                    return base, bo, at, [struct.unpack_from(bo + "LLLHH", data, at + 16 * k)
                                          for k in range(count // 16)]
        pos += 2 + length
    raise AssertionError("no MPF segment")


def _frame_pixels(data: bytes, frame: int) -> bytes:
    from PIL import Image
    with Image.open(io.BytesIO(data)) as img:
        img.seek(frame)
        return img.convert("RGB").tobytes()


def _assert_valid_mpo(data: bytes, second_pixels: bytes) -> None:
    """Both JPEG streams are intact and the MP Entry sizes/offsets describe them."""
    base, _bo, _at, entries = _mp_index(data)
    (_a0, size0, offset0, _, _), (_a1, size1, offset1, _, _) = entries
    assert offset0 == 0
    start = base + offset1                                        # the second image, absolute
    assert size0 == start and data[start - 2:start] == b"\xff\xd9"   # first image = SOI..EOI
    second = data[start:start + size1]
    assert len(second) == size1 and second[:3] == b"\xff\xd8\xff" and second.endswith(b"\xff\xd9")
    from PIL import Image
    with Image.open(io.BytesIO(second)) as img:
        assert img.convert("RGB").tobytes() == second_pixels     # the secondary JPEG decodes alone
    assert _frame_pixels(data, 1) == second_pixels                 # and is reached through the MPF


def _is_mpf(seg: jpeg_segments.Segment) -> bool:
    return seg.marker == 0xE2 and seg.payload is not None and seg.payload.startswith(b"MPF\x00")


def _assert_only_mp_entry_values_changed(before: bytes, after: bytes) -> None:
    """The MPF segment keeps its bytes except the size/offset fields of the MP Entries."""
    raw_before = next(s for s in jpeg_segments.parse(before).segments if _is_mpf(s)).raw
    raw_after = next(s for s in jpeg_segments.parse(after).segments if _is_mpf(s)).raw
    assert len(raw_before) == len(raw_after) and raw_before[:2] == b"\xff\xe2"
    base, _bo, at, entries = _mp_index(before)
    relative = at - (base - 8)                  # FF E2, length, "MPF\0" precede the MP header
    allowed = {relative + 16 * k + i for k in range(len(entries)) for i in range(4, 12)}
    changed = {i for i, (x, y) in enumerate(zip(raw_before, raw_after)) if x != y}
    assert changed <= allowed, sorted(changed - allowed)


def _mpo_sample(tmp_path: Path) -> Path:
    return _make("jpeg_mpo", tmp_path)


_MPO_WRITES = [
    ("iptc", {"iptc:Caption-Abstract": "x" * 50}, lambda s: s.is_photoshop),
    ("xmp", {"xmp:dc:title": "y" * 50}, lambda s: s.is_xmp),
    ("exif", {"exif:Artist": "z" * 50}, lambda s: s.is_exif),
]


@pytest.mark.parametrize("values,touched", [w[1:] for w in _MPO_WRITES], ids=[w[0] for w in _MPO_WRITES])
def test_mpo_secondary_frame_survives_metadata_writes(tmp_path, values, touched):
    path = _mpo_sample(tmp_path)
    original = path.read_bytes()
    second, first = _frame_pixels(original, 1), _frame_pixels(original, 0)
    _assert_valid_mpo(original, second)
    before = _segments(path)
    ImageHandler().write(path, [MetadataField(k, k, v) for k, v in values.items()])
    data = path.read_bytes()
    assert data != original
    _assert_valid_mpo(data, second)
    assert _frame_pixels(data, 0) == first
    after = _segments(path)
    _untouched_identical(before, after, lambda s: touched(s) or _is_mpf(s))
    _assert_only_mp_entry_values_changed(original, data)
    # Segments are added in front of the MPF segment: the frame offsets do not move.
    mpf = next(i for i, s in enumerate(after.segments) if _is_mpf(s))
    assert all(i < mpf for i, s in enumerate(after.segments) if touched(s))
    assert _mp_index(data)[3][1][2] == _mp_index(original)[3][1][2]
    assert not any("Duplicate metadata key" in w for w in ImageHandler().read(path).warnings)


def _mpo_with_xmp_after_mpf(tmp_path: Path) -> Path:
    """MPO whose XMP segment sits AFTER the APP2 MPF (offsets adjusted by hand)."""
    data = F.mpo_jpeg(exif=F.camera_exif(gps=True, thumbnail=False))
    base, bo, at, entries = _mp_index(data)
    mpf_start = base - 8
    mpf_end = mpf_start + 2 + struct.unpack(">H", data[mpf_start + 2:mpf_start + 4])[0]
    xmp_seg = F.segment(0xE1, jpeg_segments.XMP_HEADER + F.XMP_PACKET.encode("utf-8"))
    out = bytearray(data[:mpf_end] + xmp_seg + data[mpf_end:])
    struct.pack_into(bo + "L", out, at + 4, entries[0][1] + len(xmp_seg))         # size of image 1
    struct.pack_into(bo + "L", out, at + 16 + 8, entries[1][2] + len(xmp_seg))    # offset of image 2
    path = tmp_path / "xmp_after_mpf.jpg"
    path.write_bytes(bytes(out))
    return path


def test_mpo_segment_after_the_mpf_is_rewritten_with_relocated_offsets(tmp_path):
    path = _mpo_with_xmp_after_mpf(tmp_path)
    second = _frame_pixels(path.read_bytes(), 1)
    _assert_valid_mpo(path.read_bytes(), second)
    for title in ("a much longer title " * 20, "short"):
        _write(path, **{"xmp__dc__title": title})
        data = path.read_bytes()
        _assert_valid_mpo(data, second)
        assert _fields(path)["xmp:dc:title"].value == title
    ImageHandler().delete(path, ["xmp:dc:title", "xmp:dc:subject"])
    _assert_valid_mpo(path.read_bytes(), second)


@pytest.mark.asyncio
async def test_mpo_api_write_delete_and_undo_keep_both_frames(client, tmp_path):
    path = _mpo_sample(tmp_path)
    original = path.read_bytes()
    second = _frame_pixels(original, 1)
    token = _token_of(await _api_write(client, path, {
        "iptc:Keywords": ["stereo", "città"], "xmp:dc:title": "Titolo è", "exif:Artist": "Autore"}))
    _assert_valid_mpo(path.read_bytes(), second)
    token2 = _token_of(await _api_write(client, path, delete=["gps:Latitude", "iptc:Keywords"]))
    _assert_valid_mpo(path.read_bytes(), second)
    assert (await _api_undo(client, token2)).status_code == 200
    assert (await _api_undo(client, token)).status_code == 200
    assert path.read_bytes() == original


def _corrupt_mpo(kind: str) -> bytes:
    data = bytearray(F.mpo_jpeg(exif=F.camera_exif(gps=False, thumbnail=False)))
    base, bo, at, _entries = _mp_index(bytes(data))
    if kind == "offset_into_header":
        struct.pack_into(bo + "L", data, at + 16 + 8, 8)        # image 2 "starts" inside the MPF segment
    else:
        data[base:base + 4] = b"XX*\x00"                          # unknown MP endian
    return bytes(data)


@pytest.mark.filterwarnings("ignore::UserWarning")   # Pillow: "malformed MPO file" (intended)
@pytest.mark.parametrize("kind", ["offset_into_header", "bad_endian"])
def test_mpo_with_unusable_mp_index_is_read_only(tmp_path, kind):
    path = tmp_path / "broken_mpo.jpg"
    path.write_bytes(_corrupt_mpo(kind))
    original = path.read_bytes()
    record = ImageHandler().read(path)
    assert record.read_errors == []
    assert not record.format_supports_write and not record.format_supports_delete
    assert record.read_only_reason and "MPF" in record.read_only_reason
    fields = {f.key: f for f in record.fields}
    assert fields["exif:Make"].value == "Canon"
    assert not any(f.editable or f.deletable for f in record.fields)
    assert record.addable_fields == []
    with pytest.raises(MetadataValueError):
        _write(path, exif__Artist="x")
    assert path.read_bytes() == original


# ──────────────────────── malformed EXIF never breaks a read ─────────────────

def _tiff(ifd0, ifd1=None) -> bytes:
    """Little-endian TIFF from [(tag, type, count, value bytes | int)]; >4-byte values out of line."""
    def ifd(entries, start, next_off):
        table, blob = struct.pack("<H", len(entries)), b""
        data_pos = start + 2 + 12 * len(entries) + 4
        for tag, typ, cnt, val in entries:
            if isinstance(val, int):
                val = struct.pack("<L", val)
            if len(val) <= 4:
                table += struct.pack("<HHL", tag, typ, cnt) + val.ljust(4, b"\x00")
            else:
                table += struct.pack("<HHLL", tag, typ, cnt, data_pos + len(blob))
                blob += val + (b"\x00" if len(val) % 2 else b"")
        return table + struct.pack("<L", next_off) + blob
    first = ifd(ifd0, 8, 0)
    if ifd1 is None:
        return b"II*\x00" + struct.pack("<L", 8) + first
    first = ifd(ifd0, 8, 8 + len(first))
    return b"II*\x00" + struct.pack("<L", 8) + first + ifd(ifd1, 8 + len(first), 0)


_MAKE = (0x010F, 2, 4, b"Can\x00")
MALFORMED_IFD1 = {
    "thumb_count0": _tiff([_MAKE], [(0x0201, 4, 0, 0), (0x0202, 4, 1, 10)]),
    "thumb_rational": _tiff([_MAKE], [(0x0201, 5, 1, struct.pack("<LL", 1, 1)), (0x0202, 4, 1, 10)]),
    "thumb_unknown_type": _tiff([_MAKE], [(0x0201, 15, 1, 0), (0x0202, 4, 1, 10)]),
    "length_count2": _tiff([_MAKE], [(0x0201, 4, 1, 8), (0x0202, 3, 2, struct.pack("<HH", 4, 4))]),
}


def _png_with_exif(tiff: bytes) -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (4, 4), (1, 2, 3)).save(buf, "PNG")
    data = buf.getvalue()
    chunk = struct.pack(">L", len(tiff)) + b"eXIf" + tiff + struct.pack(">L", zlib.crc32(b"eXIf" + tiff))
    return data[:33] + chunk + data[33:]                      # right after IHDR


@pytest.mark.parametrize("name", sorted(MALFORMED_IFD1))
def test_malformed_thumbnail_pointers_make_exif_read_only_not_an_exception(tmp_path, name):
    block = exif_codec.parse(MALFORMED_IFD1[name])
    assert block.thumbnail is None
    assert any("thumbnail" in p for p in block.rewrite_problems), block.rewrite_problems
    for path in (tmp_path / "bad.jpg", tmp_path / "bad.png"):
        path.write_bytes(F.jpeg_with([F.segment(0xE1, b"Exif\x00\x00" + MALFORMED_IFD1[name])])
                         if path.suffix == ".jpg" else _png_with_exif(MALFORMED_IFD1[name]))
        record = ImageHandler().read(path)
        fields = {f.key: f for f in record.fields}
        assert fields["exif:Make"].value == "Can" and not fields["exif:Make"].editable
        if path.suffix == ".jpg":   # writable format: the reason EXIF became read-only is shown
            assert record.read_errors == [] and any("thumbnail" in w for w in record.warnings), record.warnings
        json.dumps(record.to_dict(), allow_nan=False)


@pytest.mark.asyncio
async def test_api_read_and_diff_never_answer_500_for_malformed_exif(client, tmp_path):
    paths = []
    for name, tiff in MALFORMED_IFD1.items():
        jpg, png = tmp_path / f"{name}.jpg", tmp_path / f"{name}.png"
        jpg.write_bytes(F.jpeg_with([F.segment(0xE1, b"Exif\x00\x00" + tiff)]))
        png.write_bytes(_png_with_exif(tiff))
        paths += [jpg, png]
    for path in paths:
        resp = await client.get("/read", params={"path": str(path)})
        assert resp.status_code == 200, resp.text
    for a, b in zip(paths, paths[1:]):
        resp = await client.get("/diff", params={"a": str(a), "b": str(b)})
        assert resp.status_code == 200, resp.text


def _ifd_entry_positions(tiff: bytes) -> list[int]:
    """Positions of every 12-byte IFD entry of a valid big-endian TIFF block (independent walk)."""
    out = []

    def walk(offset: int, follow_next: bool) -> None:
        count = struct.unpack_from(">H", tiff, offset)[0]
        for i in range(count):
            pos = offset + 2 + 12 * i
            out.append(pos)
            tag, _typ, _cnt, value = struct.unpack_from(">HHLL", tiff, pos)
            if tag in (0x8769, 0x8825, 0xA005):          # Exif, GPS, Interop IFDs
                walk(value, False)
        next_ifd = struct.unpack_from(">L", tiff, offset + 2 + 12 * count)[0]
        if follow_next and next_ifd:                     # IFD0 -> IFD1
            walk(next_ifd, False)
    walk(struct.unpack_from(">L", tiff, 4)[0], True)
    return out


def _mutated_exif_blocks(seed: int, count: int):
    """Deterministic byte mutations of a full camera EXIF block (GPS, Interop, MakerNote,
    IFD1 + thumbnail): IFD entry type/count/offset fields, random bytes, truncation."""
    rng = random.Random(seed)
    base = F.camera_exif()[6:]
    assert base[:2] == b"MM"
    entries = _ifd_entry_positions(base)
    size = len(base)
    types = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 255, 0xFFFF]
    for _ in range(count):
        data = bytearray(base)
        for _ in range(rng.randint(1, 4)):
            op = rng.random()
            if op < 0.5:
                pos = rng.choice(entries)
                if pos + 12 > len(data):                 # truncated by an earlier mutation
                    continue
                part = rng.randrange(3)
                if part == 0:
                    struct.pack_into(">H", data, pos + 2, rng.choice(types))
                elif part == 1:
                    struct.pack_into(">L", data, pos + 4, rng.choice(
                        [0, 1, 2, 3, 4, 5, 8, 16, 0xFFFF, 0x7FFFFFFF, 0xFFFFFFFF, rng.randrange(2 ** 32)]))
                else:
                    struct.pack_into(">L", data, pos + 8, rng.choice(
                        [0, 1, 4, 7, 8, size - 2, size - 1, size, size + 7, 0xFFFFFFFF, rng.randrange(size)]))
            elif op < 0.85:
                data[rng.randrange(len(data))] = rng.randrange(256)
            elif op < 0.95:
                del data[rng.randrange(8, len(data)):]
            else:
                struct.pack_into(">L", data, 4, rng.choice([0, 7, 8, size, rng.randrange(size)]))
        yield bytes(data)


def test_exif_parser_fuzz_raises_only_exif_format_error():
    parsed = rejected = rewritten = 0
    for tiff in _mutated_exif_blocks(seed=20261101, count=4000):
        try:
            block = exif_codec.parse(tiff)
        except exif_codec.ExifFormatError:
            rejected += 1
            continue
        parsed += 1
        for ifd, entries in block.ifds.items():
            for tag, entry in entries.items():
                exif_codec.decode(ifd, tag, entry, block.byteorder)     # never raises
        if not block.rewrite_problems:
            again = exif_codec.parse(exif_codec.serialize(block))
            assert exif_codec.same_content(again, block)
            rewritten += 1
    assert parsed > 1000 and rejected > 100 and rewritten > 500, (parsed, rejected, rewritten)


@pytest.mark.filterwarnings("ignore::UserWarning")   # Pillow warns about the corrupted EXIF on purpose
def test_image_handler_read_fuzz_never_raises(tmp_path):
    handler = ImageHandler()
    jpg, png, tif = tmp_path / "fuzz.jpg", tmp_path / "fuzz.png", tmp_path / "fuzz.tif"
    for i, tiff in enumerate(_mutated_exif_blocks(seed=31, count=240)):
        jpg.write_bytes(F.jpeg_with([F.segment(0xE1, b"Exif\x00\x00" + tiff)]))
        records = [handler.read(jpg)]
        if i % 3 == 0:
            png.write_bytes(_png_with_exif(tiff))
            records.append(handler.read(png))
        elif i % 3 == 1:
            tif.write_bytes(tiff)                       # TIFF path: parsed through mmap
            records.append(handler.read(tif))
        for record in records:
            json.dumps(record.to_dict(), allow_nan=False)
            assert not any("metadata read error" in e for e in record.read_errors), record.read_errors


@pytest.mark.filterwarnings("ignore::UserWarning")   # Pillow warns about the corrupted TIFF (intended)
def test_malformed_tiff_is_an_exif_read_error_not_a_buffer_error(tmp_path):
    path = tmp_path / "bad.tif"
    path.write_bytes(b"II*\x00" + struct.pack("<L", 8) + struct.pack("<H", 1)
                     + struct.pack("<HHLL", 0x010E, 2, 5000, 26) + bytes(4))
    record = ImageHandler().read(path)
    assert any(e.startswith("EXIF read error") for e in record.read_errors), record.read_errors
    assert not any("BufferError" in e for e in record.read_errors)


# ──────────────────────── extraneous bytes between segments ──────────────────

def _with_junk(data: bytes, after_marker: int, junk: bytes = b"\x00\x00") -> bytes:
    segs, tail = F.split_jpeg(data)
    i = next(k for k, s in enumerate(segs) if s[1] == after_marker)
    return F.assemble(segs[:i + 1] + [junk] + segs[i + 1:], tail)


def test_extraneous_bytes_are_read_with_a_warning_and_the_format_is_read_only(tmp_path):
    clean = F.jpeg_with([F.segment(0xE1, F.camera_exif())])
    data = _with_junk(clean, 0xE1)
    from PIL import Image
    with Image.open(io.BytesIO(data)) as img:
        img.load()                                              # decoders tolerate it
    path, reference = tmp_path / "junk.jpg", tmp_path / "clean.jpg"
    path.write_bytes(data)
    reference.write_bytes(clean)
    record = ImageHandler().read(path)
    assert record.read_errors == []
    assert any("extraneous" in w for w in record.warnings), record.warnings
    assert not record.format_supports_write and "extraneous" in record.read_only_reason
    got = {f.key: f.value for f in record.fields if f.key.startswith(("exif:", "gps:"))}
    want = {f.key: f.value for f in ImageHandler().read(reference).fields if f.key.startswith(("exif:", "gps:"))}
    assert got == want and len(got) > 20
    assert not any(f.editable or f.deletable for f in record.fields) and record.addable_fields == []
    with pytest.raises(MetadataValueError):
        _write(path, exif__Artist="x")
    assert path.read_bytes() == data


@pytest.mark.asyncio
async def test_api_extraneous_bytes_read_200_and_write_422(client, tmp_path):
    path = tmp_path / "junk.jpg"
    path.write_bytes(_with_junk(F.jpeg_with([F.segment(0xE1, F.camera_exif())]), 0xE1))
    original = path.read_bytes()
    resp = await client.get("/read", params={"path": str(path)})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["read_errors"] == [] and body["read_only_reason"]
    assert sum(1 for f in body["fields"] if f["key"].startswith("exif:")) > 20
    resp = await _api_write(client, path, {"exif:Artist": "x"})
    assert resp.status_code == 422, resp.text
    assert path.read_bytes() == original


def test_segment_parser_tolerant_mode_keeps_every_extraneous_byte():
    parts = [
        b"\xff\xd8",
        F.segment(0xE0, b"JFIF\x00\x01\x02\x00\x00\x01\x00\x01\x00\x00"),
        b"\x00\x00",                                         # junk before COM
        F.segment(0xFE, b"comment"),
        b"\xff\x00",                                         # FF 00 pair before APP1
        F.segment(0xE1, b"Exif\x00\x00tiff"),
        F.segment(0xDB, bytes(65)),
        b"\x12\x34\x56",                                     # junk before SOS
    ]
    tail = b"\xff\xda" + struct.pack(">H", 8) + bytes(6) + b"\x12\xff\x00\x34\xff\xd9"
    data = b"".join(parts) + tail
    with pytest.raises(jpeg_segments.JpegFormatError):
        jpeg_segments.parse(data)
    jpeg = jpeg_segments.parse(data, tolerant=True)
    assert jpeg.to_bytes() == data
    assert [s.marker for s in jpeg.segments] == [0xE0, 0xFE, 0xE1, 0xDB]
    assert [(n, m) for _, n, m in jpeg.extraneous] == [(2, 0xFE), (2, 0xE1), (3, 0xDA)]
    assert jpeg.segments[2].payload == b"Exif\x00\x00tiff"
    assert jpeg_segments.parse(b"".join(parts[:2] + parts[3:4] + parts[5:7]) + tail).extraneous == []


# ──────────────────────── MakerNote layout, no-op saves, exact Undo ──────────

def _exif_tiff(path: Path) -> bytes:
    jpeg = _segments(path)
    return jpeg.segments[jpeg.exif_indexes()[0]].payload[6:]


@pytest.mark.parametrize("edit", ["grow", "shrink", "add_tag", "delete_gps", "delete_exif_values"])
def test_canon_makernote_keeps_its_offset_and_its_absolute_pointers(tmp_path, edit):
    path = _make("jpeg_canon_makernote", tmp_path)
    tiff = _exif_tiff(path)
    where = F.makernote_location(tiff)
    assert F.canon_makernote_resolves(tiff)
    handler = ImageHandler()
    pixels = _pixels(path)
    if edit == "grow":
        _write(path, exif__Artist="A much longer artist name " * 4)
    elif edit == "shrink":
        _write(path, exif__Artist="A", exif__Model="M")
    elif edit == "add_tag":
        _write(path, exif__ImageDescription="Description added " * 3)
    elif edit == "delete_gps":
        handler.delete(path, [f.key for f in handler.read(path).fields if f.key.startswith("gps:")])
    else:   # values stored before and after the MakerNote in the Exif IFD
        handler.delete(path, ["exif:DateTimeOriginal", "exif:UserComment", "exif:ExposureTime"])
    after = _exif_tiff(path)
    assert after != tiff
    assert F.makernote_location(after) == where
    assert F.canon_makernote_resolves(after)
    assert _pixels(path) == pixels


def test_nikon_makernote_survives_rewrites_byte_for_byte(tmp_path):
    path = _make("jpeg_nikon_makernote", tmp_path)
    tiff = _exif_tiff(path)
    offset, count = F.makernote_location(tiff)
    makernote = tiff[offset:offset + count]
    _write(path, exif__Artist="Someone else entirely", exif__Model="Z9")
    after = _exif_tiff(path)
    assert F.makernote_location(after) == (offset, count)
    assert after[offset:offset + count] == makernote and F.nikon_makernote_resolves(after)


@pytest.mark.asyncio
async def test_noop_save_of_a_nul_padded_make_leaves_the_file_untouched(client, tmp_path):
    path = _make("jpeg_canon_makernote", tmp_path)
    original = path.read_bytes()
    make_bytes = exif_codec.parse(_exif_tiff(path)).ifds["0th"][0x010F].data
    assert make_bytes.startswith(b"Canon\x00\x00")                     # NUL padded, as cameras do
    fields = _fields(path)
    assert fields["exif:Make"].value == "Canon"
    resp = await _api_write(client, path, {"exif:Make": "Canon", "exif:Model": fields["exif:Model"].value})
    assert resp.status_code == 200, resp.text
    assert resp.json()["undo_token"] is None
    assert path.read_bytes() == original
    # A real change elsewhere leaves the unchanged Make bytes as they are.
    _token_of(await _api_write(client, path, {"exif:Make": "Canon", "exif:Artist": "Nuovo"}))
    assert exif_codec.parse(_exif_tiff(path)).ifds["0th"][0x010F].data == make_bytes
    assert F.canon_makernote_resolves(_exif_tiff(path))


def test_noop_xmp_and_iptc_values_do_not_rewrite_the_file(tmp_path):
    path = _make("jpeg_iptc", tmp_path)
    original = path.read_bytes()
    fields = _fields(path)
    ImageHandler().write(path, [MetadataField("iptc:Keywords", "", fields["iptc:Keywords"].value),
                                MetadataField("exif:Make", "", fields["exif:Make"].value)])
    assert path.read_bytes() == original
    path = _make("jpeg_xmp_first", tmp_path)
    original = path.read_bytes()
    fields = _fields(path)
    ImageHandler().write(path, [MetadataField("xmp:dc:title", "", fields["xmp:dc:title"].value),
                                MetadataField("xmp:xmp:Rating", "", fields["xmp:xmp:Rating"].value)])
    assert path.read_bytes() == original


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["write", "delete"])
async def test_api_undo_restores_the_exif_segment_byte_for_byte(client, tmp_path, operation):
    path = _make("jpeg_canon_makernote", tmp_path)
    original = path.read_bytes()
    if operation == "write":
        resp = await _api_write(client, path, {"exif:Artist": "Nuovo Autore è", "exif:Model": "X"})
    else:
        keys = [f.key for f in ImageHandler().read(path).fields if f.key.startswith("gps:")]
        resp = await _api_write(client, path, delete=keys + ["exif:SubjectArea"])
    token = _token_of(resp)
    assert path.read_bytes() != original and F.canon_makernote_resolves(_exif_tiff(path))
    resp = await _api_undo(client, token)
    assert resp.status_code == 200, resp.text
    assert path.read_bytes() == original


def test_restored_makernote_returns_to_its_original_offset(tmp_path):
    """Undo after an unrelated later change (no exact segment restore possible): the
    MakerNote deleted by the undone operation goes back to its original offset."""
    path = _make("jpeg_canon_makernote", tmp_path)
    handler = ImageHandler()
    where = F.makernote_location(_exif_tiff(path))
    keys = ["exif:MakerNote", "exif:Artist"]

    def operation(tmp):
        snap = handler.snapshot(tmp, keys)
        handler.apply_delete(tmp, ["exif:MakerNote"])
        handler.apply_write(tmp, [MetadataField("exif:Artist", "", "Changed")])
        return snap
    snap = _txn(path, operation)
    assert "exif:MakerNote" not in _fields(path)
    _write(path, exif__Model="Another change")
    _txn(path, lambda tmp: handler.restore(tmp, snap))
    tiff = _exif_tiff(path)
    assert F.makernote_location(tiff) == where and F.canon_makernote_resolves(tiff)
    assert _fields(path)["exif:Model"].value == "Another change"
    assert handler.snapshot(path, keys) == snap


# ──────────────────────── IPTC / XMP: exact Undo, padding, empty arrays ──────

def _iim_without_charset_marker(tmp_path: Path) -> Path:
    iim = F.iim([(2, 0, b"\x00\x04"), (2, 25, b"sea"), (2, 25, b"sun"), (2, 120, b"A caption"), (2, 80, b"Bob")])
    padded = iim + b"\x00" * ((4 - len(iim) % 4) % 4)
    app13 = F.segment(0xED, b"Photoshop 3.0\x00" + F.resource(0x03ED, F.RESOLUTION_INFO)
                      + F.resource(0x0404, padded) + F.resource(0x0425, hashlib.md5(padded).digest()))
    path = tmp_path / "iptc_no_marker.jpg"
    path.write_bytes(F.jpeg_with([app13]))
    return path


@pytest.mark.asyncio
async def test_api_undo_of_iptc_removes_the_charset_marker_the_write_added(client, tmp_path):
    path = _iim_without_charset_marker(tmp_path)
    original = path.read_bytes()
    token = _token_of(await _api_write(client, path, {"iptc:Keywords": ["città", "mare", "sea"]}))
    assert _iim(path)[0] == iptc.Dataset(1, 90, b"\x1b%G")       # needed for the UTF-8 text
    assert (await _api_undo(client, token)).status_code == 200
    assert path.read_bytes() == original
    # Delete everything, then Undo: the IIM record comes back identical as well.
    keys = [f.key for f in ImageHandler().read(path).fields if f.key.startswith("iptc:") and f.deletable]
    token = _token_of(await _api_write(client, path, delete=keys))
    assert (await _api_undo(client, token)).status_code == 200
    assert path.read_bytes() == original


@pytest.mark.asyncio
async def test_api_undo_of_non_ascii_iptc_added_from_scratch_restores_the_file(client, tmp_path):
    path = _make("jpeg_no_exif", tmp_path)
    original = path.read_bytes()
    token = _token_of(await _api_write(client, path, {"iptc:Keywords": ["città"], "xmp:dc:title": "è"}))
    assert _iim(path)[0] == iptc.Dataset(1, 90, b"\x1b%G")
    assert (await _api_undo(client, token)).status_code == 200
    assert path.read_bytes() == original          # no APP13 left with a lone charset marker, no empty XMP
    _write(path, iptc__Keywords=["città"])
    ImageHandler().delete(path, ["iptc:Keywords"])
    assert path.read_bytes() == original


_XMP_BEGIN = '<?xpacket begin="﻿" id="W5M0MpCehiHzreSzNTczkc9d"?>'
_XMP_END = '<?xpacket end="w"?>'


def _xmp_body(inner: str = "", attrs: str = "") -> str:
    return ('<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
            '<rdf:Description rdf:about="" xmlns:dc="http://purl.org/dc/elements/1.1/" '
            f'xmlns:xmp="http://ns.adobe.com/xap/1.0/" {attrs}>{inner}</rdf:Description></rdf:RDF></x:xmpmeta>')


def _xmp_jpeg(tmp_path: Path, packet: str, name: str = "xmp.jpg") -> Path:
    path = tmp_path / name
    path.write_bytes(F.jpeg_with([F.segment(0xE1, jpeg_segments.XMP_HEADER + packet.encode("utf-8"))]))
    return path


def _xmp_payload(path: Path) -> bytes:
    jpeg = _segments(path)
    return jpeg.segments[jpeg.xmp_indexes()[0]].payload[len(jpeg_segments.XMP_HEADER):]


def test_xmp_padding_is_used_before_a_too_large_refusal(tmp_path):
    head = _XMP_BEGIN + _xmp_body() + "\n"
    room = jpeg_segments.MAX_PAYLOAD - len(jpeg_segments.XMP_HEADER) - len((head + "\n" + _XMP_END).encode())
    packet = head + " " * (room - 60) + "\n" + _XMP_END
    path = _xmp_jpeg(tmp_path, packet)
    size = len(_xmp_payload(path))
    title = "un titolo piuttosto lungo è " * 4
    _write(path, **{"xmp__dc__title": title})
    assert _fields(path)["xmp:dc:title"].value == title
    assert len(_xmp_payload(path)) == size                     # the padding absorbed the growth
    text = _xmp_payload(path).decode("utf-8")
    assert text.startswith(_XMP_BEGIN) and text.endswith("\n" + _XMP_END)
    _write(path, **{"xmp__dc__title": "t"})                     # shrinking gives the room back
    assert len(_xmp_payload(path)) == size
    with pytest.raises(MetadataValueError) as exc:
        _write(path, **{"xmp__dc__description": "y" * 70_000})
    assert exc.value.code == ErrorCode.TOO_LARGE and exc.value.key == "xmp:dc:description"


@pytest.mark.asyncio
async def test_api_undo_restores_the_xmp_packet_byte_for_byte(client, tmp_path):
    packet = (_XMP_BEGIN.replace('"', "'") + _xmp_body(
        "<dc:title><rdf:Alt><rdf:li xml:lang='x-default'>T</rdf:li></rdf:Alt></dc:title>",
        "xmp:Rating='3'").replace('"', "'") + " " * 120 + _XMP_END.replace('"', "'"))
    path = _xmp_jpeg(tmp_path, packet)
    original = path.read_bytes()
    token = _token_of(await _api_write(client, path, {"xmp:dc:title": "Nuovo", "xmp:xmp:Rating": 5}))
    assert path.read_bytes() != original
    assert (await _api_undo(client, token)).status_code == 200
    assert path.read_bytes() == original


def test_xmp_empty_array_reads_as_empty_list():
    packet = xmp.XmpPacket((_XMP_BEGIN + _xmp_body("<dc:subject><rdf:Bag/></dc:subject>") + _XMP_END).encode())
    (prop,) = packet.find("http://purl.org/dc/elements/1.1/", "subject")
    assert packet.read_value(prop, "bag") == []
    assert packet.read_value(prop, "seq") is None


@pytest.mark.asyncio
async def test_empty_xmp_bag_is_addable_and_undo_brings_it_back(client, tmp_path):
    path = _xmp_jpeg(tmp_path, _XMP_BEGIN + _xmp_body("<dc:subject><rdf:Bag/></dc:subject>") + _XMP_END)
    original = path.read_bytes()
    resp = await client.get("/read", params={"path": str(path)})
    body = resp.json()
    assert "xmp:dc:subject" not in {f["key"] for f in body["fields"]}
    addable = {f["key"]: f for f in body["addable_fields"]}
    assert addable["xmp:dc:subject"]["multi"] is True
    token = _token_of(await _api_write(client, path, {"xmp:dc:subject": ["uno", "due è"]}))
    assert _fields(path)["xmp:dc:subject"].value == ["uno", "due è"]
    assert _xmp_payload(path).count(b"<dc:subject>") == 1         # the empty Bag was filled in place
    assert (await _api_undo(client, token)).status_code == 200
    assert path.read_bytes() == original


def test_xmp_bag_with_unexpected_items_stays_hidden_and_not_addable(tmp_path):
    inner = ('<dc:subject><rdf:Bag><rdf:li><rdf:Description><dc:x>struct</dc:x></rdf:Description>'
             '</rdf:li></rdf:Bag></dc:subject>')
    path = _xmp_jpeg(tmp_path, _XMP_BEGIN + _xmp_body(inner) + _XMP_END)
    record = ImageHandler().read(path)
    assert "xmp:dc:subject" not in {f.key for f in record.fields}
    assert "xmp:dc:subject" not in {f.key for f in record.addable_fields}   # preserved untouched
