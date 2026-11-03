"""JFIF, COM, MPF and ICC fields (core/icc.py and the JPEG structure fields)."""
import struct

from core import icc
from core.registry import HandlerRegistry
from tests.factories import by_name, image


def _keys(path):
    record = HandlerRegistry.get(path).read(path)
    assert record.read_errors == []
    return record, {f.key: f for f in record.fields}


def _build(tmp_path, name):
    folder = tmp_path / name
    folder.mkdir()
    return by_name(name).build(folder)


def test_icc_profile_is_assembled_from_segments_and_described(tmp_path):
    record, keys = _keys(_build(tmp_path, "jpeg_icc_comment"))
    assert keys["icc:Version"].value == "2.1.0"
    assert keys["icc:ProfileClass"].value == "Display device"
    assert keys["icc:ColorSpace"].value == "RGB" and keys["icc:ConnectionSpace"].value == "XYZ"
    assert keys["icc:RenderingIntent"].value == "Relative colorimetric"
    assert keys["icc:Platform"].value == "Microsoft"
    assert keys["icc:CreationDate"].value == "2026-01-02T03:04:05+00:00"
    assert keys["icc:Description"].value == "MetaLens test RGB"
    assert keys["icc:Copyright"].value == "Public domain"
    assert not any(f.editable or f.deletable for k, f in keys.items() if k.startswith("icc:"))


def test_comments_are_listed(tmp_path):
    _, keys = _keys(_build(tmp_path, "jpeg_icc_comment"))
    assert keys["jpeg:Comment"].value == ["First comment é", "Second"] and keys["jpeg:Comment"].multi


def test_jfif_header(tmp_path):
    _, keys = _keys(_build(tmp_path, "jpeg_jfif_exif"))
    assert keys["jfif:Version"].value.startswith("1.")
    assert keys["jfif:ResolutionUnit"].value in ("None (aspect ratio)", "Inches", "Centimeters")
    assert isinstance(keys["jfif:XResolution"].value, int)


def test_mpf_index(tmp_path):
    _, keys = _keys(_build(tmp_path, "jpeg_mpo"))
    assert keys["mpf:NumberOfImages"].value == 2
    assert keys["mpf:image1:Type"].value == "Baseline MP primary image"
    assert keys["mpf:image1:Size"].computed


def test_invalid_profiles_give_no_fields():
    assert icc.fields(b"") == []
    assert icc.fields(bytes(200)) == []
    assert icc.fields(image.icc_profile()[:100]) == []
    assert icc.assemble([b"ICC_PROFILE" + bytes(1) + bytes([1, 2]) + b"x"]) is None     # second part missing


def test_truncated_tag_table_never_raises():
    profile = bytearray(image.icc_profile())
    struct.pack_into(">I", profile, 128, 10_000)         # claims far more tags than exist
    assert {f.key for f in icc.fields(bytes(profile))} >= {"icc:Version"}
    for cut in range(128, len(profile), 7):
        icc.fields(bytes(profile[:cut]))


def test_icc_in_png_tiff_and_webp(tmp_path):
    from PIL import Image
    profile = image.icc_profile()
    for name in ("p.png", "p.tif", "p.webp"):
        path = tmp_path / name
        Image.new("RGB", (4, 4)).save(path, icc_profile=profile)
        _, keys = _keys(path)
        assert keys["icc:Description"].value == "MetaLens test RGB", name
