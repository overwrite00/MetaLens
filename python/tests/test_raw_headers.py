"""TIFF-based raw headers (ORF, RW2) and the Exif 3.0 UTF-8 type."""
import pytest

from core import exif_codec
from core.registry import HandlerRegistry
from tests.factories import image


def _record(path):
    return HandlerRegistry.get(path).read(path)


def _keys(record):
    return {f.key: f for f in record.fields}


def test_rw2_is_read_without_errors(tmp_path):
    path = tmp_path / "p.rw2"
    path.write_bytes(image.le_raw_tiff(b"IIU\x00", b"Panasonic", b"DC-Test"))
    record = _record(path)
    keys = _keys(record)
    assert record.read_errors == []
    assert keys["exif:Make"].value == "Panasonic" and keys["exif:Model"].value == "DC-Test"
    assert any("not supported by the image library" in w for w in record.warnings)
    assert not record.format_supports_write and not any(f.editable for f in record.fields)


@pytest.mark.parametrize("magic", [b"IIRO", b"IIRS"])
def test_olympus_little_endian_headers(tmp_path, magic):
    path = tmp_path / "o.orf"
    path.write_bytes(image.le_raw_tiff(magic, b"OLYMPUS", b"E-M1"))
    record = _record(path)
    assert record.read_errors == [] and _keys(record)["exif:Make"].value == "OLYMPUS"


def test_olympus_big_endian_header(tmp_path):
    path = tmp_path / "o.orf"
    path.write_bytes(b"MMOR" + image.camera_exif(thumbnail=False)[10:])
    record = _record(path)
    assert record.read_errors == [] and _keys(record)["exif:Make"].value == "Canon"


def test_raw_headers_are_refused_outside_raw_files():
    data = image.le_raw_tiff(b"IIU\x00", b"x", b"y")
    with pytest.raises(exif_codec.ExifFormatError):
        exif_codec.parse(data)
    assert exif_codec.parse(data, raw=True).ifds["0th"]


def test_unknown_raw_extensions_are_served_by_the_image_handler(tmp_path):
    for name in ("a.nrw", "a.sr2", "a.3fr", "a.iiq", "a.rwl"):
        path = tmp_path / name
        path.write_bytes(image.le_raw_tiff(b"II*\x00", b"Maker", b"Model"))
        assert HandlerRegistry.get(path).NAME == "image"
        assert _keys(_record(path))["exif:Make"].value == "Maker"


def test_utf8_exif_type_is_read_and_does_not_block_a_rewrite(tmp_path):
    # ImageDescription stored as type 129 (UTF-8) in an otherwise ordinary camera EXIF block.
    block = bytearray(image.camera_exif(thumbnail=False)[6:])
    exif = exif_codec.parse(bytes(block))
    assert not exif.rewrite_problems
    raw = image.le_raw_tiff(b"II*\x00", b"Maker", b"Model")
    parsed = exif_codec.parse(raw)
    assert parsed.rewrite_problems == []
    entry = parsed.ifds["0th"][0x010E]
    assert entry.type == 129
    decoded = exif_codec.decode("0th", 0x010E, entry, parsed.byteorder)
    assert decoded.value == "Descrizione è" and not decoded.editable


def test_jpeg_with_utf8_exif_type_stays_writable(tmp_path):
    segment = b"Exif\x00\x00" + image.le_raw_tiff(b"II*\x00", b"Maker", b"Model")
    path = tmp_path / "u.jpg"
    path.write_bytes(image.jpeg_with([image.segment(0xE1, segment)]))
    record = _record(path)
    keys = _keys(record)
    assert record.read_errors == [] and record.format_supports_write
    assert keys["exif:ImageDescription"].value == "Descrizione è" and not keys["exif:ImageDescription"].editable
    assert keys["exif:Make"].editable
