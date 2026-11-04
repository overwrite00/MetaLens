"""Removing application and custom properties from Office packages."""
import zipfile

import pytest

from core.registry import HandlerRegistry
from tests.factories import by_name


def _build(tmp_path, name):
    folder = tmp_path / name
    folder.mkdir()
    return by_name(name).build(folder)


def _keys(path):
    return {f.key: f for f in HandlerRegistry.get(path).read(path).fields}


def test_application_and_custom_properties_are_removable(tmp_path):
    keys = _keys(_build(tmp_path, "docx_app_custom"))
    removable = [k for k, f in keys.items() if k.startswith(("office:app.", "office:custom.")) and f.deletable]
    assert removable and any(k.startswith("office:custom.") for k in removable)
    assert any(k.startswith("office:app.") for k in removable)


@pytest.mark.asyncio
async def test_company_and_custom_data_are_removed_and_undo_is_exact(client, tmp_path):
    path = _build(tmp_path, "docx_app_custom")
    original = path.read_bytes()
    keys = _keys(path)
    sensitive = [k for k in keys if k.startswith("office:custom.") or k in ("office:app.Company", "office:app.Manager")]
    assert sensitive
    resp = await client.post("/write", json={"path": str(path), "fields": [], "delete": sensitive})
    assert resp.status_code == 200, resp.text
    left = _keys(path)
    assert not [k for k in sensitive if k in left]
    assert zipfile.ZipFile(path).testzip() is None
    undo = await client.post("/undo", json={"token": resp.json()["undo_token"]})
    assert undo.status_code == 200
    assert path.read_bytes() == original


@pytest.mark.asyncio
async def test_sharing_a_document_removes_the_author_and_the_company(client, tmp_path):
    path = _build(tmp_path, "docx_app_custom")
    body = (await client.post("/sanitize", json={"path": str(path), "categories": ["identity"],
                                                 "preview": False})).json()
    assert body["ok"], body
    left = _keys(path)
    assert "office:author" not in left and "office:last_modified_by" not in left
    assert "office:app.Company" not in left


@pytest.mark.asyncio
async def test_a_signed_package_is_not_touched(client, tmp_path):
    path = _build(tmp_path, "docx_signed_like")
    keys = _keys(path)
    assert not any(f.deletable for f in keys.values())
    body = (await client.post("/sanitize", json={"path": str(path), "categories": ["identity"],
                                                 "preview": False})).json()
    assert body["will_remove"] == [] and body["undo_token"] is None
