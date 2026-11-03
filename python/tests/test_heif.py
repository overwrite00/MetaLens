"""HEIC / AVIF reading (core/handlers/heif_handler.py)."""
import pytest

from core.registry import HandlerRegistry
from tests.factories import by_name, heif


def _build(tmp_path, name):
    folder = tmp_path / name
    folder.mkdir()
    return by_name(name).build(folder)


def _read(path):
    record = HandlerRegistry.get(path).read(path)
    return record, {f.key: f for f in record.fields}


def test_heic_exif_xmp_icc_and_size(tmp_path):
    record, keys = _read(_build(tmp_path, "heic_camera"))
    assert record.read_errors == [] and record.handler_name == "heif"
    assert keys["img:format"].value == "HEIF"
    assert (keys["img:width"].value, keys["img:height"].value) == (4032, 3024)
    assert keys["heif:Rotation"].value == 90
    assert keys["heif:MajorBrand"].value == "heic"
    assert keys["exif:Make"].value == "Canon" and keys["gps:Latitude"].value
    assert keys["xmp:dc:title"].value == "Titolo è"
    assert keys["icc:Description"].value == "MetaLens test RGB"
    assert not record.format_supports_write and record.read_only_reason
    assert not any(f.editable or f.deletable for f in record.fields)


def test_avif_is_recognized(tmp_path):
    record, keys = _read(_build(tmp_path, "avif_basic"))
    assert record.read_errors == [] and keys["img:format"].value == "AVIF"
    assert "exif:Make" in keys and "icc:Version" not in keys


def test_heic_without_extension_is_found_by_content(tmp_path):
    path = heif.write_heif(tmp_path / "IMG_0002")
    assert HandlerRegistry.get(path).NAME == "heif"
    assert "exif:Model" in _read(path)[1]


def test_damaged_and_truncated_files_never_raise(tmp_path):
    data = heif.write_heif(tmp_path / "a.heic").read_bytes()
    for cut in range(0, len(data), 29):
        path = tmp_path / "cut.heic"
        path.write_bytes(data[:cut])
        HandlerRegistry.get(path).read(path)
    path = tmp_path / "junk.heic"
    path.write_bytes(b"\x00\x00\x00\x18ftypheic" + b"\xff" * 64)
    record, _ = _read(path)
    assert isinstance(record.fields, list)


def test_item_payload_outside_the_file_is_ignored(tmp_path):
    path = heif.write_heif(tmp_path / "a.heic")
    data = bytearray(path.read_bytes())
    marker = data.index(b"iloc")
    data[marker + 4 + 4 + 2 + 2 + 2 + 2 + 2:marker + 4 + 4 + 2 + 2 + 2 + 2 + 2 + 4] = b"\x7f\xff\xff\xff"
    path.write_bytes(bytes(data))
    record, keys = _read(path)
    assert record.read_errors == [] and "exif:Make" in keys
