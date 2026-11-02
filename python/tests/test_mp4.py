"""MP4/QuickTime tags (plan F2C) and ``.m4a`` on the same codec (F2B.2, D8).

Samples: ``tests/factories/mp4.py`` (minimal ftyp + moov/mvhd + mdat built in
Python). The generic contract runs on them in ``test_contract.py``.
"""
from __future__ import annotations

import hashlib
import importlib
from dataclasses import replace
from pathlib import Path

import pytest

from core.diff import compute_diff
from core.errors import MetadataValueError
from core.path_security import secure_atomic_write
from core.registry import HandlerRegistry
from tests.factories import by_name
from tests.factories import mp4 as mf

importlib.import_module("core.handlers")


def make(name: str, tmp_path: Path) -> Path:
    return by_name(name).make(tmp_path / name)


def fields(path: Path):
    return {f.key: f for f in HandlerRegistry.get(path).read(path).fields}


def write(path: Path, key: str, value) -> None:
    handler = HandlerRegistry.get(path)
    schema = fields(path).get(key) or handler.writable_field(path, key)
    assert schema is not None, key
    handler.write(path, [replace(schema, value=value)])


def tags(path: Path):
    from mutagen.mp4 import MP4
    return MP4(path).tags


@pytest.fixture(params=["m4a_tags", "mp4_tags"])
def tagged(request, tmp_path):
    return make(request.param, tmp_path)


def test_text_atoms_are_lists(tagged):
    f = fields(tagged)
    assert f["mp4:\xa9ART"].value == ["Artist A", "Artist B"] and f["mp4:\xa9ART"].multi
    write(tagged, "mp4:\xa9ART", ["X", "Y", "Z"])
    assert tags(tagged)["\xa9ART"] == ["X", "Y", "Z"]


def test_track_and_disc_are_int_pairs(tagged):
    f = fields(tagged)["mp4:trkn"]
    assert (f.value, f.field_type, f.multi, f.length) == ([3, 10], "int", True, 2)
    write(tagged, "mp4:trkn", [4, 10])
    assert tags(tagged)["trkn"] == [(4, 10)]
    assert fields(tagged)["mp4:disk"].value == [1, 2]
    for bad in ([70000, 1], [1], "4/10", [1, 2, 3]):
        with pytest.raises(MetadataValueError):
            write(tagged, "mp4:trkn", bad)


def test_tempo_is_int_and_accepts_strict_numeric_string(tagged):
    f = fields(tagged)["mp4:tmpo"]
    assert (f.value, f.field_type, f.multi) == (120, "int", False)
    write(tagged, "mp4:tmpo", "121")
    assert tags(tagged)["tmpo"] == [121]
    for bad in ("12a", 1.5, 40000, -1, True):
        with pytest.raises(MetadataValueError):
            write(tagged, "mp4:tmpo", bad)


def test_compilation_false_stays_false(tagged):
    """Bug 7: cpil was written as [False] and mutagen stored bool([False]) == True."""
    f = fields(tagged)["mp4:cpil"]
    assert (f.value, f.field_type) == (False, "bool")
    write(tagged, "mp4:cpil", False)
    assert tags(tagged)["cpil"] is False
    write(tagged, "mp4:cpil", "true")
    assert tags(tagged)["cpil"] is True
    write(tagged, "mp4:cpil", "False")
    assert tags(tagged)["cpil"] is False


def test_cover_is_binary_read_only_and_restorable(tagged):
    handler = HandlerRegistry.get(tagged)
    f = fields(tagged)["mp4:covr"]
    assert f.field_type == "binary" and f.multi and not f.editable and f.deletable
    assert f.value == [{"size": len(mf.png_bytes()), "mime": "image/png",
                        "sha256": hashlib.sha256(mf.png_bytes()).hexdigest()}]
    with pytest.raises(MetadataValueError):
        handler.write(tagged, [replace(f, value=f.value)])
    snap = handler.snapshot(tagged, ["mp4:covr"])
    handler.delete(tagged, ["mp4:covr"])
    assert "covr" not in tags(tagged)
    secure_atomic_write(tagged, lambda tmp: handler.restore(tmp, snap))
    cover = tags(tagged)["covr"][0]
    assert bytes(cover) == mf.png_bytes() and cover.imageformat == cover.FORMAT_PNG


