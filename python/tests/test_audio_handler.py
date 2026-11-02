"""Audio tags other than MP4 (plan F2B.1): ID3, Vorbis/FLAC, ASF, APEv2, AAC.

Samples come from ``tests/factories/audio.py`` (built at runtime); the generic
contract (idempotence, mutation, delete + restore) runs on them in
``test_contract.py``. These tests pin the format-specific behaviour.
"""
from __future__ import annotations

import hashlib
import importlib
import shutil
import subprocess
import wave
from dataclasses import replace
from pathlib import Path

import pytest

from api import routes
from core.base_handler import ABSENT
from core.errors import ErrorCode, MetadataValueError
from core.models import MetadataField
from core.path_security import secure_atomic_write
from core.registry import HandlerRegistry
from tests.factories import by_name
from tests.factories import audio as af

importlib.import_module("core.handlers")


def make(name: str, tmp_path: Path) -> Path:
    return by_name(name).make(tmp_path / name)


def fields(path: Path) -> dict[str, MetadataField]:
    return {f.key: f for f in HandlerRegistry.get(path).read(path).fields}


def write(path: Path, key: str, value) -> None:
    handler = HandlerRegistry.get(path)
    record = handler.read(path)
    current = {f.key: f for f in record.fields}
    schema = current.get(key) or handler.writable_field(path, key)
    assert schema is not None, key
    handler.write(path, [replace(schema, value=value)])


def id3_of(path: Path):
    import mutagen
    return mutagen.File(path).tags


def mp3_audio_bytes(path: Path) -> bytes:
    """Everything after the ID3v2 tag (the MPEG frames)."""
    data = path.read_bytes()
    if data[:3] == b"ID3":
        size = 0
        for b in data[6:10]:
            size = (size << 7) | b
        data = data[10 + size:]
    return data


# ──────────────────────── ID3: value model ───────────────────────────────────

def test_id3_text_frames_are_lists_not_joined(tmp_path):
    path = make("mp3_v24_full", tmp_path)
    f = fields(path)
    assert f["tag:TPE1"].value == ["Artist A", "Artist B"] and f["tag:TPE1"].multi
    assert f["tag:TCON"].value == ["Rock", "Jazz"]
    assert f["tag:COMM:note:eng"].value == ["First", "Second"]
    assert f["tag:TXXX:MOOD"].value == ["calm"]
    write(path, "tag:TPE1", ["X", "Y", "Z"])
    assert id3_of(path)["TPE1"].text == ["X", "Y", "Z"]
    assert fields(path)["tag:TPE1"].value == ["X", "Y", "Z"]


def test_id3_uslt_keeps_the_whole_text(tmp_path):
    path = make("mp3_v24_full", tmp_path)
    f = fields(path)["tag:USLT::eng"]
    assert (f.value, f.field_type, f.multi) == ("Line one\nLine two\nLine three", "text", False)
    write(path, "tag:USLT::eng", "New\nlyrics")
    assert id3_of(path)["USLT::eng"].text == "New\nlyrics"


def test_id3_binary_and_structured_frames_are_read_only_but_deletable(tmp_path):
    path = make("mp3_v24_full", tmp_path)
    f = fields(path)
    for key in ("tag:APIC:cover", "tag:GEOB:blob", "tag:PRIV:com.example",
                "tag:UFID:http://musicbrainz.org"):
        assert f[key].field_type == "binary" and not f[key].editable and f[key].deletable, key
    apic = f["tag:APIC:cover"]
    assert apic.value == {"size": len(af.png_bytes()), "mime": "image/png",
                          "sha256": hashlib.sha256(af.png_bytes()).hexdigest()}
    assert f["tag:POPM:user@example.org"].value == [196, 3]
    assert f["tag:PCNT"].value == 7
    assert f["tag:SYLT:synced:eng"].value == ["1000: Hello", "2000: World"]
    assert f["tag:TIPL"].value == ["producer: P. Name"]
    for key in ("tag:POPM:user@example.org", "tag:PCNT", "tag:SYLT:synced:eng", "tag:TIPL"):
        assert not f[key].editable and f[key].deletable, key
        with pytest.raises(MetadataValueError):
            HandlerRegistry.get(path).write(path, [replace(f[key], value=f[key].value)])


def test_id3_binary_delete_and_restore_is_byte_exact(tmp_path):
    path = make("mp3_v24_full", tmp_path)
    handler = HandlerRegistry.get(path)
    keys = ["tag:APIC:cover", "tag:PRIV:com.example", "tag:POPM:user@example.org",
            "tag:SYLT:synced:eng", "tag:TIPL", "tag:WOAR"]
    before = {k: v for k, v in id3_of(path).items()}
    snaps = []
    secure_atomic_write(path, lambda tmp: (snaps.append(handler.snapshot(tmp, keys)),
                                           handler.apply_delete(tmp, keys)))
    assert not set(keys) & set(fields(path))
    assert "APIC:cover" not in id3_of(path)
    secure_atomic_write(path, lambda tmp: handler.restore(tmp, snaps[0]))
    after = id3_of(path)
    assert after["APIC:cover"].data == before["APIC:cover"].data
    assert after["APIC:cover"].mime == "image/png" and after["APIC:cover"].type == 3
    assert after["PRIV:com.example:\x00\x01private"].data == b"\x00\x01private"
    assert after["POPM:user@example.org"].rating == 196
    assert after["SYLT:synced:eng"].text == [("Hello", 1000), ("World", 2000)]
    assert handler.snapshot(path, keys) == snaps[0]


def test_id3_url_frames_grouped_by_frame_id_and_replaced(tmp_path):
    path = make("mp3_v24_full", tmp_path)
    f = fields(path)
    assert "tag:WOAR" in f and not any(k.startswith("tag:WOAR:") for k in f)
    assert f["tag:WOAR"].value == ["https://example.org/a", "https://example.org/b"]
    write(path, "tag:WOAR", ["https://example.org/new"])
    assert [fr.url for fr in id3_of(path).getall("WOAR")] == ["https://example.org/new"]
    with pytest.raises(MetadataValueError):
        write(path, "tag:WOAR", ["https://example.org/€"])  # URL frames are Latin-1


