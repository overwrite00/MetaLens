"""Known fields of vendor MakerNotes are shown read only."""
import struct

from core import makernote, sensitivity


def _canon(offset=0):
    text = b"Ada Lovelace\x00"
    entry = struct.pack("<HHLL", 0x0009, 2, len(text), offset + 2 + 12 + 4)
    return struct.pack("<H", 1) + entry + bytes(4) + text


def _nikon():
    text = b"6001234\x00"
    ifd = struct.pack(">H", 1) + struct.pack(">HHLL", 0x001D, 2, len(text), 8 + 2 + 12 + 4) + bytes(4)
    return b"Nikon\x00\x02\x10\x00\x00" + b"MM\x00*" + struct.pack(">L", 8) + ifd + text


def test_canon_owner_name_with_absolute_offsets():
    found = makernote.read("Canon", _canon(500), "<", 500)
    assert [(v.name, v.value) for v in found] == [("OwnerName", "Ada Lovelace")]


def test_nikon_serial_number_with_relative_offsets():
    found = makernote.read("NIKON", _nikon(), ">", 300)
    assert [(v.name, v.value) for v in found] == [("SerialNumber", "6001234")]


def test_unknown_or_damaged_notes_give_nothing():
    assert makernote.read("Acme", b"VENDOR\x00\x01\x02\x03" * 4, "<", 100) == []
    assert makernote.read("Canon", b"\xff" * 40, "<", 100) == []
    assert makernote.read("Canon", _canon(500)[:10], "<", 500) == []
    assert makernote.read("Canon", _canon(0), "<", None) == []


def test_the_values_are_classified():
    assert sensitivity.classify("maker:canon:OwnerName") == "identity"
    assert sensitivity.classify("maker:nikon:SerialNumber") == "device"


def test_a_real_jpeg_shows_the_note_and_it_goes_with_the_makernote(tmp_path):
    from core.registry import HandlerRegistry
    from tests.factories import image
    path = tmp_path / "canon.jpg"
    exif = image.vendor_camera_exif("canon")
    path.write_bytes(image.jpeg_with([image.segment(0xE1, b"Exif\x00\x00" + exif[6:])]))
    keys = {f.key: f for f in HandlerRegistry.get(path).read(path).fields}
    assert keys["maker:canon:ImageType"].value == "Canon EOS TEST BODY"
    assert keys["maker:canon:ImageType"].computed and not keys["maker:canon:ImageType"].deletable
