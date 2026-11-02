"""IPTC datasets and their XMP twins stay in step inside one transaction."""
from __future__ import annotations

import pytest

from tests.factories import by_name


async def _read(client, path) -> dict:
    body = (await client.get("/read", params={"path": str(path)})).json()
    return {f["key"]: f for f in body["fields"]}


async def _write(client, path, fields=None, delete=None):
    resp = await client.post("/write", json={"path": str(path), "fields": [
        {"key": k, "value": v} for k, v in (fields or {}).items()], "delete": delete or []})
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _both(client, tmp_path):
    """A JPEG holding Keywords + Title in IPTC and in XMP, with the SAME values."""
    path = by_name("jpeg_no_exif").build(tmp_path)
    await _write(client, path, {"xmp:dc:subject": ["sea"], "xmp:dc:title": "Old"})
    await _write(client, path, {"iptc:Keywords": ["sea"], "iptc:ObjectName": "Old"})
    return path


@pytest.mark.asyncio
async def test_iptc_edit_updates_the_xmp_twin_and_undo_restores_both(client, tmp_path):
    both = await _both(client, tmp_path)
    original = both.read_bytes()
    body = await _write(client, both, {"iptc:Keywords": ["sea", "sun"], "iptc:ObjectName": "New"})
    fields = await _read(client, both)
    assert fields["xmp:dc:subject"]["value"] == ["sea", "sun"] and fields["xmp:dc:title"]["value"] == "New"
    assert fields["iptc:Keywords"]["value"] == ["sea", "sun"]
    assert any("xmp:dc:subject" in w for w in body["warnings"])
    assert (await client.post("/undo", json={"token": body["undo_token"]})).status_code == 200
    assert both.read_bytes() == original


@pytest.mark.asyncio
async def test_xmp_edit_updates_the_iptc_twin(client, tmp_path):
    both = await _both(client, tmp_path)
    await _write(client, both, {"xmp:dc:title": "From XMP"})
    fields = await _read(client, both)
    assert fields["iptc:ObjectName"]["value"] == "From XMP"


@pytest.mark.asyncio
async def test_deleting_one_side_deletes_the_twin(client, tmp_path):
    both = await _both(client, tmp_path)
    body = await _write(client, both, delete=["iptc:Keywords"])
    fields = await _read(client, both)
    assert "xmp:dc:subject" not in fields and "iptc:Keywords" not in fields
    assert (await client.post("/undo", json={"token": body["undo_token"]})).status_code == 200
    fields = await _read(client, both)
    assert fields["xmp:dc:subject"]["value"] == ["sea"] and fields["iptc:Keywords"]["value"] == ["sea"]


@pytest.mark.asyncio
async def test_both_sides_in_one_request_are_not_overridden(client, tmp_path):
    both = await _both(client, tmp_path)
    await _write(client, both, {"iptc:ObjectName": "I", "xmp:dc:title": "X"})
    fields = await _read(client, both)
    assert fields["iptc:ObjectName"]["value"] == "I" and fields["xmp:dc:title"]["value"] == "X"


@pytest.mark.asyncio
async def test_twin_is_never_created(client, tmp_path):
    path = by_name("jpeg_no_exif").build(tmp_path)
    await _write(client, path, {"xmp:dc:subject": ["a"]})
    body = await _write(client, path, {"iptc:City": "Rome"})
    fields = await _read(client, path)
    assert "xmp:photoshop:City" not in fields and fields["iptc:City"]["value"] == "Rome"
    assert not any("photoshop" in w for w in body["warnings"])


@pytest.mark.asyncio
async def test_saving_an_unchanged_value_leaves_the_twin_alone(client, tmp_path):
    both = await _both(client, tmp_path)
    await _write(client, both, {"xmp:dc:title": "Different"})        # twin follows: both "Different"
    # make them differ on purpose through the XMP side only, then re-save the IPTC value unchanged
    await _write(client, both, {"iptc:ObjectName": "Different"})
    before = both.read_bytes()
    body = await _write(client, both, {"iptc:ObjectName": "Different"})
    assert body["undo_token"] is None and both.read_bytes() == before


@pytest.mark.asyncio
async def test_twin_that_cannot_store_the_value_is_skipped_with_a_note(client, tmp_path):
    both = await _both(client, tmp_path)
    long_title = "x" * 40000          # above the 32767-byte IPTC dataset limit
    body = await _write(client, both, {"xmp:dc:title": long_title})
    fields = await _read(client, both)
    assert fields["xmp:dc:title"]["value"] == long_title and fields["iptc:ObjectName"]["value"] == "Old"
    assert any("iptc:ObjectName" in w for w in body["warnings"])


@pytest.mark.asyncio
async def test_twin_that_does_not_fit_its_segment_is_skipped_with_a_note(client, tmp_path):
    """B6: the XMP twin of a long caption no longer fits the 64 KiB segment. The IPTC edit that
    was asked for is saved, the twin is left as it was and the response says so."""
    path = by_name("jpeg_no_exif").build(tmp_path)
    await _write(client, path, {"xmp:dc:description": "short", "xmp:dc:title": "t" * 60000})
    await _write(client, path, {"iptc:Caption-Abstract": "short"})
    body = await _write(client, path, {"iptc:Caption-Abstract": "y" * 10000})
    fields = await _read(client, path)
    assert fields["iptc:Caption-Abstract"]["value"] == "y" * 10000
    assert fields["xmp:dc:description"]["value"] == "short"
    assert any("xmp:dc:description" in w and "not updated" in w for w in body["warnings"])
    assert not any(w.endswith("updated to match") for w in body["warnings"])


@pytest.mark.asyncio
async def test_deleting_an_iptc_dataset_keeps_a_twin_with_other_languages(client, tmp_path):
    """B8: the XMP title holds x-default + German. Deleting the IPTC object name must not
    take the German title with it: the twin stays, the response says so, the Undo is exact."""
    path = by_name("jpeg_xmp_first").build(tmp_path)
    await _write(client, path, {"iptc:ObjectName": "Titolo"})
    before = path.read_bytes()
    assert (await _read(client, path))["xmp:dc:title"]["value"] == "Titolo"
    body = await _write(client, path, delete=["iptc:ObjectName"])
    fields = await _read(client, path)
    assert "iptc:ObjectName" not in fields and fields["xmp:dc:title"]["value"] == "Titolo"
    assert b"Titel" in path.read_bytes()
    assert any("xmp:dc:title" in w and "other language" in w for w in body["warnings"])
    assert (await client.post("/undo", json={"token": body["undo_token"]})).status_code == 200
    assert path.read_bytes() == before


@pytest.mark.asyncio
async def test_deleting_an_iptc_dataset_still_removes_a_single_language_twin(client, tmp_path):
    both = await _both(client, tmp_path)
    body = await _write(client, both, delete=["iptc:ObjectName"])
    fields = await _read(client, both)
    assert "xmp:dc:title" not in fields and "iptc:ObjectName" not in fields
    assert not any("other language" in w for w in body["warnings"])