def test_id3_url_list_with_duplicates_is_rejected_upfront(tmp_path):
    """ID3v2.4 allows several WOAR/WCOM frames 'but not with the same content': a
    duplicated URL used to fail only at the read-after-write check (lossy_write)."""
    path = make("mp3_v24_full", tmp_path)
    before = path.read_bytes()
    with pytest.raises(MetadataValueError) as exc:
        write(path, "tag:WOAR", ["https://example.org/x", "https://example.org/x"])
    assert (exc.value.key, exc.value.code) == ("tag:WOAR", ErrorCode.INVALID_VALUE)
    assert path.read_bytes() == before


def test_id3_priv_key_does_not_contain_the_data(tmp_path):
    path = make("mp3_v24_full", tmp_path)
    assert [k for k in fields(path) if k.startswith("tag:PRIV")] == ["tag:PRIV:com.example"]


@pytest.mark.parametrize("value", ["", [""], [], None, ["a\x00b"], ["\ud800"]])
def test_id3_rejects_empty_null_nul_and_unencodable(tmp_path, value):
    path = make("mp3_v24_full", tmp_path)
    before = path.read_bytes()
    with pytest.raises(MetadataValueError):
        write(path, "tag:TIT2", value)
    assert path.read_bytes() == before


def test_id3_timestamp_frames_are_validated(tmp_path):
    path = make("mp3_v24_full", tmp_path)
    with pytest.raises(MetadataValueError):
        write(path, "tag:TDRC", ["garbage"])
    write(path, "tag:TDRC", ["2027-05-06T07:08"])
    assert fields(path)["tag:TDRC"].value == ["2027-05-06T07:08"]


def test_id3_latin1_frame_is_upgraded_for_non_latin1_text(tmp_path):
    """N4: '€' in an ISO-8859-1 frame must not fail (it used to be a 500)."""
    import mutagen.id3 as id3
    path = make("mp3_v24_full", tmp_path)
    assert id3_of(path)["TRCK"].encoding == id3.Encoding.LATIN1
    write(path, "tag:TRCK", ["3/10 €"])
    frame = id3_of(path)["TRCK"]
    assert frame.text == ["3/10 €"] and frame.encoding == id3.Encoding.UTF8


def test_id3_v23_is_preserved_and_upgraded_to_utf16(tmp_path):
    """D14/N5: saving keeps ID3v2.3; Latin-1 frames upgrade to UTF-16 (not UTF-8)."""
    import mutagen.id3 as id3
    path = make("mp3_v23_latin1", tmp_path)
    assert id3_of(path).version == (2, 3, 0)
    assert fields(path)["tag:TPE1"].value == ["Jos\xe9"]
    write(path, "tag:TPE1", ["José €", "Second"])
    tags = id3_of(path)
    assert tags.version == (2, 3, 0)
    assert tags["TPE1"].text == ["José €", "Second"]
    assert tags["TPE1"].encoding == id3.Encoding.UTF16
    assert tags["TIT2"].encoding == id3.Encoding.LATIN1      # untouched frame keeps Latin-1
    assert path.read_bytes()[:4] == b"ID3\x03"
    assert fields(path)["tag:TDRC"].value == ["2026"]           # TYER round trip


def test_id3_v23_rejects_v24_only_frames(tmp_path):
    path = make("mp3_v23_latin1", tmp_path)
    handler = HandlerRegistry.get(path)
    assert handler.writable_field(path, "tag:TMOO") is None
    assert "tag:TMOO" not in {f.key for f in handler.writable_fields(path, handler.read(path))}


def test_id3_dynamic_keys(tmp_path):
    path = make("mp3_untagged", tmp_path)
    handler = HandlerRegistry.get(path)
    for key in ("tag:TXXX:My Key", "tag:COMM:desc:ita", "tag:USLT::eng", "tag:WXXX:site",
                "tag:TIT2", "tag:WOAR"):
        assert handler.writable_field(path, key) is not None, key
    for key in ("tag:ZZZZ", "tag:COMM:nolang", "tag:COMM:x:toolong", "tag:TIT2:x",
                "tag:APIC:cover", "tag:PCNT", "mp4:\xa9nam"):
        assert handler.writable_field(path, key) is None, key
    write(path, "tag:TXXX:My Key", ["v1", "v2"])
    write(path, "tag:COMM:desc:ita", ["commento"])
    f = fields(path)
    assert f["tag:TXXX:My Key"].value == ["v1", "v2"]
    assert f["tag:COMM:desc:ita"].value == ["commento"]
    with pytest.raises(MetadataValueError):
        handler.write(path, [MetadataField("tag:ZZZZ", "x", ["v"], multi=True)])


def test_id3_write_leaves_mpeg_frames_identical(tmp_path):
    path = make("mp3_v24_full", tmp_path)
    audio = mp3_audio_bytes(path)
    assert audio == af.mp3_stream()
    write(path, "tag:TIT2", ["A much longer title " * 20])
    HandlerRegistry.get(path).delete(path, ["tag:APIC:cover"])
    assert mp3_audio_bytes(path) == audio


@pytest.mark.parametrize("name", ["wav_untagged", "aiff_untagged", "mp3_untagged"])
def test_id3_tags_can_be_added_to_untagged_files(tmp_path, name):
    path = make(name, tmp_path)
    handler = HandlerRegistry.get(path)
    record = handler.read(path)
    assert record.supports_write and not record.read_errors
    addable = {f.key for f in handler.writable_fields(path, record)}
    assert {"tag:TIT2", "tag:TPE1", "tag:COMM::eng"} <= addable
    write(path, "tag:TIT2", ["Added"])
    assert fields(path)["tag:TIT2"].value == ["Added"]
    if name == "wav_untagged":
        with wave.open(str(path), "rb") as w:
            assert w.readframes(w.getnframes()) == af.PCM


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["wav_untagged", "aiff_untagged"])
async def test_api_adds_tag_to_untagged_wav_and_aiff(client, tmp_path, name):
    path = make(name, tmp_path)
    resp = await client.post("/write", json={"path": str(path),
                                             "fields": [{"key": "tag:TIT2", "value": ["T"]}]})
    assert resp.status_code == 200, resp.text
    assert fields(path)["tag:TIT2"].value == ["T"]


