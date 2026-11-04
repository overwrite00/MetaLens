"""OpenDocument and EPUB reading."""
import zipfile

import pytest

from core import sniff
from core.registry import HandlerRegistry
from tests.factories import by_name, packages


def _build(tmp_path, name):
    folder = tmp_path / name
    folder.mkdir()
    return by_name(name).build(folder)


def _read(path):
    record = HandlerRegistry.get(path).read(path)
    return record, {f.key: f for f in record.fields}


def test_odf_properties(tmp_path):
    record, keys = _read(_build(tmp_path, "odt_meta"))
    assert record.read_errors == [] and record.handler_name == "odf"
    assert keys["odf:title"].value == "Relazione è"
    assert keys["odf:initial-creator"].value == "Ada" and keys["odf:creator"].value == "Bruno"
    assert keys["odf:keyword"].value == ["alpha", "beta"]
    assert keys["odf:creation-date"].field_type == "localdatetime" and keys["odf:date"].field_type == "datetime"
    assert keys["odf:editing-duration"].value == 3723
    assert keys["odf:editing-cycles"].value == 3
    assert keys["odf:stat.page-count"].value == 4
    assert keys["odf:custom.Project"].value == "Mercury"
    assert record.format_supports_write and keys["odf:title"].editable and keys["odf:creator"].deletable
    assert not keys["odf:stat.page-count"].editable and keys["odf:stat.page-count"].deletable


def test_epub_properties(tmp_path):
    record, keys = _read(_build(tmp_path, "epub_book"))
    assert record.read_errors == [] and record.handler_name == "epub"
    assert keys["epub:version"].value == "3.0"
    assert keys["epub:title"].value == ["Il libro è"]
    assert keys["epub:creator"].value == ["Ada Lovelace", "Charles Babbage"]
    assert keys["epub:modified"].value == "2026-03-05T06:07:08+00:00"


def test_packages_without_extension_are_found_by_content(tmp_path):
    odf = packages.write_odf(tmp_path / "doc")
    epub = packages.write_epub(tmp_path / "book")
    assert sniff.detect(odf) == "odf" and sniff.detect(epub) == "epub"
    assert HandlerRegistry.get(odf).NAME == "odf" and HandlerRegistry.get(epub).NAME == "epub"


def test_signed_document_is_flagged(tmp_path):
    path = packages.write_odf(tmp_path / "s.odt")
    with zipfile.ZipFile(path, "a") as z:
        z.writestr("META-INF/documentsignatures.xml", "<x/>")
    assert any("signed" in w for w in _read(path)[0].warnings)