def test_freeform_utf8_is_editable_and_binary_is_not(tagged):
    from mutagen.mp4 import AtomDataType
    f = fields(tagged)
    mood = f["mp4:----:com.apple.iTunes:MOOD"]
    assert (mood.value, mood.field_type, mood.editable) == (["calm é"], "str", True)
    blob = f["mp4:----:com.apple.iTunes:BLOB"]
    assert blob.field_type == "binary" and not blob.editable and blob.deletable
    write(tagged, "mp4:----:com.apple.iTunes:MOOD", ["happy €", "second"])
    stored = tags(tagged)["----:com.apple.iTunes:MOOD"]
    assert [bytes(v).decode() for v in stored] == ["happy €", "second"]
    assert all(v.dataformat == AtomDataType.UTF8 for v in stored)
    write(tagged, "mp4:----:org.example:NEW", ["added"])
    assert fields(tagged)["mp4:----:org.example:NEW"].value == ["added"]


@pytest.mark.parametrize("value", ["", None, [""], [], "single string"])
def test_invalid_text_values_rejected(tagged, value):
    before = tagged.read_bytes()
    with pytest.raises(MetadataValueError):
        write(tagged, "mp4:\xa9nam", value)
    assert tagged.read_bytes() == before


def test_media_data_identical_after_writes(tagged):
    assert mf.mdat_payload(tagged) == mf.MDAT_PAYLOAD
    write(tagged, "mp4:\xa9nam", ["A long title " * 300])
    HandlerRegistry.get(tagged).delete(tagged, ["mp4:covr", "mp4:trkn"])
    assert mf.mdat_payload(tagged) == mf.MDAT_PAYLOAD


def test_mov_without_tags_accepts_title(tmp_path):
    path = make("mov", tmp_path)
    handler = HandlerRegistry.get(path)
    record = handler.read(path)
    assert record.supports_write and not record.read_errors
    addable = {f.key for f in handler.writable_fields(path, record)}
    assert {"mp4:\xa9nam", "mp4:trkn", "mp4:cpil", "mp4:tmpo"} <= addable
    write(path, "mp4:\xa9nam", ["Movie"])
    assert fields(path)["mp4:\xa9nam"].value == ["Movie"]
    assert mf.mdat_payload(path) == mf.MDAT_PAYLOAD


def test_unknown_atoms_not_addable(tmp_path):
    path = make("mov", tmp_path)
    handler = HandlerRegistry.get(path)
    for key in ("mp4:zzzz", "mp4:covr", "mp4:----:onlymean", "mp4:----::name", "tag:TIT2"):
        assert handler.writable_field(path, key) is None, key


@pytest.mark.asyncio
async def test_api_mp4_typed_write(client, tmp_path):
    path = make("mp4_tags", tmp_path)
    resp = await client.post("/write", json={"path": str(path), "fields": [
        {"key": "mp4:trkn", "value": [5, 12]}, {"key": "mp4:cpil", "value": True},
        {"key": "mp4:\xa9ART", "value": ["One", "Two"]}]})
    assert resp.status_code == 200, resp.text
    f = fields(path)
    assert (f["mp4:trkn"].value, f["mp4:cpil"].value, f["mp4:\xa9ART"].value) == \
        ([5, 12], True, ["One", "Two"])
    resp = await client.post("/write", json={"path": str(path),
                                             "fields": [{"key": "mp4:tmpo", "value": None}]})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_undo_of_the_first_tag_of_a_mov_leaves_no_atom(client, tmp_path):
    """Review item 11c. mutagen inserts moov.udta.meta (hdlr + ilst + free padding) and
    shifts the chunk offsets and has no API to take the meta atom out again; the Undo
    gives the original bytes back from the journal, so the file is byte-identical."""
    path = make("mov", tmp_path)
    original = path.read_bytes()
    resp = await client.post("/write", json={"path": str(path),
                                             "fields": [{"key": "mp4:©nam", "value": ["Movie"]}]})
    assert resp.status_code == 200, resp.text
    assert _children(path.read_bytes(), b"moov") == [b"udta", b"mvhd"]
    resp = await client.post("/undo", json={"token": resp.json()["undo_token"]})
    assert resp.status_code == 200, resp.text
    assert path.read_bytes() == original
    assert _children(path.read_bytes(), b"moov") == [b"mvhd"]