@pytest.mark.asyncio
async def test_api_euro_in_latin1_frame_is_200_not_500(client, tmp_path):
    path = make("mp3_v23_latin1", tmp_path)
    resp = await client.post("/write", json={"path": str(path),
                                             "fields": [{"key": "tag:TIT2", "value": ["Café €"]}]})
    assert resp.status_code == 200, resp.text
    assert fields(path)["tag:TIT2"].value == ["Café €"]


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["", None, [""], "plain string"])
async def test_api_rejects_invalid_values_with_422(client, tmp_path, value):
    path = make("mp3_v24_full", tmp_path)
    before = path.read_bytes()
    resp = await client.post("/write", json={"path": str(path),
                                             "fields": [{"key": "tag:TIT2", "value": value}]})
    assert resp.status_code == 422, resp.text
    assert isinstance(resp.json()["detail"], str)
    assert path.read_bytes() == before


@pytest.mark.asyncio
async def test_api_read_only_frames_rejected(client, tmp_path):
    path = make("mp3_v24_full", tmp_path)
    for key, value in (("tag:POPM:user@example.org", [1, 1]), ("tag:PCNT", 8),
                       ("tag:APIC:cover", "x")):
        resp = await client.post("/write", json={"path": str(path),
                                                 "fields": [{"key": key, "value": value}]})
        assert resp.status_code == 422, (key, resp.text)


# ──────────────────────── Vorbis / FLAC / Ogg ────────────────────────────────

def test_flac_vorbis_multivalue_and_source(tmp_path):
    path = make("flac_vorbis_picture", tmp_path)
    f = fields(path)
    assert f["tag:artist"].value == ["First", "Second"]
    assert all(f[k].source == "vorbis" for k in ("tag:title", "tag:artist", "tag:genre"))
    assert f["flac:picture"].field_type == "binary" and not f["flac:picture"].editable
    write(path, "tag:artist", ["Uno", "Due", "Tre"])
    from mutagen.flac import FLAC
    flac = FLAC(path)
    assert flac.tags["artist"] == ["Uno", "Due", "Tre"]
    assert [k for k, _ in flac.tags if k.lower() == "artist"] == ["ARTIST"] * 3  # case kept
    assert flac.pictures[0].data == af.png_bytes()


def test_flac_picture_delete_and_restore(tmp_path):
    path = make("flac_vorbis_picture", tmp_path)
    handler = HandlerRegistry.get(path)
    snap = handler.snapshot(path, ["flac:picture"])
    handler.delete(path, ["flac:picture"])
    from mutagen.flac import FLAC
    assert FLAC(path).pictures == []
    secure_atomic_write(path, lambda tmp: handler.restore(tmp, snap))
    pic = FLAC(path).pictures[0]
    assert (pic.data, pic.mime, pic.desc, pic.type) == (af.png_bytes(), "image/png", "front", 3)


def test_flac_audio_frames_identical_after_write(tmp_path):
    path = make("flac_vorbis_picture", tmp_path)
    tail = af.PCM[:2048]
    assert path.read_bytes().endswith(b"\xff\xf8" + tail)
    write(path, "tag:title", ["x" * 5000])
    assert path.read_bytes().endswith(b"\xff\xf8" + tail)


def test_flac_without_tags_gets_vorbis_comment(tmp_path):
    path = make("flac_untagged", tmp_path)
    write(path, "tag:title", ["New"])
    f = fields(path)["tag:title"]
    assert (f.value, f.source) == (["New"], "vorbis")


def test_ogg_picture_is_binary_and_tags_multivalue(tmp_path):
    path = make("ogg_vorbis", tmp_path)
    f = fields(path)
    assert f["tag:artist"].value == ["X", "Y"]
    pic = f["tag:metadata_block_picture"]
    assert pic.field_type == "binary" and not pic.editable and pic.deletable
    assert pic.value[0]["sha256"] == hashlib.sha256(af.png_bytes()).hexdigest()
    write(path, "tag:artist", ["Z"])
    import mutagen
    assert mutagen.File(path).tags["artist"] == ["Z"]


def test_vorbis_invalid_key_rejected(tmp_path):
    path = make("ogg_opus", tmp_path)
    handler = HandlerRegistry.get(path)
    assert handler.writable_field(path, "tag:bad=key") is None
    assert handler.writable_field(path, "tag:UPPER") is None   # keys are lower-case
    assert handler.writable_field(path, "tag:mood") is not None


# ──────────────────────── APEv2 ──────────────────────────────────────────────

def test_ape_text_split_binary_external_and_source(tmp_path):
    path = make("wv_apev2", tmp_path)
    f = fields(path)
    assert f["tag:Artist"].value == ["One", "Two"]
    assert f["tag:Related"].value == "https://example.org/rel" and not f["tag:Related"].multi
    cover = f["tag:Cover Art (Front)"]
    assert cover.field_type == "binary" and not cover.editable and cover.deletable
    assert {x.source for x in f.values() if x.key.startswith("tag:")} == {"ape"}
    write(path, "tag:Artist", ["A", "B", "C"])
    from mutagen.apev2 import APEv2, EXTERNAL
    tags = APEv2(path)
    assert list(tags["Artist"]) == ["A", "B", "C"]
    write(path, "tag:Related", "https://example.org/x")
    assert APEv2(path)["Related"].kind == EXTERNAL
    assert HandlerRegistry.get(path).writable_field(path, "tag:artist") is None  # case clash


# ──────────────────────── AAC: read-only ─────────────────────────────────────