def test_hostile_or_damaged_packages_are_read_errors_not_crashes(tmp_path):
    bad = tmp_path / "bad.odt"
    bad.write_bytes(b"not a zip")
    assert _read(bad)[0].read_errors
    dtd = tmp_path / "dtd.odt"
    with zipfile.ZipFile(dtd, "w") as z:
        z.writestr("meta.xml", '<!DOCTYPE x [<!ENTITY a "aaaa">]><x>&a;</x>')
    assert _read(dtd)[0].read_errors
    empty = tmp_path / "e.epub"
    with zipfile.ZipFile(empty, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
    assert _read(empty)[0].read_errors
    traversal = tmp_path / "t.epub"
    with zipfile.ZipFile(traversal, "w") as z:
        z.writestr("META-INF/container.xml",
                   packages.EPUB_CONTAINER.replace("OEBPS/content.opf", "../outside.opf"))
    assert _read(traversal)[0].read_errors


def test_pdf_version_and_linearization(tmp_path):
    from tests.factories import by_name as sample_by_name
    folder = tmp_path / "pdf"
    folder.mkdir()
    path = sample_by_name("pdf_basic").build(folder)
    _, keys = _read(path)
    assert keys["pdf:PDFVersion"].value.startswith("1.") and keys["pdf:PDFVersion"].computed
    assert keys["pdf:Linearized"].value is False


async def _write(client, path, fields=(), delete=()):
    return await client.post("/write", json={"path": str(path), "fields": list(fields), "delete": list(delete)})


@pytest.mark.asyncio
async def test_odf_edit_add_and_remove_keep_the_rest_of_the_package(client, tmp_path):
    path = _build(tmp_path, "odt_meta")
    before = {i.filename: zipfile.ZipFile(path).read(i.filename) for i in zipfile.ZipFile(path).infolist()}
    resp = await _write(client, path, [{"key": "odf:title", "value": "Nuovo titolo"},
                                       {"key": "odf:keyword", "value": ["uno", "due", "tre"]},
                                       {"key": "odf:custom.Project", "value": "Venus"}],
                        delete=["odf:creator", "odf:stat.word-count"])
    assert resp.status_code == 200, resp.text
    keys = _read(path)[1]
    assert keys["odf:title"].value == "Nuovo titolo" and keys["odf:keyword"].value == ["uno", "due", "tre"]
    assert keys["odf:custom.Project"].value == "Venus" and "odf:creator" not in keys and "odf:stat.word-count" not in keys
    after = {i.filename: zipfile.ZipFile(path).read(i.filename) for i in zipfile.ZipFile(path).infolist()}
    assert list(after) == list(before) and all(after[n] == before[n] for n in before if n != "meta.xml")
    assert zipfile.ZipFile(path).infolist()[0].filename == "mimetype"
    assert (await client.post("/undo", json={"token": resp.json()["undo_token"]})).status_code == 200
    restored = _read(path)[1]
    assert restored["odf:creator"].value == "Bruno" and restored["odf:keyword"].value == ["alpha", "beta"]
    assert restored["odf:stat.word-count"].value == 1200


@pytest.mark.asyncio
async def test_odf_can_receive_a_new_text(client, tmp_path):
    path = _build(tmp_path, "odt_meta")
    await _write(client, path, delete=["odf:subject"])
    handler = HandlerRegistry.get(path)
    assert "odf:subject" in {f.key for f in handler.writable_fields(path, handler.read(path))}
    resp = await _write(client, path, [{"key": "odf:subject", "value": "Bilancio"}])
    assert resp.status_code == 200, resp.text
    assert _read(path)[1]["odf:subject"].value == "Bilancio"


@pytest.mark.asyncio
async def test_epub_required_fields_stay_and_the_rest_can_go(client, tmp_path):
    path = _build(tmp_path, "epub_book")
    keys = _read(path)[1]
    assert not keys["epub:identifier"].deletable and not keys["epub:title"].deletable and keys["epub:title"].editable
    assert keys["epub:publisher"].deletable and keys["epub:creator"].deletable
    resp = await _write(client, path, [{"key": "epub:title", "value": ["Altro titolo"]}],
                        delete=["epub:publisher", "epub:creator", "epub:modified"])
    assert resp.status_code == 200, resp.text
    left = _read(path)[1]
    assert left["epub:title"].value == ["Altro titolo"]
    assert "epub:publisher" not in left and "epub:creator" not in left and "epub:modified" not in left
    assert (await _write(client, path, delete=["epub:identifier"])).status_code == 422
    assert zipfile.ZipFile(path).infolist()[0].filename == "mimetype"


@pytest.mark.asyncio
async def test_sharing_removes_the_author_from_odf_and_epub(client, tmp_path):
    for name, gone in (("odt_meta", "odf:initial-creator"), ("epub_book", "epub:creator")):
        path = _build(tmp_path, name)
        body = (await client.post("/sanitize", json={"path": str(path), "categories": ["identity"],
                                                     "preview": False})).json()
        assert body["ok"], body
        assert gone not in _read(path)[1], name


@pytest.mark.asyncio
async def test_a_signed_odf_is_not_changed(client, tmp_path):
    path = packages.write_odf(tmp_path / "s.odt")
    with zipfile.ZipFile(path, "a") as z:
        z.writestr("META-INF/documentsignatures.xml", "<x/>")
    record = _read(path)[0]
    assert not record.format_supports_write and not any(f.deletable or f.editable for f in record.fields)
