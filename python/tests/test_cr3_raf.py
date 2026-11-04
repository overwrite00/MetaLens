"""Canon CR3 and Fujifilm RAF: EXIF, GPS and the preview picture, read only."""
from core import sniff
from core.registry import HandlerRegistry
from tests.factories import image


def _read(path):
    record = HandlerRegistry.get(path).read(path)
    return record, {f.key: f for f in record.fields}


def test_cr3_exif_and_gps(tmp_path):
    path = tmp_path / "a.cr3"
    path.write_bytes(image.cr3_bytes())
    record, keys = _read(path)
    assert record.read_errors == [] and record.handler_name == "image"
    assert keys["exif:Make"].value == "Canon" and keys["exif:Model"].value == "Canon EOS R5"
    assert keys["exif:ExposureTime"].value == "1/250" and keys["exif:ISOSpeedRatings"].value == 400
    assert keys["gps:LatitudeRef"].value == "N" and keys["gps:Latitude"].value[0] == "45/1"
    assert not record.format_supports_write and not any(f.editable or f.deletable for f in record.fields)


def test_raf_reads_the_preview_picture_metadata(tmp_path):
    path = tmp_path / "a.raf"
    path.write_bytes(image.raf_bytes())
    record, keys = _read(path)
    assert record.read_errors == [] and keys["exif:Make"].value == "Canon" and "gps:Latitude" in keys
    assert not record.format_supports_write


def test_both_are_recognized_without_extension(tmp_path):
    for name, data, kind in (("one", image.cr3_bytes(), "cr3"), ("two", image.raf_bytes(), "raf")):
        path = tmp_path / name
        path.write_bytes(data)
        assert sniff.detect(path) == kind and HandlerRegistry.get(path).NAME == "image"
        assert "exif:Make" in _read(path)[1]


def test_damaged_files_never_raise(tmp_path):
    for data in (image.cr3_bytes(), image.raf_bytes()):
        for cut in range(0, len(data), 37):
            path = tmp_path / "cut.cr3" if data[4:8] == b"ftyp" else tmp_path / "cut.raf"
            path.write_bytes(data[:cut])
            HandlerRegistry.get(path).read(path)