def _atoms(data: bytes) -> list[tuple[bytes, bytes]]:
    """(name, payload) of the atoms in ``data`` (32-bit sizes only, enough here)."""
    import struct
    out, pos = [], 0
    while pos + 8 <= len(data):
        size, name = struct.unpack(">I4s", data[pos:pos + 8])
        out.append((name, data[pos + 8:pos + size]))
        pos += size
    return out


def _children(data: bytes, parent: bytes) -> list[bytes]:
    return [n for n, _ in _atoms(dict(_atoms(data))[parent])]


def test_stored_integer_outside_the_atom_range_is_read_only(tagged):
    """mutagen reads integer atoms as SIGNED: a 4-byte cnID 3000000000 comes back
    negative. The value cannot be written back as read (it is outside the range the
    codec writes), so the field must not be editable (G1); it stays deletable."""
    from mutagen.mp4 import MP4
    mp4 = MP4(tagged)
    mp4.tags["cnID"] = [-1294967296]
    mp4.tags["tmpo"] = [-25536]
    mp4.save()
    handler = HandlerRegistry.get(tagged)
    f = fields(tagged)
    for key in ("mp4:cnID", "mp4:tmpo"):
        assert not f[key].editable and f[key].deletable, key
    snap = handler.snapshot(tagged, ["mp4:cnID"])
    handler.delete(tagged, ["mp4:cnID"])
    secure_atomic_write(tagged, lambda tmp: handler.restore(tmp, snap))
    assert MP4(tagged).tags["cnID"] == [-1294967296]


@pytest.mark.asyncio
async def test_api_integer_above_2_53_is_exact_both_ways(client, tmp_path):
    """Review item 14a: Python serialises every digit (JavaScript's JSON.parse is what
    rounds above 2**53 - 1); /write accepts the decimal string an editor can send."""
    from mutagen.mp4 import MP4
    path = make("mp4_tags", tmp_path)
    big = 2**60 + 3
    resp = await client.post("/write", json={"path": str(path),
                                             "fields": [{"key": "mp4:plID", "value": str(big)}]})
    assert resp.status_code == 200, resp.text
    assert MP4(path).tags["plID"] == [big]
    resp = await client.get("/read", params={"path": str(path)})
    assert f'"value":{big}' in resp.text.replace(" ", "")
    assert {f["key"]: f for f in resp.json()["fields"]}["mp4:plID"]["value"] == big
    resp = await client.post("/write", json={"path": str(path),
                                             "fields": [{"key": "mp4:plID", "value": 2**63 - 1}]})
    assert resp.status_code == 200, resp.text
    assert MP4(path).tags["plID"] == [2**63 - 1]


# ──────────────────────── F2B.2: .m4a on the MP4 codec ───────────────────────

def test_m4a_uses_mp4_keys_and_keeps_audio_info(tmp_path):
    path = make("m4a_tags", tmp_path)
    record = HandlerRegistry.get(path).read(path)
    assert record.handler_name == "audio"
    keys = {f.key for f in record.fields}
    assert not any(k.startswith("tag:") for k in keys)
    assert {"mp4:\xa9nam", "mp4:trkn", "mp4:covr", "audio:duration"} <= keys


def test_mp4_video_info_is_not_mislabelled(tmp_path):
    path = make("mp4_tags", tmp_path)
    record = HandlerRegistry.get(path).read(path)
    assert record.handler_name == "video"
    keys = {f.key for f in record.fields}
    assert "video:duration" in keys and "video:bitrate" not in keys
    assert fields(path)["video:duration"].field_type == "duration"


def test_diff_m4a_vs_mp4_same_atoms_has_no_tag_differences(tmp_path):
    a = make("m4a_tags", tmp_path)
    b = make("mp4_tags", tmp_path)
    diff = compute_diff(HandlerRegistry.get(a).read(a), HandlerRegistry.get(b).read(b))
    tag_keys = lambda fs: {f.key for f in fs if f.key.startswith("mp4:")}  # noqa: E731
    assert not tag_keys(diff.only_in_a) and not tag_keys(diff.only_in_b)
    assert not {fa.key for fa, _ in diff.changed if fa.key.startswith("mp4:")}
    assert len(tag_keys(diff.identical)) == 10
