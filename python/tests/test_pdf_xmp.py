"""The XMP metadata stream of a PDF: shown, removed as a whole, restored."""
import pytest

from core.registry import HandlerRegistry
from tests.factories import by_name


def _build(tmp_path, name):
    folder = tmp_path / name
    folder.mkdir()
    return by_name(name).build(folder)


def _keys(path):
    return {f.key: f for f in HandlerRegistry.get(path).read(path).fields}


def test_xmp_properties_are_shown_and_the_stream_can_be_removed(tmp_path):
    keys = _keys(_build(tmp_path, "pdf_xmp"))
    assert keys["xmp:dc:creator"].value == ["Ada"] and keys["xmp:xmp:CreatorTool"].value == "Writer 1.0"
    assert not keys["xmp:dc:creator"].editable and not keys["xmp:dc:creator"].deletable
    assert keys["pdf:@xmp"].deletable and keys["pdf:@xmp"].binary["mime"] == "application/rdf+xml"


@pytest.mark.asyncio
async def test_removing_the_stream_leaves_no_xmp_and_undo_brings_it_back(client, tmp_path):
    path = _build(tmp_path, "pdf_xmp")
    original = path.read_bytes()
    resp = await client.post("/delete", json={"path": str(path), "keys": ["pdf:@xmp"]})
    assert resp.status_code == 200, resp.text
    keys = _keys(path)
    assert "pdf:@xmp" not in keys and not any(k.startswith("xmp:") for k in keys)
    assert b"xmpmeta" not in path.read_bytes()                  # the packet itself is gone from the file
    undo = await client.post("/undo", json={"token": resp.json()["undo_token"]})
    assert undo.status_code == 200
    assert path.read_bytes() == original


@pytest.mark.asyncio
async def test_sharing_a_pdf_removes_the_author_and_the_hidden_xmp(client, tmp_path):
    path = _build(tmp_path, "pdf_xmp")
    body = (await client.post("/sanitize", json={"path": str(path), "preview": False,
                                                 "categories": ["identity", "software", "history", "text"]})).json()
    assert body["ok"], body
    keys = _keys(path)
    assert "pdf:Author" not in keys and not any(k.startswith("xmp:") for k in keys)


@pytest.mark.asyncio
async def test_signed_and_encrypted_pdfs_keep_their_xmp(client, tmp_path):
    for name in ("pdf_signed_like", "pdf_encrypted"):
        path = _build(tmp_path, name)
        assert not any(f.deletable for f in HandlerRegistry.get(path).read(path).fields), name
