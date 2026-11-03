"""QuickTime / ISO base media structure fields (core/isobmff.py, handlers/qt_fields.py)."""
import struct

import pytest

from core.handlers.qt_fields import qt_fields
from core.isobmff import Reader, open_reader
from core.registry import HandlerRegistry
from tests.factories import mp4


@pytest.fixture
def phone(tmp_path):
    return mp4.write_phone_movie(tmp_path / "phone.mov")


def _by_key(path):
    fields, notes = qt_fields(path)
    return {f.key: f for f in fields}, notes


def test_movie_and_track_headers(phone):
    fields, notes = _by_key(phone)
    assert fields["qt:MajorBrand"].value == "qt"
    assert fields["qt:CreateDate"].value == "2026-05-01T08:20:30+00:00"
    assert fields["qt:TimeScale"].value == 1000
    assert fields["qt:track1:HandlerType"].value == "Video"
    assert (fields["qt:track1:Width"].value, fields["qt:track1:Height"].value) == (1920, 1080)
    assert fields["qt:track1:Rotation"].value == 90
    assert fields["qt:track1:FrameRate"].value == 29.97
    assert fields["qt:track1:Language"].value == "ita"
    assert fields["qt:track1:CodecID"].value == "avc1"
    assert fields["qt:track2:HandlerType"].value == "Audio"
    assert (fields["qt:track2:Channels"].value, fields["qt:track2:SampleRate"].value) == (2, 48000)
    assert any("UTC" in n for n in notes)


def test_quicktime_keys_and_udta(phone):
    fields, _ = _by_key(phone)
    assert fields["qt:mdta:com.apple.quicktime.location.ISO6709"].value == "+45.4642+009.1900+120.500/"
    assert fields["qt:mdta:com.apple.quicktime.model"].value == "iPhone 15 Pro"
    assert fields["qt:mdta:com.apple.quicktime.live-photo.vitality-score"].value == 0.5
    assert fields["qt:mdta:com.apple.quicktime.content.identifier"].value == 7
    assert fields["qt:udta:©xyz"].value == "+45.4642+009.1900+120.500/"


def test_meta_without_full_box_header_as_written_by_phones(tmp_path):
    path = mp4.write_phone_movie(tmp_path / "phone.mov", full_box_meta=False)
    fields, _ = _by_key(path)
    assert fields["qt:mdta:com.apple.quicktime.make"].value == "Apple"
    assert fields["qt:track1:Rotation"].value == 90
    record = HandlerRegistry.get(path).read(path)      # never raises, whatever mutagen thinks of the file
    assert any(f.key == "qt:mdta:com.apple.quicktime.model" for f in record.fields)
    assert record.read_errors == [] and record.format_supports_write


def test_structure_is_shown_when_the_tag_library_cannot_open_the_file(phone, monkeypatch):
    import mutagen.mp4

    def broken(*args, **kwargs):
        raise mutagen.mp4.MP4StreamInfoError("unsupported stream layout")
    monkeypatch.setattr(mutagen.mp4, "MP4", broken)
    record = HandlerRegistry.get(phone).read(phone)
    assert record.read_errors == [] and not record.format_supports_write
    assert record.read_only_reason and any("Tags not read" in w for w in record.warnings)
    assert any(f.key == "qt:track1:Rotation" for f in record.fields)
    assert not any(f.editable or f.deletable for f in record.fields)


def test_xmp_packet_in_uuid_box(phone):
    fields, _ = _by_key(phone)
    assert fields["xmp:xmp:CreatorTool"].value == "Tool & Co"
    assert fields["xmp:dc:creator"].value == ["Ada"]


def test_all_structure_fields_are_read_only(phone):
    fields, _ = qt_fields(phone)
    assert fields and not any(f.editable or f.deletable for f in fields)


def test_handler_serves_structure_and_keeps_tags_writable(phone):
    mp4.add_full_tags(phone)
    handler = HandlerRegistry.get(phone)
    record = handler.read(phone)
    keys = {f.key: f for f in record.fields}
    assert record.read_errors == [] and record.format_supports_write
    assert keys["qt:track1:Rotation"].value == 90 and not keys["qt:track1:Rotation"].editable
    assert keys["mp4:©nam"].editable
    assert any("UTC" in w for w in record.warnings)


def test_m4a_gets_the_structure_too(tmp_path):
    path = mp4.write_mp4(tmp_path / "song.m4a")
    mp4.add_full_tags(path)
    record = HandlerRegistry.get(path).read(path)
    assert any(f.key == "qt:MajorBrand" for f in record.fields)


def test_write_leaves_structure_and_media_untouched(phone):
    mp4.add_full_tags(phone)
    handler = HandlerRegistry.get(phone)
    before = {k: v.value for k, v in _by_key(phone)[0].items()}
    title = next(f for f in handler.read(phone).fields if f.key == "mp4:©nam")
    title.value = ["Changed"]
    handler.write(phone, [title])
    assert next(f for f in handler.read(phone).fields if f.key == "mp4:©nam").value == ["Changed"]
    assert {k: v.value for k, v in _by_key(phone)[0].items()} == before
    assert mp4.mdat_payload(phone) == mp4.MDAT_PAYLOAD


@pytest.mark.parametrize("data", [
    b"", b"\x00\x00\x00", struct.pack(">I4s", 4, b"free"), struct.pack(">I4s", 1 << 30, b"moov"),
    struct.pack(">I4s", 0, b"moov") + b"\xff" * 3,
    struct.pack(">I4s", 1, b"moov") + b"\x00" * 3,
])
def test_damaged_files_never_raise(tmp_path, data):
    path = tmp_path / "bad.mov"
    path.write_bytes(data)
    fields, _ = qt_fields(path)
    assert isinstance(fields, list)


def test_truncated_movie_never_raises(phone, tmp_path):
    data = phone.read_bytes()
    for cut in range(0, len(data), 37):
        path = tmp_path / "cut.mov"
        path.write_bytes(data[:cut])
        qt_fields(path)


def test_box_walk_is_bounded():
    box = struct.pack(">I4s", 8, b"free")
    reader = Reader(box * 200_000)
    assert len(reader.children()) <= 100_000


def test_reader_handles_64_bit_and_zero_sizes(tmp_path):
    big = struct.pack(">I4sQ", 1, b"free", 20) + bytes(4)
    last = struct.pack(">I4s", 0, b"mdat") + b"xyz"
    with open_reader(_write(tmp_path, big + last)) as rd:
        kinds = [(b.type, b.size) for b in rd.children()]
    assert kinds == [(b"free", 20), (b"mdat", 11)]


def _write(tmp_path, data):
    path = tmp_path / "x.bin"
    path.write_bytes(data)
    return path
