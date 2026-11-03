"""Classification of fields by what they reveal when the file is shared."""
import pytest

from core import sensitivity
from core.registry import HandlerRegistry
from tests.factories import ALL


@pytest.mark.parametrize("key, expected", [
    ("gps:Latitude", "location"), ("xmp:photoshop:City", "location"), ("iptc:Sub-location", "location"),
    ("qt:mdta:com.apple.quicktime.location.ISO6709", "location"), ("qt:udta:©xyz", "location"),
    ("exif:Artist", "identity"), ("pdf:Author", "identity"), ("office:last_modified_by", "identity"),
    ("odf:initial-creator", "identity"), ("fs:owner", "identity"), ("xmp:dc:creator", "identity"),
    ("exif:Make", "device"), ("exif:BodySerialNumber", "device"), ("qt:mdta:com.apple.quicktime.model", "device"),
    ("exif:DateTimeOriginal", "datetime"), ("pdf:CreationDate", "datetime"), ("qt:CreateDate", "datetime"),
    ("qt:track1:CreateDate", "datetime"), ("fs:mtime", "datetime"),
    ("exif:Software", "software"), ("pdf:Producer", "software"), ("xmp:xmpMM:History", "software"),
    ("exif:Thumbnail", "thumbnail"), ("exif:UserComment", "text"), ("jpeg:Comment", "text"),
    ("img:width", None), ("exif:Orientation", None), ("audio:bitrate", None), ("icc:Version", None),
])
def test_classification(key, expected):
    assert sensitivity.classify(key) == expected


def test_every_rule_names_a_known_category_and_severity():
    assert {c for c, _ in sensitivity._RULES} <= set(sensitivity.CATEGORIES) == set(sensitivity.SEVERITY)


def test_summary_is_ordered_by_severity():
    summary = sensitivity.summarize(["exif:DateTime", "gps:Latitude", "gps:Longitude", "exif:Make", "img:width"])
    assert summary == {"location": 2, "device": 1, "datetime": 1}
    assert list(summary)[0] == "location"


def test_records_and_fields_carry_the_classification(tmp_path):
    sample = next(s for s in ALL if s.name == "jpeg_camera")
    folder = tmp_path / "s"
    folder.mkdir()
    path = sample.build(folder)
    data = HandlerRegistry.get(path).read(path).to_dict()
    by_key = {f["key"]: f for f in data["fields"]}
    assert by_key["gps:Latitude"]["sensitivity"] == "location"
    assert by_key["img:width"]["sensitivity"] is None
    assert data["sensitivity_summary"]["location"] >= 2
    assert next(iter(data["sensitivity_summary"])) in ("location", "identity")


def test_all_sample_keys_classify_without_error():
    # No key of any sample may crash the classifier; names with odd characters included.
    for sample in ALL:
        if not sample.available:
            continue
        sensitivity.classify(sample.name)
