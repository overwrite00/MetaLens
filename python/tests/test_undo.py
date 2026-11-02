"""Phase F3B: exact Undo through the server-side journal (``POST /undo``).

Contract (used by the renderer):

* ``POST /write`` / ``POST /delete`` succeed with
  ``{"ok": true, "errors": [], "undo_token": <opaque str> | null}``; the token is
  null when no key actually changed (no-op save).
* ``POST /undo {"token"}`` (no path: it is in the journal) succeeds with
  ``{"ok": true, "errors": [], "keys": [...], "undo_token": null}``; unknown,
  consumed or evicted token -> 410 ``undo_expired``; a touched key no longer
  holding its post-value, or a replaced file -> 409 ``concurrent_modification``
  and nothing is touched or consumed.
* The Undo is itself one verified atomic transaction (G4); mtime is restored
  only when ``fs:mtime`` was among the touched keys (D19).
"""
from __future__ import annotations

import copy
import json
import os
import pickle
from pathlib import Path
from typing import Any

import pytest

import api.routes as routes
from api.undo_journal import UndoJournal, UndoTooLargeError
from core.base_handler import ABSENT, BaseMetadataHandler
from core.errors import ErrorCode
from core.models import MetadataField, MetadataRecord
from core.registry import HandlerRegistry
from tests.factories import by_name


# ──────────────────────── helpers ────────────────────────────────────────────

def _ok(resp) -> dict:
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True and body["errors"] == [], body
    return body


def _error(resp, status: int, code: str) -> dict:
    assert resp.status_code == status, resp.text
    body = resp.json()
    assert body["ok"] is False and isinstance(body["detail"], str) and body["detail"]
    assert code in [e["code"] for e in body["errors"]], body
    return body


def _token(body: dict) -> str:
    token = body.get("undo_token")
    assert isinstance(token, str) and token, body
    return token


def _build(name: str, tmp_path: Path) -> Path:
    directory = tmp_path / name
    directory.mkdir()
    return by_name(name).build(directory)


def _snap(path: Path, keys: list[str]) -> dict:
    return HandlerRegistry.get(path).snapshot(path, keys)


async def _read(client, path: Path) -> dict[str, dict]:
    resp = await client.get("/read", params={"path": str(path)})
    assert resp.status_code == 200, resp.text
    return {f["key"]: f for f in resp.json()["fields"]}


def _stable(fields: dict[str, dict]) -> dict[str, Any]:
    """Values of everything but the filesystem times/size and computed values."""
    return {k: f["value"] for k, f in fields.items()
            if not k.startswith("fs:") and (f["editable"] or f["deletable"])}


def _no_leftovers(path: Path) -> None:
    others = sorted(p.name for p in path.parent.iterdir()
                    if p.name.startswith(".metalens-") or ".ml_tmp" in p.name)
    assert not others, others


async def _write(client, path: Path, fields: dict | None = None, delete: list[str] | None = None):
    body = {"path": str(path), "fields": [{"key": k, "value": v} for k, v in (fields or {}).items()]}
    if delete:
        body["delete"] = delete
    return await client.post("/write", json=body)


async def _delete(client, path: Path, keys: list[str]):
    return await client.post("/delete", json={"path": str(path), "keys": keys})


async def _undo(client, token: str):
    return await client.post("/undo", json={"token": token})


@pytest.fixture(autouse=True)
def _fresh_journal():
    routes.UNDO_JOURNAL.clear()
    yield
    routes.UNDO_JOURNAL.clear()


# ──────────────────────── response contract ──────────────────────────────────

SAVE_KEYS = {"ok", "errors", "undo_token", "undo_unavailable", "warnings"}


@pytest.mark.asyncio
async def test_write_and_delete_return_undo_token(client, tmp_path):
    path = _build("jpeg_camera", tmp_path)
    body = _ok(await _write(client, path, {"exif:Make": "Nikon"}))
    assert set(body) == SAVE_KEYS
    assert body["undo_unavailable"] is None and body["warnings"] == []
    token_a = _token(body)
    body = _ok(await _delete(client, path, ["exif:Model"]))
    assert set(body) == SAVE_KEYS
    assert body["undo_unavailable"] is None and body["warnings"] == []
    token_b = _token(body)
    assert token_a != token_b


@pytest.mark.asyncio
async def test_noop_write_has_no_token(client, tmp_path):
    path = _build("jpeg_camera", tmp_path)
    fields = await _read(client, path)
    body = _ok(await _write(client, path, {"fs:mtime": fields["fs:mtime"]["value"]}))
    assert body["undo_token"] is None and body["undo_unavailable"] is None
    body = _ok(await _write(client, path, {"exif:Make": fields["exif:Make"]["value"]}))
    assert body["undo_token"] is None and body["undo_unavailable"] is None
    assert len(routes.UNDO_JOURNAL) == 0


@pytest.mark.asyncio
async def test_noop_write_does_not_replace_the_file(client, tmp_path):
    """A save that changes nothing must not swap the file (hard links, file id, mtime stay)."""
    path = _build("jpeg_camera", tmp_path)
    link = path.with_name("link_" + path.name)
    os.link(path, link)
    before = path.stat()
    fields = await _read(client, path)
    body = _ok(await _write(client, path, {"exif:Make": fields["exif:Make"]["value"],
                                           "fs:mtime": fields["fs:mtime"]["value"]}))
    assert body["undo_token"] is None and body["undo_unavailable"] is None
    after = path.stat()
    assert (after.st_ino, after.st_mtime_ns, after.st_size) == (before.st_ino, before.st_mtime_ns, before.st_size)
    assert os.path.samefile(path, link)                       # still the same file as its hard link
    _no_leftovers(path)
    # A real change still replaces it atomically (the link keeps the old content).
    original = link.read_bytes()
    _token(_ok(await _write(client, path, {"exif:Make": "Nikon"})))
    assert not os.path.samefile(path, link) and link.read_bytes() == original