@pytest.mark.asyncio
async def test_aac_is_read_only(client, tmp_path):
    path = make("aac_adts", tmp_path)
    handler = HandlerRegistry.get(path)
    record = handler.read(path)
    assert not record.format_supports_write and not record.format_supports_delete
    assert record.read_only_reason
    assert type(record.fields[1].value) is int  # bitrate (mutagen gives a float)
    assert handler.writable_fields(path, record) == []
    before = path.read_bytes()
    with pytest.raises(MetadataValueError):
        handler.apply_write(path, [MetadataField("tag:TIT2", "Title", ["x"], multi=True, source="id3")])
    resp = await client.post("/write", json={"path": str(path),
                                             "fields": [{"key": "tag:TIT2", "value": ["x"]}]})
    assert resp.status_code == 422
    assert path.read_bytes() == before


# ──────────────────────── ASF codec (in memory) ──────────────────────────────

class _FakeAsf:
    def __init__(self, tags):
        self.tags = tags

    def add_tags(self):  # pragma: no cover - ASF always has tags
        raise AssertionError


def _asf_codec():
    import mutagen.asf as asf
    from core.handlers._tag_codecs import AsfCodec
    tags = asf.ASFTags()
    tags.append(("Title", asf.ASFUnicodeAttribute("Song", language=1, stream=2)))
    tags.append(("WM/TrackNumber", asf.ASFDWordAttribute(5)))
    tags.append(("WM/Big", asf.ASFQWordAttribute(2**40)))
    tags.append(("WM/Flag", asf.ASFBoolAttribute(False)))
    tags.append(("WM/Genre", asf.ASFUnicodeAttribute("Rock")))
    tags.append(("WM/Genre", asf.ASFUnicodeAttribute("Pop")))
    picture = b"\x03" + (10).to_bytes(4, "little") + "image/png".encode("utf-16-le") + b"\x00\x00" \
        + b"\x00\x00" + b"0123456789"
    tags.append(("WM/Picture", asf.ASFByteArrayAttribute(picture)))
    return AsfCodec(_FakeAsf(tags)), tags, picture


def test_asf_codec_types():
    codec, _, picture = _asf_codec()
    f = {x.key: x for x in codec.fields()}
    assert (f["tag:Title"].value, f["tag:Title"].field_type) == (["Song"], "str")
    assert (f["tag:WM/TrackNumber"].value, f["tag:WM/TrackNumber"].field_type) == ([5], "int")
    assert f["tag:WM/Big"].value == [2**40]
    assert (f["tag:WM/Flag"].value, f["tag:WM/Flag"].field_type) == ([False], "bool")
    assert f["tag:WM/Genre"].value == ["Rock", "Pop"]
    pic = f["tag:WM/Picture"]
    assert pic.field_type == "binary" and not pic.editable and pic.deletable
    assert pic.value[0]["mime"] == "image/png" and pic.value[0]["size"] == len(picture)


def test_asf_codec_write_keeps_class_language_and_stream():
    import mutagen.asf as asf
    codec, tags, _ = _asf_codec()
    f = {x.key: x for x in codec.fields()}
    codec.write(f["tag:Title"], codec.validate(f["tag:Title"], ["New"]))
    title = tags["Title"][0]
    assert (type(title), title.value, title.language, title.stream) == \
        (asf.ASFUnicodeAttribute, "New", 1, 2)
    codec.write(f["tag:WM/TrackNumber"], codec.validate(f["tag:WM/TrackNumber"], ["7"]))
    assert type(tags["WM/TrackNumber"][0]) is asf.ASFDWordAttribute and tags["WM/TrackNumber"][0].value == 7
    codec.write(f["tag:WM/Flag"], codec.validate(f["tag:WM/Flag"], ["true"]))
    assert type(tags["WM/Flag"][0]) is asf.ASFBoolAttribute and tags["WM/Flag"][0].value is True
    with pytest.raises(MetadataValueError):
        codec.validate(f["tag:WM/TrackNumber"], [2**32])       # DWORD range
    codec.validate(f["tag:WM/Big"], [2**63])                    # QWORD allows it
    with pytest.raises(MetadataValueError):
        codec.validate(f["tag:Title"], ["a\x00b"])


def test_asf_codec_snapshot_restore_exact():
    codec, tags, picture = _asf_codec()
    keys = ["tag:WM/Picture", "tag:Title", "tag:WM/Genre", "tag:WM/Flag"]
    snap = {k: codec.snapshot(k) for k in keys}
    for k in keys:
        codec.delete(k)
    assert all(codec.snapshot(k) is ABSENT for k in keys)
    for k in keys:
        codec.restore(k, snap[k])
    assert {k: codec.snapshot(k) for k in keys} == snap
    assert bytes(tags["WM/Picture"][0].value) == picture
    assert tags["Title"][0].language == 1 and tags["Title"][0].stream == 2


def test_ape_codec_in_memory():
    from mutagen.apev2 import APEv2, APEValue, BINARY
    from core.handlers._tag_codecs import ApeCodec
    tags = APEv2()
    tags["Artist"] = ["A", "B"]
    tags["Blob"] = APEValue(b"\x00\x01", BINARY)
    codec = ApeCodec(_FakeAsf(tags))
    f = {x.key: x for x in codec.fields()}
    assert f["tag:Artist"].value == ["A", "B"]
    assert f["tag:Blob"].field_type == "binary" and "\x00" not in str(f["tag:Blob"].value)
    snap = codec.snapshot("tag:Blob")
    codec.delete("tag:Blob")
    codec.restore("tag:Blob", snap)
    assert bytes(tags["Blob"].value) == b"\x00\x01"


# ──────────────────────── Undo: entry order and container bytes ──────────────
#
# Review item 11: the raw snapshot of a Vorbis comment / ASF attribute / FLAC
# picture block records where its entries were, and the restore puts them back
# there, so delete + Undo gives the same order (not "moved to the end"). The
# positions are a restore hint, not part of the value (see ``Placed``). Undo of
# the first tag added to an untagged file removes the tag container again.

