"""PNG XMP / header / modification time and XMP + IPTC stored inside TIFF files."""
import struct
import zlib

from core.registry import HandlerRegistry


def _chunk(kind: bytes, body: bytes) -> bytes:
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))


def _read(path):
    record = HandlerRegistry.get(path).read(path)
    return record, {f.key: f for f in record.fields}


def test_png_xmp_is_interpreted_and_the_raw_chunk_is_hidden(tmp_path):
    from tests.factories import by_name
    path = by_name("png_text_exif").build(_dir(tmp_path))
    record, keys = _read(path)
    assert record.read_errors == []
    assert keys["xmp:dc:title"].value == "Titolo è"
    assert keys["xmp:xmp:CreatorTool"].value == "Tool & Co"
    assert "png:XML:com.adobe.xmp" not in keys


def test_png_text_languages_have_distinct_keys(tmp_path):
    from tests.factories import by_name
    _, keys = _read(by_name("png_text_exif").build(_dir(tmp_path)))
    assert keys["png:Description@it"].value.startswith("Descrizione")
    assert keys["png:Description@en"].value == "Description"
    assert keys["png:Title"].value == "PNG title"


def test_png_header_fields(tmp_path):
    from tests.factories import by_name
    _, keys = _read(by_name("png_text_exif").build(_dir(tmp_path)))
    assert keys["img:bit_depth"].value == 8
    assert keys["img:color_type"].value == "RGB"
    assert keys["img:interlace"].value is False


def test_png_modification_time(tmp_path):
    from tests.factories import by_name
    path = by_name("png_text_exif").build(_dir(tmp_path))
    data = path.read_bytes()
    at = data.index(b"IDAT") - 4
    path.write_bytes(data[:at] + _chunk(b"tIME", struct.pack(">HBBBBB", 2026, 5, 1, 8, 20, 30)) + data[at:])
    _, keys = _read(path)
    assert keys["pngchunk:tIME"].value == "2026-05-01T08:20:30+00:00"


def test_png_with_a_bad_time_chunk_is_still_read(tmp_path):
    from tests.factories import by_name
    path = by_name("png_text_exif").build(_dir(tmp_path))
    data = path.read_bytes()
    at = data.index(b"IDAT") - 4
    path.write_bytes(data[:at] + _chunk(b"tIME", struct.pack(">HBBBBB", 2026, 13, 40, 99, 99, 99)) + data[at:])
    record, keys = _read(path)
    assert record.read_errors == [] and "pngchunk:tIME" not in keys


def test_tiff_xmp_and_iptc_are_interpreted(tmp_path):
    from tests.factories import by_name
    record, keys = _read(by_name("tiff_xmp_iptc").build(_dir(tmp_path)))
    assert record.read_errors == []
    assert keys["xmp:dc:title"].value == "Titolo è"
    assert keys["xmp:dc:creator"].value == ["Ada"]
    assert any(k.startswith("iptc:") for k in keys)
    assert keys["xmp:dc:title"].editable and not keys["img:width"].editable
    assert keys["xmp:dc:title"].deletable and not any(f.deletable for k, f in keys.items() if k.startswith("iptc:") and k != "iptc:@block")
    assert keys["iptc:@block"].deletable


def _dir(tmp_path):
    folder = tmp_path / "sample"
    folder.mkdir(exist_ok=True)
    return folder