@pytest.mark.asyncio
async def test_undo_response_shape_and_consumption(client, tmp_path):
    path = _build("jpeg_camera", tmp_path)
    token = _token(_ok(await _write(client, path, {"exif:Make": "Nikon"})))
    body = _ok(await _undo(client, token))
    assert set(body) == {"ok", "errors", "keys", "undo_token", "warnings"}
    assert body["keys"] == ["exif:Make"] and body["undo_token"] is None and body["warnings"] == []
    assert (await _read(client, path))["exif:Make"]["value"] == "Canon"
    _error(await _undo(client, token), 410, ErrorCode.UNDO_EXPIRED)   # consumed
    _no_leftovers(path)


@pytest.mark.asyncio
async def test_undo_never_accepts_a_path(client, tmp_path):
    path = _build("jpeg_camera", tmp_path)
    token = _token(_ok(await _write(client, path, {"exif:Make": "Nikon"})))
    resp = await client.post("/undo", json={"token": token, "path": str(path)})
    _error(resp, 422, "invalid_request")
    _error(await client.post("/undo", json={}), 422, "invalid_request")
    _error(await client.post("/undo", json={"token": ""}), 422, "invalid_request")
    assert (await _read(client, path))["exif:Make"]["value"] == "Nikon"
    _ok(await _undo(client, token))   # the failed requests did not consume it


# ──────────────────────── exact restore ──────────────────────────────────────

@pytest.mark.asyncio
async def test_bulk_delete_then_undo_restores_everything(client, tmp_path):
    path = _build("jpeg_camera", tmp_path)
    keys = ["exif:Make", "gps:Latitude", "exif:SubjectArea"]
    before_snap = _snap(path, keys)
    before = _stable(await _read(client, path))
    token = _token(_ok(await _delete(client, path, keys)))
    after_delete = await _read(client, path)
    assert not set(keys) & set(after_delete)
    body = _ok(await _undo(client, token))
    assert sorted(body["keys"]) == sorted(keys)
    assert _snap(path, keys) == before_snap
    assert _stable(await _read(client, path)) == before
    _no_leftovers(path)


@pytest.mark.asyncio
async def test_undo_restores_latin1_artist_bytes(client, tmp_path):
    import piexif
    path = _build("jpeg_camera", tmp_path)
    assert piexif.load(str(path))["0th"][piexif.ImageIFD.Artist] == b"Jos\xe9"
    token = _token(_ok(await _write(client, path, {"exif:Artist": "Zoë"})))
    assert piexif.load(str(path))["0th"][piexif.ImageIFD.Artist] == "Zoë".encode("utf-8")
    _ok(await _undo(client, token))
    assert piexif.load(str(path))["0th"][piexif.ImageIFD.Artist] == b"Jos\xe9"


@pytest.mark.asyncio
async def test_undo_restores_apic_byte_for_byte(client, tmp_path):
    import mutagen.id3 as id3
    path = _build("mp3_v24_full", tmp_path)
    original = id3.ID3(path).getall("APIC")[0]
    snap = _snap(path, ["tag:APIC:cover"])
    token = _token(_ok(await _delete(client, path, ["tag:APIC:cover"])))
    assert id3.ID3(path).getall("APIC") == []
    _ok(await _undo(client, token))
    restored = id3.ID3(path).getall("APIC")
    assert len(restored) == 1
    assert (restored[0].data, restored[0].mime, restored[0].type, restored[0].desc, restored[0].encoding) \
        == (original.data, original.mime, original.type, original.desc, original.encoding)
    assert _snap(path, ["tag:APIC:cover"]) == snap


@pytest.mark.asyncio
async def test_undo_restores_mp4_covr_byte_for_byte(client, tmp_path):
    from mutagen.mp4 import MP4
    path = _build("mp4_tags", tmp_path)
    original = MP4(path).tags["covr"]
    snap = _snap(path, ["mp4:covr"])
    token = _token(_ok(await _delete(client, path, ["mp4:covr"])))
    assert "covr" not in MP4(path).tags
    _ok(await _undo(client, token))
    restored = MP4(path).tags["covr"]
    assert [bytes(c) for c in restored] == [bytes(c) for c in original]
    assert [c.imageformat for c in restored] == [c.imageformat for c in original]
    assert _snap(path, ["mp4:covr"]) == snap


