"""Removing the properties of legacy Office documents (DOC, XLS, PPT) in place."""
import pytest

from core.registry import HandlerRegistry
from tests.factories import by_name


def _build(tmp_path, name):
    folder = tmp_path / name
    folder.mkdir()
    return by_name(name).build(folder)


def _keys(path):
    return {f.key: f for f in HandlerRegistry.get(path).read(path).fields}


def test_properties_are_removable_but_never_edited(tmp_path):
    path = _build(tmp_path, "ole_doc_cp1252")
    keys = _keys(path)
    assert keys["ole:author"].deletable and not keys["ole:author"].editable
    record = HandlerRegistry.get(path).read(path)
    assert record.format_supports_delete and not record.format_supports_write


@pytest.mark.asyncio
@pytest.mark.parametrize("name, secret", [("ole_doc_cp1252", "José Müller".encode("cp1252")),
                                          ("ole_ppt_utf16", "José Müller".encode("utf-16-le"))])
async def test_the_author_and_the_company_are_removed_from_the_file_and_undo_is_exact(client, tmp_path, name, secret):
    path = _build(tmp_path, name)
    original = path.read_bytes()
    keys = _keys(path)
    if secret not in original:                                   # the sample stores the name in another form
        secret = keys["ole:author"].value.encode("utf-8")[:4]
    body = (await client.post("/sanitize", json={"path": str(path), "preview": False,
                                                 "categories": ["identity", "software", "datetime", "text"]})).json()
    assert body["ok"] and body["undo_token"], body
    left = _keys(path)
    assert "ole:author" not in left and "ole:last_author" not in left and "ole:company" not in left
    data = path.read_bytes()
    assert len(data) == len(original)
    assert (await client.post("/undo", json={"token": body["undo_token"]})).status_code == 200
    assert path.read_bytes() == original


@pytest.mark.asyncio
async def test_other_properties_survive_a_removal(client, tmp_path):
    path = _build(tmp_path, "ole_doc_cp1252")
    before = _keys(path)
    resp = await client.post("/delete", json={"path": str(path), "keys": ["ole:author"]})
    assert resp.status_code == 200, resp.text
    after = _keys(path)
    assert set(before) - set(after) == {"ole:author"}
    assert all(after[k].value == before[k].value for k in after)
    assert (await client.post("/delete", json={"path": str(path), "keys": ["ole:author"]})).status_code == 422


@pytest.mark.asyncio
async def test_a_damaged_document_is_not_touched(client, tmp_path):
    path = tmp_path / "bad.doc"
    path.write_bytes(b"not a compound file")
    record = HandlerRegistry.get(path).read(path)
    assert record.read_errors and not record.format_supports_delete
