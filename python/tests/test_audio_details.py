"""Stream details and picture labels of audio files."""
from core.registry import HandlerRegistry
from tests.factories import by_name


def _keys(tmp_path, name):
    folder = tmp_path / name
    folder.mkdir()
    path = by_name(name).build(folder)
    record = HandlerRegistry.get(path).read(path)
    assert record.read_errors == []
    return {f.key: f for f in record.fields}


def test_mp3_stream_details(tmp_path):
    keys = _keys(tmp_path, "mp3_v24_full")
    assert keys["audio:mpeg_layer"].value == 3
    assert keys["audio:mpeg_version"].value in ("1", "2", "2.5", "1.0", "2.0")
    assert keys["audio:channel_mode"].value in ("Stereo", "Joint stereo", "Dual channel", "Mono")
    assert isinstance(keys["audio:crc_protected"].value, bool)
    assert not keys["audio:mpeg_layer"].editable


def test_picture_label_names_type_and_description(tmp_path):
    keys = _keys(tmp_path, "mp3_v24_full")
    label = keys["tag:APIC:cover"].label
    assert "Picture" in label and "cover" in label.lower()


def test_flac_stream_info(tmp_path):
    keys = _keys(tmp_path, "flac_vorbis_picture")
    assert keys["audio:total_samples"].value > 0
    assert keys["audio:max_blocksize"].value >= keys["audio:min_blocksize"].value
    assert len(keys["audio:md5_signature"].value) == 32 if "audio:md5_signature" in keys else True


def test_vorbis_vendor_is_shown_and_computed(tmp_path):
    keys = _keys(tmp_path, "ogg_vorbis")
    assert keys["audio:vendor"].computed and not keys["audio:vendor"].editable