@pytest.mark.asyncio
async def test_undo_of_xmp_and_iptc_changes(client, tmp_path):
    path = _build("jpeg_xmp_first", tmp_path)
    keys = ["xmp:dc:title", "xmp:dc:subject", "xmp:xmp:Rating"]
    snap = _snap(path, keys)
    before = _stable(await _read(client, path))
    token = _token(_ok(await _write(client, path, {"xmp:dc:title": "Nuovo € 日本",
                                                   "xmp:dc:subject": ["uno"]},
                                    delete=["xmp:xmp:Rating"])))
    _ok(await _undo(client, token))
    assert _snap(path, keys) == snap
    assert _stable(await _read(client, path)) == before

    path = _build("jpeg_iptc", tmp_path)
    keys = ["iptc:Keywords", "iptc:ObjectName", "iptc:Caption-Abstract"]
    snap = _snap(path, keys)
    raw = path.read_bytes()
    token = _token(_ok(await _write(client, path, {"iptc:Keywords": ["solo €"]},
                                    delete=["iptc:ObjectName", "iptc:Caption-Abstract"])))
    assert path.read_bytes() != raw
    _ok(await _undo(client, token))
    assert _snap(path, keys) == snap
    assert (await _read(client, path))["iptc:Keywords"]["value"] == ["one", "due è", "三"]


# ── WM/Picture: ASF attributes through the real TagMutations hooks ───────────

