"""hachoir fields: stable unique keys, typed raw values, read-only (F2G).

Keys are ``hachoir:<group index>:<item.key>``: group 0 is the root metadata
("Common"/"Metadata"), groups 1..n follow ``MultipleMetadata.iterGroups()``.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from hachoir.metadata.metadata import Metadata, MultipleMetadata, RootMetadata
from hachoir.metadata.metadata_item import DataValue

from core.handlers import hachoir_handler
from core.handlers.hachoir_handler import HachoirHandler, extract_fields, fields_from_metadata
from tests.factories import hachoir as factory


def _by_key(fields):
    keys = [f.key for f in fields]
    assert len(keys) == len(set(keys)), keys
    return {f.key: f for f in fields}


def _two_stream_metadata() -> MultipleMetadata:
    meta = MultipleMetadata()
    meta.duration = timedelta(seconds=90)
    meta.mime_type = "video/x-test"
    video = Metadata(meta)
    video.duration = timedelta(seconds=90, milliseconds=250)
    video.width = 640
    video.frame_rate = 25.0
    meta.addGroup("video[]", video, "Video stream")
    audio = Metadata(meta)
    audio.duration = timedelta(seconds=89)
    audio.sample_rate = 48000
    meta.addGroup("audio[]", audio, "Audio stream")
    return meta


def test_synthetic_groups_give_unique_typed_keys():
    f = _by_key(fields_from_metadata(_two_stream_metadata()))
    assert {k for k in f if k.endswith(":duration")} == \
        {"hachoir:0:duration", "hachoir:1:duration", "hachoir:2:duration"}
    assert f["hachoir:0:duration"].value == 90.0
    assert f["hachoir:1:duration"].value == 90.25
    assert f["hachoir:2:duration"].value == 89.0
    assert all(f[k].field_type == "duration" for k in f if k.endswith(":duration"))
    assert (f["hachoir:1:width"].value, f["hachoir:1:width"].field_type) == (640, "int")
    assert (f["hachoir:1:frame_rate"].value, f["hachoir:1:frame_rate"].field_type) == (25.0, "float")
    assert (f["hachoir:2:sample_rate"].value, f["hachoir:2:sample_rate"].field_type) == (48000, "int")
    assert f["hachoir:0:mime_type"].value == "video/x-test"
    assert f["hachoir:1:duration"].label == "Video stream — Duration"
    assert f["hachoir:0:duration"].label == "Common — Duration"
    assert f["hachoir:1:duration"].raw_value == "1 min 30 sec 250 ms"  # human text kept
    assert all(not x.editable and not x.deletable and x.source == "hachoir" for x in f.values())


def test_keys_do_not_depend_on_labels():
    meta = _two_stream_metadata()
    before = [f.key for f in fields_from_metadata(meta)]
    meta.getItems("duration").description = "Durée"  # localized label
    after = fields_from_metadata(meta)
    assert [f.key for f in after] == before
    assert _by_key(after)["hachoir:0:duration"].label == "Common — Durée"


def test_several_values_become_a_list():
    meta = RootMetadata()
    meta.author = "Anna"
    meta.author = "Bruno"
    f = _by_key(fields_from_metadata(meta))["hachoir:0:author"]
    assert f.multi and f.value == ["Anna", "Bruno"] and f.field_type == "str"


def test_dates_and_other_types():
    meta = RootMetadata()
    meta.creation_date = datetime(2010, 12, 30, 1, 2, 3)
    meta.last_modification = datetime(2011, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    meta.comment = "line 1\nline 2"
    f = _by_key(fields_from_metadata(meta))
    assert (f["hachoir:0:creation_date"].value, f["hachoir:0:creation_date"].field_type) == \
        ("2010-12-30T01:02:03", "localdatetime")
    assert (f["hachoir:0:last_modification"].value, f["hachoir:0:last_modification"].field_type) == \
        ("2011-01-02T03:04:05+00:00", "datetime")
    assert f["hachoir:0:comment"].field_type == "text"


def test_bytes_value_is_binary_and_unknown_objects_use_their_text():
    meta = RootMetadata()
    meta.getItems("comment").values.append(DataValue(b"\x00\x01\x02", "<3 bytes>"))
    meta.getItems("language").values.append(DataValue(object(), "Italian"))
    f = _by_key(fields_from_metadata(meta))
    assert f["hachoir:0:comment"].field_type == "binary"
    assert f["hachoir:0:comment"].value["size"] == 3 and not f["hachoir:0:comment"].editable
    assert (f["hachoir:0:language"].value, f["hachoir:0:language"].field_type) == ("Italian", "str")


def test_mixed_value_types_fall_back_to_text():
    meta = RootMetadata()
    data = meta.getItems("comment")
    data.values += [DataValue(3, "3"), DataValue(timedelta(seconds=1), "1 sec")]
    f = _by_key(fields_from_metadata(meta))["hachoir:0:comment"]
    assert f.multi and f.value == ["3", "1 sec"] and f.field_type == "str"


def test_wav_renamed_bin(tmp_path):
    path = factory.write_wav(tmp_path / "tone.bin")
    record = HachoirHandler().read(path)
    assert record.read_errors == []
    f = _by_key(record.fields)
    assert (f["hachoir:0:duration"].value, f["hachoir:0:duration"].field_type) == (1.0, "duration")
    assert f["hachoir:0:sample_rate"].value == 8000
    assert f["hachoir:0:nb_channel"].value == 1
    assert f["hachoir:0:mime_type"].value == "audio/x-wav"
    assert not record.supports_write and not record.supports_delete
    json.dumps(record.to_dict(), allow_nan=False)


def test_avi_streams_have_distinct_duration_keys(tmp_path):
    path = tmp_path / "clip.avi"
    path.write_bytes(factory.avi_bytes())
    fields, errors = extract_fields(path)
    assert errors == []
    f = _by_key(fields)
    for key in ("hachoir:0:duration", "hachoir:1:duration", "hachoir:2:duration"):
        assert (f[key].value, f[key].field_type) == (1.0, "duration")
    assert f["hachoir:1:width"].value == 64 and f["hachoir:1:height"].value == 48
    assert f["hachoir:2:sample_rate"].value == 8000
    assert f["hachoir:1:duration"].label == "Video stream — Duration"
    assert f["hachoir:2:duration"].label == "Audio stream — Duration"


def test_mkv_info_and_tracks(tmp_path):
    path = tmp_path / "clip.mkv"
    path.write_bytes(factory.mkv_bytes())
    fields, errors = extract_fields(path)
    assert errors == []
    f = _by_key(fields)
    assert f["hachoir:0:title"].value == factory.MKV_TITLE
    assert f["hachoir:0:duration"].value == factory.MKV_DURATION
    assert (f["hachoir:0:creation_date"].value, f["hachoir:0:creation_date"].field_type) == \
        ("2010-12-30T00:00:00", "localdatetime")
    assert f["hachoir:1:language"].value == "Italian"
    assert f["hachoir:2:language"].value == "English"
    assert f["hachoir:2:sample_rate"].value == 8000.0


def test_keys_are_stable_across_reads(tmp_path):
    path = tmp_path / "clip.avi"
    path.write_bytes(factory.avi_bytes())
    first = [(f.key, f.value) for f in extract_fields(path)[0]]
    assert first == [(f.key, f.value) for f in extract_fields(path)[0]]


@pytest.mark.parametrize("name, content", [
    ("noise.xyz", bytes([0x13, 0x37]) * 64),
    ("notes.txt", "Appunti è 日本\nseconda riga\n".encode("utf-8")),
    ("empty.dat", b""),
])
def test_unrecognized_content_is_not_an_error(tmp_path, name, content):
    path = tmp_path / name
    path.write_bytes(content)
    record = HachoirHandler().read(path)
    assert record.read_errors == []
    assert record.fields == []
    assert record.warnings == [hachoir_handler.NO_METADATA]
    # VideoHandler claims formats by extension: for it, unparsable content stays an error.
    assert extract_fields(path) == ([], ["hachoir could not parse file"])


def test_recognized_file_has_no_informative_warning(tmp_path):
    record = HachoirHandler().read(factory.write_wav(tmp_path / "tone.bin"))
    assert record.warnings == [] and record.read_errors == [] and record.fields


def test_real_failures_are_read_errors(tmp_path, monkeypatch):
    noise = tmp_path / "noise.xyz"
    noise.write_bytes(bytes([0x13, 0x37]) * 64)
    import hachoir.parser

    def boom(*_a, **_k):
        raise RuntimeError("boom")
    monkeypatch.setattr(hachoir.parser, "createParser", boom)
    record = HachoirHandler().read(noise)
    assert record.read_errors == ["hachoir read error: boom"] and record.warnings == []
    assert extract_fields(noise) == ([], ["hachoir read error: boom"])


def test_extraction_failure_logged_by_hachoir_is_a_read_error(tmp_path, monkeypatch):
    # hachoir's extractMetadata swallows extractor exceptions and only logs them.
    from hachoir.metadata import riff
    path = factory.write_wav(tmp_path / "tone.bin")

    def broken(self, parser):
        self.duration = timedelta(seconds=1)
        raise RuntimeError("corrupt chunk")
    monkeypatch.setattr(riff.RiffMetadata, "extract", broken)
    record = HachoirHandler().read(path)
    assert any("corrupt chunk" in e and e.startswith("hachoir read error: ")
               for e in record.read_errors), record.read_errors
    assert _by_key(record.fields)["hachoir:0:duration"].value == 1.0  # partial data kept


def test_hachoir_log_noise_is_not_printed(tmp_path, capfd, caplog):
    path = factory.write_wav(tmp_path / "tone.bin")  # probing logs "[warn] Skip parser ..."
    noise = tmp_path / "noise.xyz"
    noise.write_bytes(bytes([0x13, 0x37]) * 64)
    capfd.readouterr()
    with caplog.at_level("DEBUG", logger="hachoir"):
        HachoirHandler().read(path)
        HachoirHandler().read(noise)
    out, err = capfd.readouterr()
    assert "[warn]" not in out + err and "Skip parser" not in out + err
    assert any("Skip parser" in r.getMessage() and r.levelname == "DEBUG"
               for r in caplog.records if r.name == "hachoir")


def test_video_handler_alias_is_kept():
    from core.handlers import video_handler
    assert video_handler._read_hachoir is hachoir_handler.extract_fields


def test_read_only(tmp_path):
    path = factory.write_wav(tmp_path / "tone.bin")
    with pytest.raises(NotImplementedError):
        HachoirHandler().write(path, [])
    with pytest.raises(NotImplementedError):
        HachoirHandler().delete(path, ["hachoir:0:duration"])


def test_record_explains_why_it_is_read_only(tmp_path):
    """B9: a file read by the generic reader tells the user why its format metadata is read-only."""
    path = tmp_path / "unknown.xyz"
    path.write_bytes(b"not a known container")
    record = HachoirHandler().read(path)
    assert record.read_only_reason and "no metadata writer" in record.read_only_reason
