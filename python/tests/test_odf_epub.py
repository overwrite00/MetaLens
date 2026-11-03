"""OpenDocument and EPUB reading."""
import zipfile

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
    assert not record.format_supports_write and not any(f.editable or f.deletable for f in record.fields)


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