class _PickledAsf:
    """Pickled stand-in for a mutagen ASF file object: it lets this test drive WM/Picture through the
        real ``TagMutations`` hooks with an in-memory container. A real ASF file is covered by the
        ``wma_asf`` sample of ``tests/factories/audio.py`` (test_contract)."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.tags = pickle.loads(self.path.read_bytes())

    def add_tags(self):  # pragma: no cover - ASF always has tags
        raise AssertionError

    def save(self) -> None:
        self.path.write_bytes(pickle.dumps(self.tags))


def _asf_handler_class():
    from core.handlers._tag_codecs import AsfCodec, TagMutations

    class AsfLikeHandler(TagMutations, BaseMetadataHandler):
        NAME = "asf_like"
        EXTENSIONS = frozenset({".mlasf"})
        PRIORITY = 1

        def _open_codec(self, path: Path):
            return AsfCodec(_PickledAsf(path)), True

        def read(self, path: Path) -> MetadataRecord:
            record = self._make_record(path, supports_write=True, supports_delete=True)
            record.fields = self._open_codec(path)[0].fields()
            return record

        def write(self, path, fields):  # pragma: no cover - not used by the route
            raise AssertionError

        def delete(self, path, keys):  # pragma: no cover
            raise AssertionError

    return AsfLikeHandler


@pytest.mark.asyncio
async def test_undo_restores_wm_picture_byte_for_byte(client, tmp_path, monkeypatch):
    import mutagen.asf as asf
    handler = _asf_handler_class()()
    monkeypatch.setattr(HandlerRegistry, "_handlers",
                        sorted([*HandlerRegistry._handlers, handler], key=lambda h: h.PRIORITY))
    picture = b"\x03" + (10).to_bytes(4, "little") + "image/png".encode("utf-16-le") + b"\x00\x00" \
        + b"\x00\x00" + b"0123456789"
    tags = asf.ASFTags()
    tags.append(("Title", asf.ASFUnicodeAttribute("Song", language=1, stream=2)))
    tags.append(("WM/Picture", asf.ASFByteArrayAttribute(picture)))
    path = tmp_path / "song.mlasf"
    path.write_bytes(pickle.dumps(tags))
    snap = handler.snapshot(path, ["tag:WM/Picture", "tag:Title"])
    token = _token(_ok(await _write(client, path, {"tag:Title": ["New"]}, delete=["tag:WM/Picture"])))
    assert "WM/Picture" not in dict(list.__iter__(pickle.loads(path.read_bytes())))
    body = _ok(await _undo(client, token))
    assert sorted(body["keys"]) == ["tag:Title", "tag:WM/Picture"]
    restored = pickle.loads(path.read_bytes())
    pic = restored["WM/Picture"][0]
    assert type(pic) is asf.ASFByteArrayAttribute and bytes(pic.value) == picture
    assert restored["Title"][0].language == 1 and restored["Title"][0].stream == 2
    assert handler.snapshot(path, ["tag:WM/Picture", "tag:Title"]) == snap


# ──────────────────────── conflicts and expiry ───────────────────────────────

def _external_edit(path: Path, served: dict, value: Any) -> None:
    """Another program changes one key IN PLACE (same file, new content)."""
    from core.handlers.image_handler import ImageHandler
    work = path.parent / "external-work"
    work.mkdir(exist_ok=True)
    copy_path = work / path.name
    copy_path.write_bytes(path.read_bytes())
    ImageHandler().write(copy_path, [MetadataField(**{**_field_kwargs(served), "value": value})])
    data = copy_path.read_bytes()
    copy_path.unlink()
    work.rmdir()
    with path.open("r+b") as stream:
        stream.write(data)
        stream.truncate()


@pytest.mark.asyncio
async def test_external_modification_is_409_and_keeps_the_entry(client, tmp_path):
    path = _build("jpeg_camera", tmp_path)
    token = _token(_ok(await _write(client, path, {"exif:Make": "Nikon"})))
    make = (await _read(client, path))["exif:Make"]
    _external_edit(path, make, "External")
    before = path.read_bytes()
    body = _error(await _undo(client, token), 409, ErrorCode.CONCURRENT_MODIFICATION)
    assert [e["key"] for e in body["errors"]] == ["exif:Make"], body
    assert path.read_bytes() == before
    _no_leftovers(path)
    # Not consumed: once the key holds the post-value again, the Undo works.
    _external_edit(path, make, "Nikon")
    _ok(await _undo(client, token))
    assert (await _read(client, path))["exif:Make"]["value"] == "Canon"


@pytest.mark.asyncio
async def test_external_change_of_other_keys_does_not_block_undo(client, tmp_path):
    path = _build("jpeg_camera", tmp_path)
    token = _token(_ok(await _write(client, path, {"exif:Make": "Nikon"})))
    _external_edit(path, (await _read(client, path))["exif:Model"], "Changed elsewhere")
    _ok(await _undo(client, token))
    fields = await _read(client, path)
    assert (fields["exif:Make"]["value"], fields["exif:Model"]["value"]) == ("Canon", "Changed elsewhere")


def _field_kwargs(served: dict) -> dict:
    return {"key": served["key"], "label": served["label"], "value": served["value"],
            "field_type": served["field_type"], "multi": served["multi"], "length": served["length"],
            "source": served["source"]}


@pytest.mark.asyncio
async def test_replaced_file_is_409(client, tmp_path):
    path = tmp_path / "file.txt"
    path.write_text("content")
    token = _token(_ok(await _write(client, path, {"fs:mtime": "2020-01-01T00:00:00+00:00"})))
    stat = path.stat()
    twin = tmp_path / "twin.txt"
    twin.write_text("content")
    os.utime(twin, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    os.replace(twin, path)                     # same content and mtime, another file
    _error(await _undo(client, token), 409, ErrorCode.CONCURRENT_MODIFICATION)
    assert path.stat().st_mtime_ns == stat.st_mtime_ns
    assert len(routes.UNDO_JOURNAL) == 1


@pytest.mark.asyncio
async def test_deleted_file_is_409(client, tmp_path):
    path = tmp_path / "file.txt"
    path.write_text("content")
    token = _token(_ok(await _write(client, path, {"fs:mtime": "2020-01-01T00:00:00+00:00"})))
    path.unlink()
    _error(await _undo(client, token), 409, ErrorCode.CONCURRENT_MODIFICATION)


@pytest.mark.asyncio
async def test_unknown_token_is_410(client):
    _error(await _undo(client, "no-such-token"), 410, ErrorCode.UNDO_EXPIRED)


@pytest.mark.asyncio
async def test_undo_after_restart_is_410(client, tmp_path):
    path = _build("jpeg_camera", tmp_path)
    token = _token(_ok(await _write(client, path, {"exif:Make": "Nikon"})))
    routes.UNDO_JOURNAL.clear()   # sidecar restarted: the journal lives in memory only
    _error(await _undo(client, token), 410, ErrorCode.UNDO_EXPIRED)
    assert (await _read(client, path))["exif:Make"]["value"] == "Nikon"


@pytest.mark.asyncio
async def test_entry_limit_evicts_oldest(client, tmp_path):
    path = tmp_path / "file.txt"
    path.write_text("content")
    tokens = []
    for i in range(routes.UNDO_JOURNAL.max_entries + 1):
        body = _ok(await _write(client, path, {"fs:mtime": f"2020-01-01T00:{i // 60:02d}:{i % 60:02d}+00:00"}))
        tokens.append(_token(body))
    assert routes.UNDO_JOURNAL.max_entries == 50
    assert len(routes.UNDO_JOURNAL) == 50
    _error(await _undo(client, tokens[0]), 410, ErrorCode.UNDO_EXPIRED)
    _ok(await _undo(client, tokens[-1]))
    _ok(await _undo(client, tokens[-2]))


def test_journal_limits_unit():
    journal = UndoJournal(max_entries=3, max_bytes=1200)
    assert journal.max_bytes == 1200
    t1 = journal.add(Path("a"), "h", ("dev", 1), {"format": {"k": b"x" * 400}}, {"format": {"k": ABSENT}})
    t2 = journal.add(Path("a"), "h", ("dev", 1), {"format": {"k": b"y" * 400}}, {"format": {"k": ABSENT}})
    assert journal.get(t1) is not None and journal.get(t2) is not None
    t3 = journal.add(Path("b"), "h", ("dev", 2), {"format": {"k": b"z" * 400}}, {"format": {"k": ABSENT}})
    assert journal.get(t1) is None                        # byte budget: FIFO eviction
    assert journal.get(t2) is not None and journal.get(t3) is not None
    t4 = journal.add(Path("b"), "h", ("dev", 2), {"format": {"k": 1}}, {"format": {"k": 2}})
    t5 = journal.add(Path("b"), "h", ("dev", 2), {"format": {"k": 2}}, {"format": {"k": 3}})
    assert journal.get(t2) is None and len(journal) == 3   # entry budget
    assert journal.add(Path("c"), "h", ("dev", 3), {"format": {"k": b"w" * 5000}},
                       {"format": {"k": ABSENT}}) is None   # larger than the whole budget
    with pytest.raises(UndoTooLargeError) as too_large:      # same, with the sizes
        journal.add_entry(Path("c"), "h", ("dev", 3), {"format": {"k": b"w" * 5000}},
                          {"format": {"k": ABSENT}})
    assert too_large.value.size > 5000 and too_large.value.limit == 1200
    assert {journal.get(t) is not None for t in (t3, t4, t5)} == {True}   # nothing evicted
    entry = journal.get(t5)
    entry.pre["format"]["k"] = 99                          # callers get copies
    assert journal.get(t5).pre["format"]["k"] == 2
    journal.update_identity(Path("b"), ("dev", 2), ("dev", 9))
    assert journal.get(t4).identity == ("dev", 9)
    assert journal.pop(t4) is not None and journal.get(t4) is None
    journal.clear()
    assert len(journal) == 0


# ──────────────────────── ordering ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_two_undos_in_reverse_order(client, tmp_path):
    path = _build("jpeg_camera", tmp_path)
    t1 = _token(_ok(await _write(client, path, {"exif:Make": "First", "exif:Model": "M1"})))
    t2 = _token(_ok(await _write(client, path, {"exif:Make": "Second"}, delete=["gps:Latitude"])))
    # Wrong order: the first entry's post-value (Make=First) is gone -> 409, not consumed.
    _error(await _undo(client, t1), 409, ErrorCode.CONCURRENT_MODIFICATION)
    _ok(await _undo(client, t2))
    fields = await _read(client, path)
    assert fields["exif:Make"]["value"] == "First" and "gps:Latitude" in fields
    _ok(await _undo(client, t1))
    fields = await _read(client, path)
    assert (fields["exif:Make"]["value"], fields["exif:Model"]["value"]) == ("Canon", "EOS Test")


@pytest.mark.asyncio
async def test_undo_of_other_keys_keeps_newer_entries_valid(client, tmp_path):
    path = _build("jpeg_camera", tmp_path)
    t1 = _token(_ok(await _write(client, path, {"exif:Make": "First"})))
    t2 = _token(_ok(await _write(client, path, {"exif:Model": "Other"})))
    _ok(await _undo(client, t1))        # disjoint keys: the order does not matter
    _ok(await _undo(client, t2))
    fields = await _read(client, path)
    assert (fields["exif:Make"]["value"], fields["exif:Model"]["value"]) == ("Canon", "EOS Test")


# ──────────────────────── mtime (D19) ────────────────────────────────────────

@pytest.mark.asyncio
async def test_undo_does_not_restore_mtime_implicitly(client, tmp_path):
    path = _build("jpeg_camera", tmp_path)
    old_ns = 946684800 * 10**9   # 2000-01-01
    os.utime(path, ns=(old_ns, old_ns))
    token = _token(_ok(await _write(client, path, {"exif:Make": "Nikon"})))
    body = _ok(await _undo(client, token))
    assert "fs:mtime" not in body["keys"]
    assert path.stat().st_mtime_ns != old_ns


@pytest.mark.asyncio
async def test_undo_restores_mtime_when_touched(client, tmp_path):
    path = _build("jpeg_camera", tmp_path)
    old_ns = 946684800 * 10**9 + 123456700
    os.utime(path, ns=(old_ns, old_ns))
    token = _token(_ok(await _write(client, path, {"exif:Make": "Nikon",
                                                   "fs:mtime": "2020-01-01T00:00:00+00:00"})))
    body = _ok(await _undo(client, token))
    assert sorted(body["keys"]) == ["exif:Make", "fs:mtime"]
    assert path.stat().st_mtime_ns == old_ns
    assert (await _read(client, path))["exif:Make"]["value"] == "Canon"


# ──────────────────────── single save and failures (D2, G4) ──────────────────

@pytest.mark.asyncio
async def test_combined_save_is_one_undo(client, tmp_path):
    path = _build("jpeg_camera", tmp_path)
    keys = ["exif:Make", "exif:Model", "gps:Latitude"]
    snap = _snap(path, keys)
    token = _token(_ok(await _write(client, path, {"exif:Make": "Nikon"},
                                    delete=["exif:Model", "gps:Latitude"])))
    assert len(routes.UNDO_JOURNAL) == 1
    body = _ok(await _undo(client, token))
    assert sorted(body["keys"]) == sorted(keys)
    assert _snap(path, keys) == snap


@pytest.mark.asyncio
async def test_failed_combined_save_changes_nothing_and_has_no_token(client, tmp_path):
    path = _build("jpeg_camera", tmp_path)
    before = path.read_bytes()
    resp = await _write(client, path, {"exif:Make": "Nikon"}, delete=["fs:size", "nope:x"])
    body = _error(resp, 422, ErrorCode.NOT_DELETABLE)
    assert "undo_token" not in body
    resp = await _write(client, path, {"exif:Make": "Nikon", "exif:DateTimeOriginal": "garbage"},
                        delete=["gps:Latitude"])
    body = _error(resp, 422, ErrorCode.INVALID_VALUE)
    assert "undo_token" not in body
    assert path.read_bytes() == before
    assert len(routes.UNDO_JOURNAL) == 0
    _no_leftovers(path)


# Fake handler whose restore can be made lossy (G4 inside the Undo transaction).

class _JsonHandler(BaseMetadataHandler):
    NAME = "jsonfake"
    EXTENSIONS = frozenset({".mlundo"})
    PRIORITY = 1
    lose_on_restore: str | None = None
    lose_on_write: str | None = None

    @staticmethod
    def load(path: Path) -> dict:
        return json.loads(Path(path).read_text(encoding="utf-8"))

    @staticmethod
    def save(path: Path, data: dict) -> None:
        Path(path).write_text(json.dumps(data, sort_keys=True), encoding="utf-8")

    def read(self, path: Path) -> MetadataRecord:
        record = self._make_record(path, supports_write=True, supports_delete=True)
        record.fields = [MetadataField(k, k, copy.deepcopy(v), field_type="str", source="jsonfake")
                         for k, v in self.load(path).items()]
        return record

    def writable_fields(self, path, record):
        present = {f.key for f in record.fields}
        return [MetadataField(k, k, None, field_type="str") for k in ("j:a", "j:b", "j:c")
                if k not in present]

    def apply_write(self, tmp, fields):
        data = self.load(tmp)
        data.update({f.key: self.canonical(f, f.value) for f in fields})
        if self.lose_on_write:
            data.pop(self.lose_on_write, None)
        self.save(tmp, data)

    def apply_delete(self, tmp, keys):
        data = self.load(tmp)
        for k in keys:
            data.pop(k, None)
        self.save(tmp, data)

    def snapshot(self, tmp, keys):
        data = self.load(tmp)
        return {k: data[k] if k in data else ABSENT for k in keys}

    def restore(self, tmp, snap):
        data = self.load(tmp)
        for k, raw in snap.items():
            if raw is ABSENT:
                data.pop(k, None)
            else:
                data[k] = raw
        if self.lose_on_restore:
            data.pop(self.lose_on_restore, None)
        self.save(tmp, data)

    def write(self, path, fields):  # pragma: no cover
        raise AssertionError

    def delete(self, path, keys):  # pragma: no cover
        raise AssertionError


@pytest.fixture
def jsonfake(monkeypatch):
    handler = _JsonHandler()
    monkeypatch.setattr(HandlerRegistry, "_handlers",
                        sorted([*HandlerRegistry._handlers, handler], key=lambda h: h.PRIORITY))
    return handler


@pytest.mark.asyncio
async def test_lossy_undo_is_422_file_intact_entry_kept(client, tmp_path, jsonfake, monkeypatch):
    monkeypatch.setattr(routes, "make_patch", lambda before, after: None)   # value-level restore only
    path = tmp_path / "data.mlundo"
    _JsonHandler.save(path, {"j:a": "1", "j:b": "2"})
    token = _token(_ok(await _write(client, path, {"j:a": "changed"})))
    before = path.read_bytes()
    jsonfake.lose_on_restore = "j:b"                 # the restore silently drops another key
    body = _error(await _undo(client, token), 422, ErrorCode.LOSSY_WRITE)
    assert "j:b" in [e["key"] for e in body["errors"]]
    assert path.read_bytes() == before
    _no_leftovers(path)
    jsonfake.lose_on_restore = None
    body = _ok(await _undo(client, token))
    assert _JsonHandler.load(path) == {"j:a": "1", "j:b": "2"}


@pytest.mark.asyncio
async def test_lossy_write_is_422_without_token(client, tmp_path, jsonfake):
    path = tmp_path / "data.mlundo"
    _JsonHandler.save(path, {"j:a": "1", "j:b": "2"})
    before = path.read_bytes()
    jsonfake.lose_on_write = "j:b"
    resp = await _write(client, path, {"j:a": "changed"})
    body = _error(resp, 422, ErrorCode.LOSSY_WRITE)
    assert "undo_token" not in body
    assert path.read_bytes() == before
    assert len(routes.UNDO_JOURNAL) == 0


@pytest.mark.asyncio
async def test_undo_of_added_key_removes_it(client, tmp_path, jsonfake):
    path = tmp_path / "data.mlundo"
    _JsonHandler.save(path, {"j:a": "1"})
    token = _token(_ok(await _write(client, path, {"j:c": "added"})))
    assert _JsonHandler.load(path) == {"j:a": "1", "j:c": "added"}
    body = _ok(await _undo(client, token))
    assert body["keys"] == ["j:c"]
    assert _JsonHandler.load(path) == {"j:a": "1"}


@pytest.mark.asyncio
async def test_undo_with_changed_handler_is_409(client, tmp_path, jsonfake, monkeypatch):
    path = tmp_path / "data.mlundo"
    _JsonHandler.save(path, {"j:a": "1"})
    token = _token(_ok(await _write(client, path, {"j:a": "2"})))
    monkeypatch.setattr(_JsonHandler, "NAME", "renamed")
    _error(await _undo(client, token), 409, ErrorCode.CONCURRENT_MODIFICATION)
    assert _JsonHandler.load(path) == {"j:a": "2"}


# ──────────────────────── saved without Undo: undo_unavailable ───────────────

@pytest.fixture
def small_journal(monkeypatch):
    journal = UndoJournal(max_bytes=64 * 1024)
    monkeypatch.setattr(routes, "UNDO_JOURNAL", journal)
    return journal


@pytest.mark.asyncio
async def test_change_too_large_for_the_journal_reports_undo_unavailable(client, tmp_path, jsonfake,
                                                                       small_journal):
    """Used to answer exactly like a no-op (undo_token null) and the UI said "Saved"."""
    path = tmp_path / "data.mlundo"
    _JsonHandler.save(path, {"j:a": "x" * 200_000})
    body = _ok(await _write(client, path, {"j:a": "short"}))
    assert body["undo_token"] is None
    unavailable = body["undo_unavailable"]
    assert set(unavailable) == {"code", "message"} and unavailable["code"] == "undo_too_large"
    assert "too large to be undone" in unavailable["message"]
    assert "64 KiB limit" in unavailable["message"], unavailable["message"]
    assert _JsonHandler.load(path)["j:a"] == "short"     # the change itself was saved
    assert len(small_journal) == 0


@pytest.mark.asyncio
async def test_deleting_a_large_cover_is_saved_without_undo_and_says_so(client, tmp_path, small_journal):
    """Decision: a deletion too large for the journal is NOT refused (the user may
    want to remove a huge picture), but the response says it cannot be undone."""
    import mutagen.id3 as id3
    path = _build("mp3_v24_full", tmp_path)
    tags = id3.ID3(path)
    tags.delall("APIC")
    tags.add(id3.APIC(encoding=3, mime="image/png", type=3, desc="cover", data=os.urandom(200_000)))
    tags.save(path)
    assert "tag:APIC:cover" in await _read(client, path)
    body = _ok(await _delete(client, path, ["tag:APIC:cover"]))
    assert body["undo_token"] is None
    assert body["undo_unavailable"]["code"] == "undo_too_large"
    assert "KiB > 64 KiB limit" in body["undo_unavailable"]["message"]
    assert id3.ID3(path).getall("APIC") == []
    assert "tag:APIC:cover" not in await _read(client, path)


@pytest.mark.asyncio
async def test_snapshot_failure_is_logged_and_reported(client, tmp_path, jsonfake, monkeypatch, caplog):
    """A failing pre-image used to be swallowed (``except Exception: pre = None``)."""
    path = tmp_path / "data.mlundo"
    _JsonHandler.save(path, {"j:a": "1", "j:b": "2"})

    def broken(self, tmp, keys):
        raise KeyError("bug in snapshot")
    monkeypatch.setattr(_JsonHandler, "snapshot", broken)
    with caplog.at_level("ERROR", logger="api.routes"):
        body = _ok(await _write(client, path, {"j:a": "changed"}))
    assert body["undo_token"] is None
    assert body["undo_unavailable"]["code"] == "undo_snapshot_failed"
    # fixed description in the response; the exception itself is in the log (below)
    assert "unexpected value in the file" in body["undo_unavailable"]["message"]
    assert "bug in snapshot" not in body["undo_unavailable"]["message"]
    assert _JsonHandler.load(path) == {"j:a": "changed", "j:b": "2"}
    assert any(r.exc_info and r.exc_info[0] is KeyError for r in caplog.records), caplog.records
    # A real no-op stays a no-op even when the snapshot is broken.
    body = _ok(await _write(client, path, {"j:b": "2"}))
    assert body["undo_token"] is None and body["undo_unavailable"] is None


@pytest.mark.asyncio
async def test_post_image_failure_is_reported(client, tmp_path, jsonfake, monkeypatch, caplog):
    path = tmp_path / "data.mlundo"
    _JsonHandler.save(path, {"j:a": "1"})
    real = _JsonHandler.snapshot

    def only_on_the_private_copy(self, tmp, keys):
        if Path(tmp).resolve() == path.resolve():   # the post-image is read on the target itself
            raise OSError("simulated read failure")
        return real(self, tmp, keys)
    monkeypatch.setattr(_JsonHandler, "snapshot", only_on_the_private_copy)
    with caplog.at_level("ERROR", logger="api.routes"):
        body = _ok(await _delete(client, path, ["j:a"]))
    assert body["undo_token"] is None
    assert body["undo_unavailable"]["code"] == "undo_snapshot_failed"
    assert _JsonHandler.load(path) == {}
    assert any("post-image" in r.getMessage() for r in caplog.records)


# ──────────────────────── byte-exact Undo (journal byte patch) ───────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["flac_untagged", "flac_vorbis_picture", "mov", "mp4_tags", "m4a_tags", "mp3_untagged", "mp3_v24_full",
                                  "wav_untagged", "wv_untagged", "ogg_vorbis", "jpeg_no_exif", "jpeg_xmp_first", "jpeg_iptc",
                                  "pdf_basic", "pdf_xmp", "docx", "xlsx"])
async def test_undo_gives_the_original_bytes_back(client, tmp_path, name):
    """Adding a first tag and undoing it leaves a file identical to the original."""
    path = _build(name, tmp_path)
    original = path.read_bytes()
    record = (await client.get("/read", params={"path": str(path)})).json()
    field = next((a for a in record.get("addable_fields") or [] if a.get("field_type") in ("str", "text")), None)
    if field is None:       # nothing to add (PDF/Office/OLE): change an existing text field
        field = next((f for f in record["fields"] if f.get("editable") and f.get("field_type") in ("str", "text")
                      and not f.get("multi") and f["key"].split(":")[0] != "fs"), None)
    assert field is not None, f"{name}: no text field to exercise"
    multi = field.get("multi")
    token = _token(_ok(await _write(client, path, {field["key"]: ["undo-probe"] if multi else "undo-probe"})))
    assert path.read_bytes() != original
    _ok(await _undo(client, token))
    assert path.read_bytes() == original
    _no_leftovers(path)


@pytest.mark.asyncio
async def test_byte_patch_is_refused_for_a_file_changed_elsewhere(client, tmp_path):
    """The file changed in the patched region by a later save: the byte patch does not
    apply and the value-level restore keeps the newer change."""
    path = _build("flac_untagged", tmp_path)
    t1 = _token(_ok(await _write(client, path, {"tag:title": ["One"]})))
    _ok(await _write(client, path, {"tag:artist": [["Two"]][0]}))
    _ok(await _undo(client, t1))
    now = await _read(client, path)
    assert "tag:title" not in now and now["tag:artist"]["value"] == ["Two"]


def test_make_and_apply_patch_roundtrip(tmp_path):
    from api.byte_patch import apply_patch, make_patch
    before, after = tmp_path / "a.bin", tmp_path / "b.bin"
    head, tail = os.urandom(5000), os.urandom(3 * 1024 * 1024 + 7)
    for old_mid, new_mid in [(b"", b"NEW" * 900), (b"OLD" * 1000, b""), (b"abc", b"abd"), (b"x" * 10, b"y" * 4000)]:
        before.write_bytes(head + old_mid + tail)
        after.write_bytes(head + new_mid + tail)
        patch = make_patch(before, after)
        assert patch is not None
        assert apply_patch(after, patch) and after.read_bytes() == before.read_bytes()
        assert not apply_patch(after, patch)          # no longer the saved file


# ──────────────────────── second review fixes (B1, B2, B3) ───────────────────

@pytest.mark.asyncio
async def test_undo_chain_survives_a_save_that_wrote_mtime(client, tmp_path):
    """B1: any later rewrite moves fs:mtime; that must not make an older Undo a 409."""
    path = _build("jpeg_camera", tmp_path)
    t1 = _token(_ok(await _write(client, path, {"exif:Make": "Nikon", "fs:mtime": "2020-01-01T00:00:00+00:00"})))
    t2 = _token(_ok(await _write(client, path, {"exif:Model": "Z9"})))
    _ok(await _undo(client, t2))
    _ok(await _undo(client, t1))
    fields = await _read(client, path)
    assert (fields["exif:Make"]["value"], fields["exif:Model"]["value"]) == ("Canon", "EOS Test")


@pytest.mark.asyncio
async def test_undo_in_save_order_also_survives_mtime(client, tmp_path):
    path = _build("jpeg_camera", tmp_path)
    t1 = _token(_ok(await _write(client, path, {"exif:Make": "Nikon", "fs:mtime": "2020-01-01T00:00:00+00:00"})))
    t2 = _token(_ok(await _write(client, path, {"exif:Model": "Z9"})))
    _ok(await _undo(client, t1))
    _ok(await _undo(client, t2))


@pytest.mark.asyncio
async def test_mtime_changed_by_someone_else_is_still_a_conflict(client, tmp_path):
    path = _build("jpeg_camera", tmp_path)
    token = _token(_ok(await _write(client, path, {"exif:Make": "Nikon", "fs:mtime": "2020-01-01T00:00:00+00:00"})))
    stamp = path.stat().st_mtime_ns + 5_000_000_000
    os.utime(path, ns=(stamp, stamp))
    _error(await _undo(client, token), 409, "concurrent_modification")


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["mp3_v24_full", "flac_vorbis_picture", "ogg_vorbis", "m4a_tags"])
async def test_unchanged_save_does_not_replace_mutagen_files(client, tmp_path, name):
    """B2: writing back the value a tag already has leaves the file alone."""
    path = _build(name, tmp_path)
    record = await _read(client, path)
    key, field = next((k, f) for k, f in record.items()
                      if f["editable"] and not k.startswith("fs:") and f["value"] not in (None, "", []))
    before, data = path.stat(), path.read_bytes()
    body = _ok(await _write(client, path, {key: field["value"]}))
    assert body["undo_token"] is None
    after = path.stat()
    assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)
    assert path.read_bytes() == data
    _no_leftovers(path)


@pytest.mark.asyncio
async def test_io_error_during_the_byte_patch_falls_back_to_the_values(client, tmp_path, monkeypatch):
    """B3: a failure after the patch started writing must not leave a half patched file."""
    import api.byte_patch as byte_patch
    path = _build("flac_untagged", tmp_path)
    original = path.read_bytes()
    token = _token(_ok(await _write(client, path, {"tag:title": ["One"]})))
    real_apply = byte_patch.apply_patch

    def broken(target, patch):
        with open(target, "r+b") as stream:      # damage the copy, then fail like a full disk
            stream.seek(patch.prefix)
            stream.write(b"\0" * 8)
        raise OSError("No space left on device")

    monkeypatch.setattr(routes, "apply_patch", broken)
    _ok(await _undo(client, token))
    monkeypatch.setattr(routes, "apply_patch", real_apply)
    fields = await _read(client, path)
    assert "tag:title" not in fields
    assert path.read_bytes()[:4] == original[:4]
    _no_leftovers(path)


def test_byte_patch_never_evicts_other_entries():
    """B7: a patch that does not fit next to the existing entries is dropped, the values stay."""
    from api.byte_patch import BytePatch
    journal = UndoJournal(max_entries=50, max_bytes=100_000)
    snap = {"format": {"k": b"a" * 100}}
    path, ident = Path("f.bin"), (1, 1)
    first = journal.add_entry(path, "h", ident, snap, snap)
    big = BytePatch(0, 0, b"m" * 99_800, 10, "d")
    second = journal.add_entry(path, "h", ident, snap, snap, big)
    assert journal.get(first) is not None
    assert journal.get(second) is not None and journal.get(second).patch is None
    small = BytePatch(0, 0, b"m" * 100, 10, "d")
    third = journal.add_entry(path, "h", ident, snap, snap, small)
    assert journal.get(third).patch is not None and journal.get(first) is not None


def test_make_patch_gives_up_when_the_file_shrinks_while_it_is_read(tmp_path, monkeypatch):
    """A file shorter than its reported size (short reads) yields no patch, never an exception."""
    from api import byte_patch
    before, after = tmp_path / "b.bin", tmp_path / "a.bin"
    before.write_bytes(b"A" * 100 + b"x" + b"B" * 100)
    after.write_bytes(b"A" * 100 + b"y" + b"B" * 100)
    real = os.path.getsize
    monkeypatch.setattr(byte_patch.os.path, "getsize", lambda p: real(p) + 50 if str(p) == str(after) else real(p))
    byte_patch.make_patch(before, after)        # must not raise (None or a patch the digest check rejects)