async def _api(client, method: str, url: str, **body):
    resp = await getattr(client, method)(url, **body)
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _api_delete(client, path: Path, keys: list[str]) -> str:
    token = (await _api(client, "post", "/delete", json={"path": str(path), "keys": keys}))["undo_token"]
    assert token, keys
    return token


async def _api_write(client, path: Path, fields: dict) -> str:
    body = {"path": str(path), "fields": [{"key": k, "value": v} for k, v in fields.items()]}
    token = (await _api(client, "post", "/write", json=body))["undo_token"]
    assert token, fields
    return token


async def _api_undo(client, token: str) -> dict:
    return await _api(client, "post", "/undo", json={"token": token})


def comment_pairs(path: Path) -> list[tuple[str, str]]:
    import mutagen
    return list(list.__iter__(mutagen.File(path).tags))


def asf_attributes(path: Path) -> list[tuple]:
    import mutagen.asf as asf
    return [(name, type(a).__name__, a.value, a.language, a.stream)
            for name, a in list.__iter__(asf.ASF(path).tags)]


@pytest.fixture
def journal():
    routes.UNDO_JOURNAL.clear()
    yield routes.UNDO_JOURNAL
    routes.UNDO_JOURNAL.clear()


_VORBIS_CASES = [
    ("flac_vorbis_picture", ["tag:title"]),            # first comment
    ("flac_vorbis_picture", ["tag:title", "tag:genre"]),  # first and last, one Undo
    ("flac_vorbis_picture", ["tag:artist"]),           # two values in the middle
    ("ogg_vorbis", ["tag:title"]),
    ("ogg_vorbis", ["tag:title", "tag:metadata_block_picture"]),
    ("ogg_opus", ["tag:title"]),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("name, keys", _VORBIS_CASES)
async def test_vorbis_delete_then_undo_keeps_the_comment_order(client, journal, tmp_path, name, keys):
    path = make(name, tmp_path)
    before = comment_pairs(path)
    token = await _api_delete(client, path, keys)
    assert not set(keys) & set(fields(path))
    await _api_undo(client, token)
    assert comment_pairs(path) == before


@pytest.mark.asyncio
async def test_vorbis_undo_of_a_write_puts_interleaved_values_back(client, journal, tmp_path):
    """The write groups the values of a name; the Undo restores the original interleaving."""
    from mutagen.flac import FLAC
    path = af.write_flac(tmp_path / "interleaved.flac")
    audio = FLAC(path)
    audio.add_tags()
    for pair in (("TITLE", "t"), ("ARTIST", "a"), ("ALBUM", "l"), ("Artist", "b"), ("GENRE", "g")):
        audio.tags.append(pair)
    audio.save()
    before = comment_pairs(path)
    assert fields(path)["tag:artist"].value == ["a", "b"]
    token = await _api_write(client, path, {"tag:artist": ["x", "y", "z"]})
    assert [k for k, _ in comment_pairs(path)] == ["TITLE", "ARTIST", "ARTIST", "ARTIST", "ALBUM", "GENRE"]
    await _api_undo(client, token)
    assert comment_pairs(path) == before


def _flac_block_codes(path: Path) -> list[int]:
    from mutagen.flac import FLAC, Padding
    return [b.code for b in FLAC(path).metadata_blocks if b.code != Padding.code]


@pytest.mark.asyncio
async def test_flac_picture_blocks_go_back_to_their_position(client, journal, tmp_path):
    from mutagen.flac import FLAC, Picture
    path = af.write_flac(tmp_path / "blocks.flac")
    audio = FLAC(path)
    first, second = Picture(), Picture()
    first.type, first.mime, first.data = 3, "image/png", af.png_bytes()
    second.type, second.mime, second.data = 4, "image/png", af.png_bytes()[::-1]
    audio.add_picture(first)                  # STREAMINFO, PICTURE, VORBIS_COMMENT, PICTURE
    audio.add_tags()
    audio.tags.append(("TITLE", "blocks"))
    audio.add_picture(second)
    audio.save()
    before_codes, before_data = _flac_block_codes(path), [p.data for p in FLAC(path).pictures]
    assert before_codes == [0, 6, 4, 6]
    token = await _api_delete(client, path, ["flac:picture"])
    assert _flac_block_codes(path) == [0, 4]
    await _api_undo(client, token)
    assert _flac_block_codes(path) == before_codes
    assert [p.data for p in FLAC(path).pictures] == before_data
    assert path.read_bytes().endswith(b"\xff\xf8" + af.PCM[:2048])


@pytest.mark.asyncio
@pytest.mark.parametrize("keys", [["tag:WM/AlbumTitle"], ["tag:Title"], ["tag:WM/Genre"],
                                  ["tag:Title", "tag:WM/Genre", "tag:WM/Lyrics", "tag:WM/Picture"]])
async def test_asf_delete_then_undo_keeps_the_attribute_order(client, journal, tmp_path, keys):
    """Real ASF file: attributes come back in their header object and at their place."""
    path = make("wma_asf", tmp_path)
    before = asf_attributes(path)
    assert [a[0] for a in before] == af.WMA_ORDER
    token = await _api_delete(client, path, keys)
    assert not set(keys) & set(fields(path))
    await _api_undo(client, token)
    assert asf_attributes(path) == before
    assert path.read_bytes().endswith(af.ASF_PAYLOAD)


def test_asf_codec_restores_several_keys_in_place():
    """All keys of one Undo restored together: the attribute list is identical."""
    codec, tags, _ = _asf_codec()
    original = [(name, type(a), a.value, a.language, a.stream) for name, a in list.__iter__(tags)]
    keys = ["tag:WM/Picture", "tag:Title", "tag:WM/Genre", "tag:WM/Flag"]
    snap = {k: codec.snapshot(k) for k in keys}
    for k in keys:
        codec.delete(k)
    codec.restore_many(snap)
    assert [(name, type(a), a.value, a.language, a.stream) for name, a in list.__iter__(tags)] == original
    assert {k: codec.snapshot(k) for k in keys} == snap


def test_snapshot_positions_are_a_hint_not_part_of_the_value():
    """A key holding the same entries elsewhere still has the same raw value (no false 409)."""
    codec, tags, _ = _asf_codec()
    snap = codec.snapshot("tag:WM/Genre")
    list.__delitem__(tags, 0)                      # another key removed: WM/Genre moves up
    moved = codec.snapshot("tag:WM/Genre")
    assert moved == snap and moved.positions != snap.positions
    assert codec.snapshot("tag:WM/Big") != snap


@pytest.mark.asyncio
async def test_external_change_of_other_comments_does_not_block_the_undo(client, journal, tmp_path):
    """Another program removes other comments in place: the Undo still restores the key."""
    from mutagen.flac import FLAC
    path = make("flac_vorbis_picture", tmp_path)
    token = await _api_delete(client, path, ["tag:genre"])
    audio = FLAC(path)                             # in place: same file, other comments gone
    del audio.tags["title"]
    del audio.tags["artist"]
    audio.save()
    await _api_undo(client, token)
    assert comment_pairs(path) == [("Genre", "Ambient")]


def _md5(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()


@pytest.mark.asyncio
@pytest.mark.parametrize("name, key", [("wav_untagged", "tag:TIT2"), ("aiff_untagged", "tag:TIT2"),
                                       ("mp3_untagged", "tag:TIT2"), ("wv_untagged", "tag:Title")])
async def test_undo_of_the_first_tag_gives_back_the_original_bytes(client, journal, tmp_path, name, key):
    """WAV/AIFF: no empty 'id3 ' chunk; MP3: no ID3v2 header; WavPack: no APEv2 tag."""
    import mutagen
    path = make(name, tmp_path)
    original = _md5(path)
    token = await _api_write(client, path, {key: ["Added"]})
    assert _md5(path) != original and fields(path)[key].value == ["Added"]
    await _api_undo(client, token)
    assert _md5(path) == original
    assert mutagen.File(path).tags is None


def test_restore_keeps_a_container_that_still_holds_tags(tmp_path):
    """Only an EMPTY container is removed: undoing one of two added tags keeps the other."""
    path = make("wav_untagged", tmp_path)
    handler = HandlerRegistry.get(path)
    snaps = []
    for key in ("tag:TIT2", "tag:TPE1"):
        schema = handler.writable_field(path, key)
        secure_atomic_write(path, lambda tmp, key=key, schema=schema: (
            snaps.append(handler.snapshot(tmp, [key])),
            handler.apply_write(tmp, [replace(schema, value=["v"])])))
    secure_atomic_write(path, lambda tmp: handler.restore(tmp, snaps[1]))
    assert set(k for k in fields(path) if k.startswith("tag:")) == {"tag:TIT2"}
    secure_atomic_write(path, lambda tmp: handler.restore(tmp, snaps[0]))
    import mutagen
    assert mutagen.File(path).tags is None


@pytest.mark.asyncio
async def test_undo_of_the_first_flac_comment_removes_the_vorbis_block(client, journal, tmp_path):
    """FLAC: the Vorbis comment block and the PADDING mutagen adds go away: the file is
    byte-identical to the original (the journal keeps the original bytes of the tag area)."""
    from mutagen.flac import FLAC
    path = make("flac_untagged", tmp_path)
    original = path.read_bytes()
    token = await _api_write(client, path, {"tag:title": ["Added"]})
    assert path.read_bytes() != original
    await _api_undo(client, token)
    assert FLAC(path).tags is None
    assert path.read_bytes() == original


def test_vorbis_block_of_another_writer_is_kept_even_if_empty(tmp_path):
    """An empty Vorbis comment block written by an encoder keeps its vendor string."""
    from mutagen.flac import FLAC
    path = make("flac_untagged", tmp_path)
    audio = FLAC(path)
    audio.add_tags()
    audio.tags.vendor = "reference libFLAC 1.4.3 20230623"
    audio.save()
    handler = HandlerRegistry.get(path)
    schema = handler.writable_field(path, "tag:title")
    snaps = []
    secure_atomic_write(path, lambda tmp: (snaps.append(handler.snapshot(tmp, ["tag:title"])),
                                           handler.apply_write(tmp, [replace(schema, value=["x"])])))
    secure_atomic_write(path, lambda tmp: handler.restore(tmp, snaps[0]))
    tags = FLAC(path).tags
    assert tags is not None and list(tags) == [] and tags.vendor == "reference libFLAC 1.4.3 20230623"


# ──────────────────────── ID3 timestamps (review item 12) ────────────────────

@pytest.mark.parametrize("stamp", [
    "2024-13-45", "2024-13", "2024-00", "2024-02-30", "2023-02-29", "1900-02-29", "2024-04-31",
    "2024-05-00", "2024-05-01T24", "2024-05-01T23:60", "2024-05-01T23:59:60",
    "2024-5-1", "24-05-01", "2024-05-01T7:08", "2024-05-01T07:08:09Z", "2024/05/01", "2024-05-01T",
])
def test_id3_impossible_timestamps_are_rejected(tmp_path, stamp):
    path = make("mp3_v24_full", tmp_path)
    before = path.read_bytes()
    with pytest.raises(MetadataValueError) as exc:
        write(path, "tag:TDRC", ["2026", stamp])
    assert (exc.value.key, exc.value.code) == ("tag:TDRC", ErrorCode.INVALID_VALUE)
    assert path.read_bytes() == before


@pytest.mark.parametrize("stamp, stored", [
    ("2024", "2024"), ("2024-02", "2024-02"), ("2024-02-29", "2024-02-29"), ("2000-02-29", "2000-02-29"),
    ("2024-12-31T23:59:59", "2024-12-31T23:59:59"), ("2024-05-01T00", "2024-05-01T00"),
    ("2024-05-01T00:00", "2024-05-01T00:00"), ("2024-05-01 12:30", "2024-05-01T12:30"),
])
def test_id3_valid_timestamps_are_stored(tmp_path, stamp, stored):
    path = make("mp3_v24_full", tmp_path)
    write(path, "tag:TDRC", [stamp])
    assert fields(path)["tag:TDRC"].value == [stored]


@pytest.mark.parametrize("frame", ["TDRC", "TDOR", "TDRL", "TDTG", "TDEN"])
def test_every_id3_timestamp_frame_is_range_checked(tmp_path, frame):
    path = make("mp3_v24_full", tmp_path)
    with pytest.raises(MetadataValueError) as exc:
        write(path, f"tag:{frame}", ["2024-02-30"])
    assert exc.value.key == f"tag:{frame}"
    write(path, f"tag:{frame}", ["2024-02-29T10:11:12"])
    assert fields(path)[f"tag:{frame}"].value == ["2024-02-29T10:11:12"]


@pytest.mark.asyncio
async def test_api_impossible_timestamp_is_422_with_the_key(client, tmp_path):
    path = make("mp3_v24_full", tmp_path)
    before = path.read_bytes()
    resp = await client.post("/write", json={"path": str(path),
                                             "fields": [{"key": "tag:TDRC", "value": ["2024-13-45"]}]})
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert [(e["code"], e["key"]) for e in body["errors"]] == [("invalid_value", "tag:TDRC")]
    assert "2024-13-45" in body["detail"]
    assert path.read_bytes() == before


def test_stored_impossible_timestamp_is_read_only_but_deletable(tmp_path):
    """A date another tool wrote (13th month) is shown as is, cannot be re-written as
    read (G1), and delete + restore puts the exact text back."""
    import mutagen.id3 as id3
    path = make("mp3_v24_full", tmp_path)
    tags = id3.ID3(path)
    tags["TDRC"].text = ["2024-13-45"]
    tags.save(path)
    handler = HandlerRegistry.get(path)
    f = fields(path)["tag:TDRC"]
    assert (f.value, f.editable, f.deletable) == (["2024-13-45"], False, True)
    snap = handler.snapshot(path, ["tag:TDRC"])
    handler.delete(path, ["tag:TDRC"])
    secure_atomic_write(path, lambda tmp: handler.restore(tmp, snap))
    assert [str(t) for t in id3.ID3(path)["TDRC"].text] == ["2024-13-45"]


# ──────────────────────── further codec findings ─────────────────────────────

@pytest.mark.parametrize("name", ["mp3_v24_full", "mp3_v23_latin1", "mp3_untagged"])
def test_id3_frames_converted_on_load_are_not_offered(tmp_path, name):
    """mutagen turns TYER/TDAT/TIME/TORY into TDRC/TDOR and drops TRDA/TSIZ on load:
    such keys could never be read back, every write was a lossy_write 422."""
    path = make(name, tmp_path)
    handler = HandlerRegistry.get(path)
    for fid in ("TYER", "TDAT", "TIME", "TORY", "TRDA", "TSIZ", "IPLS", "RVAD", "EQUA"):
        assert handler.writable_field(path, f"tag:{fid}") is None, fid
    with pytest.raises(MetadataValueError) as exc:
        handler.write(path, [MetadataField("tag:TYER", "Year", ["2027"], multi=True, source="id3")])
    assert exc.value.code == ErrorCode.UNKNOWN_FIELD
    present = fields(path)           # the date is written through TDRC instead
    assert ("tag:TDRC" in present and present["tag:TDRC"].editable) or \
        handler.writable_field(path, "tag:TDRC") is not None


def _v23_file(tmp_path: Path, *frames) -> Path:
    """MP3 with an ID3v2.3 tag holding exactly ``frames`` (no v2.3 translation)."""
    import mutagen.id3 as id3
    path = af.write_mp3(tmp_path / "v23.mp3")
    tags = id3.ID3()
    tags.add(id3.TIT2(encoding=1, text=["Title"]))
    for frame in frames:
        tags.add(frame)
    tags.save(path, v2_version=3, v23_sep=None)
    assert id3.ID3(path).version == (2, 3, 0)
    return path


@pytest.mark.asyncio
async def test_v23_tag_with_itunes_sort_frames_stays_writable(client, journal, tmp_path):
    """iTunes writes TSOP/TSOA/TSOT (v2.4 frames) into ID3v2.3 tags; mutagen's
    update_to_v23 deletes them, so EVERY write of such a file was a 422 lossy_write."""
    import mutagen.id3 as id3
    path = _v23_file(tmp_path, id3.TSOP(encoding=1, text=["Artist, The"]),
                     id3.TSOA(encoding=1, text=["Album, The"]), id3.TSOT(encoding=1, text=["Song"]))
    await _api_write(client, path, {"tag:TIT2": ["New"]})
    tags = id3.ID3(path, translate=False)
    assert tags.version == (2, 3, 0) and tags["TIT2"].text == ["New"]
    assert (tags["TSOP"].text, tags["TSOA"].text, tags["TSOT"].text) == (["Artist, The"], ["Album, The"], ["Song"])
    token = await _api_delete(client, path, ["tag:TSOP"])
    assert "TSOP" not in id3.ID3(path)
    await _api_undo(client, token)
    assert id3.ID3(path, translate=False)["TSOP"].text == ["Artist, The"]
    assert HandlerRegistry.get(path).writable_field(path, "tag:TMOO") is None  # still not offered


@pytest.mark.asyncio
async def test_v23_recording_time_at_minute_zero_round_trips(client, journal, tmp_path):
    """mutagen converts TDRC to TIME only when hour AND minute are non-zero (and keeps
    only the first date): a v2.3 file with TIME 1200 could not be written at all."""
    import mutagen.id3 as id3
    path = _v23_file(tmp_path, id3.TYER(encoding=1, text=["2020"]), id3.TDAT(encoding=1, text=["0105"]),
                     id3.TIME(encoding=1, text=["1200"]))
    assert fields(path)["tag:TDRC"].value == ["2020-05-01T12:00:00"]
    await _api_write(client, path, {"tag:TIT2": ["New"]})
    raw = id3.ID3(path, translate=False)
    assert (raw["TYER"].text, raw["TDAT"].text, raw["TIME"].text) == (["2020"], ["0105"], ["1200"])
    for value in (["2026-05-01T00:30:00"], ["2026-05-01T12:00:00", "2027-01-02T03:04:00", "2028"],
                  ["2026-12-31", "2027"], ["2029"]):
        await _api_write(client, path, {"tag:TDRC": value})
        assert fields(path)["tag:TDRC"].value == value
        assert id3.ID3(path).version == (2, 3, 0)


@pytest.mark.parametrize("key, value", [
    ("tag:TDRC", ["2026-05"]),                    # v2.3 has no month-only date
    ("tag:TDRC", ["2026-05-01T12"]),              # nor an hour alone
    ("tag:TDRC", ["2026-05-01T12:30"]),           # read back as 12:30:00
    ("tag:TDRC", ["2026-05-01T12:30:15"]),        # no seconds in TIME
    ("tag:TDRC", ["2027", "2026-05-01"]),         # TYER/TDAT are paired by position
    ("tag:TDOR", ["2020-01-01"]),                 # TORY holds a year only
    ("tag:TDOR", ["2020", "2021"]),
])
def test_v23_dates_it_cannot_store_are_rejected_upfront(tmp_path, key, value):
    path = make("mp3_v23_latin1", tmp_path)
    before = path.read_bytes()
    with pytest.raises(MetadataValueError) as exc:
        write(path, key, value)
    assert (exc.value.key, exc.value.code) == (key, ErrorCode.UNSUPPORTED)
    assert "ID3v2.3" in exc.value.message
    assert path.read_bytes() == before


#: ID3v1 tag (128 bytes) as foobar2000 & co. append after an APEv2 tag.
ID3V1 = (b"TAG" + b"v1 title".ljust(30, b"\0") + b"v1 artist".ljust(30, b"\0") + bytes(30)
         + b"2020" + bytes(30) + b"\x0c")


@pytest.mark.asyncio
async def test_ape_write_delete_and_undo_keep_a_trailing_id3v1_tag(client, journal, tmp_path):
    """mutagen's APEv2 save truncates the file at the old APE tag: the ID3v1 tag after
    it was silently lost (it is not a field of the APE record, G4 could not see it)."""
    from mutagen.apev2 import APEv2
    path = make("wv_apev2", tmp_path)
    path.write_bytes(path.read_bytes() + ID3V1)
    assert str(APEv2(path)["Title"]) == "Ape title"
    token = await _api_write(client, path, {"tag:Title": ["New title"]})
    assert path.read_bytes().endswith(ID3V1) and path.read_bytes().count(ID3V1) == 1
    assert str(APEv2(path)["Title"]) == "New title"
    await _api_undo(client, token)
    assert path.read_bytes().endswith(ID3V1)
    token = await _api_delete(client, path, ["tag:Artist"])
    assert path.read_bytes().endswith(ID3V1)
    await _api_undo(client, token)
    assert path.read_bytes().endswith(ID3V1) and list(APEv2(path)["Artist"]) == ["One", "Two"]


@pytest.mark.asyncio
async def test_ape_tag_added_in_front_of_an_existing_id3v1_tag(client, journal, tmp_path):
    """Without an APE tag mutagen appended the new one AFTER the ID3v1 tag, which then
    was no longer the last 128 bytes (where every reader looks for it)."""
    from mutagen.apev2 import APEv2
    path = make("wv_untagged", tmp_path)
    original = path.read_bytes() + ID3V1
    path.write_bytes(original)
    token = await _api_write(client, path, {"tag:Title": ["Added"]})
    data = path.read_bytes()
    assert data.endswith(ID3V1) and data.count(ID3V1) == 1 and data.startswith(original[:-128])
    assert str(APEv2(path)["Title"]) == "Added"
    await _api_undo(client, token)
    assert path.read_bytes() == original


# ──────────────────────── large integers (review item 14a) ───────────────────

@pytest.mark.asyncio
async def test_api_qword_above_2_53_is_exact_both_ways(client, journal, tmp_path):
    """The JSON of /read carries every digit (the precision loss is in JavaScript's
    JSON.parse); /write accepts the decimal string the editor can send."""
    import mutagen.asf as asf
    path = make("wma_asf", tmp_path)
    resp = await client.get("/read", params={"path": str(path)})
    assert '"value":[1152921504606846979]' in resp.text.replace(" ", "")
    big = str(2**64 - 1)
    token = await _api_write(client, path, {"tag:WM/EncodingTime": [big]})
    assert asf.ASF(path).tags["WM/EncodingTime"][0].value == 2**64 - 1
    resp = await client.get("/read", params={"path": str(path)})
    assert f'"value":[{big}]' in resp.text.replace(" ", "")
    await _api_undo(client, token)
    assert asf.ASF(path).tags["WM/EncodingTime"][0].value == 2**60 + 3


# ──────────────────────── WMA via ffmpeg (optional) ──────────────────────────

@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not available")
def test_wma_file_round_trip_with_ffmpeg(tmp_path):
    path = tmp_path / "tone.wma"
    proc = subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "anullsrc=r=8000:cl=mono",
                           "-t", "0.2", "-c:a", "wmav2", "-metadata", "title=WMA title",
                           "-metadata", "WM/TrackNumber=3", str(path)],
                          capture_output=True, timeout=60)
    if proc.returncode != 0 or not path.exists():
        pytest.skip(f"ffmpeg cannot encode WMA: {proc.stderr[-200:]!r}")
    handler = HandlerRegistry.get(path)
    record = handler.read(path)
    assert record.supports_write and not record.read_errors
    f = {x.key: x for x in record.fields}
    assert f["tag:Title"].value == ["WMA title"]
    write(path, "tag:Title", ["Nuovo €"])
    assert fields(path)["tag:Title"].value == ["Nuovo €"]
    snap = handler.snapshot(path, ["tag:Title"])
    handler.delete(path, ["tag:Title"])
    secure_atomic_write(path, lambda tmp: handler.restore(tmp, snap))
    assert handler.snapshot(path, ["tag:Title"]) == snap
