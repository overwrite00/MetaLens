"""Legacy OLE (.doc/.xls/.ppt) property sets: decoding, types, read-only contract (F2F).

Samples are real compound files built by ``tests.factories.ole`` (no binaries in the repo).
"""
from __future__ import annotations

import hashlib
import json
import struct

import olefile
import pytest

from core.handlers.ole_handler import OleHandler
from tests.factories import ole as factory


def _read(path):
    record = OleHandler().read(path)
    return record, {f.key: f for f in record.fields}


@pytest.fixture
def doc_cp1252(tmp_path):
    return factory.build_office_file(tmp_path / "legacy.doc", 1252)


def test_factory_builds_a_file_olefile_accepts(doc_cp1252):
    assert olefile.isOleFile(str(doc_cp1252))
    with olefile.OleFileIO(str(doc_cp1252)) as ole:
        assert ole.exists(factory.SUMMARY_STREAM)
        assert ole.exists(factory.DOC_SUMMARY_STREAM)
        assert ole.getproperties(factory.SUMMARY_STREAM)[14] == 3


def test_lpstr_decoded_with_file_codepage(doc_cp1252):
    record, f = _read(doc_cp1252)
    assert record.read_errors == []
    assert f["ole:title"].value == "Relazione è"          # cp1252 0xE8, not U+FFFD
    assert f["ole:author"].value == "José Müller"
    assert f["ole:title"].field_type == "str"
    assert f["ole:comments"].value == "Riga 1\r\nRiga 2"
    assert f["ole:comments"].field_type == "text"
    assert f["ole:revision"].value == "7"


@pytest.mark.parametrize("codepage, suffix", [(65001, ".xls"), (1200, ".ppt")])
def test_unicode_codepages(tmp_path, codepage, suffix):
    path = factory.build_office_file(tmp_path / f"u{suffix}", codepage, title="日本 € è",
                                     author="Zoë 名前")
    record, f = _read(path)
    assert record.read_errors == [] and record.warnings == []
    assert f["ole:title"].value == "日本 € è"
    assert f["ole:author"].value == "Zoë 名前"
    assert f["ole:company"].value == "Società €uro S.p.A."
    assert f["ole:titles_of_parts"].value == ["Documento", "Foglio è", "Riepilogo"]
    assert f["ole:custom:Progetto"].value == "Città nuova"


def test_edit_time_is_a_duration_in_seconds(doc_cp1252):
    _, f = _read(doc_cp1252)
    edit = f["ole:edit_time"]
    assert edit.field_type == "duration"
    assert edit.value == pytest.approx(factory.EDIT_SECONDS) and type(edit.value) is float


def test_timestamps_are_utc_datetimes_and_zero_is_absent(doc_cp1252):
    _, f = _read(doc_cp1252)
    assert f["ole:create_time"].value == "2026-01-02T03:04:05+00:00"
    assert f["ole:create_time"].field_type == "datetime"
    assert f["ole:modified_time"].value == factory.MODIFIED.isoformat()
    assert "ole:last_print" not in f      # FILETIME 0 = never printed


def test_integers_are_typed(doc_cp1252):
    _, f = _read(doc_cp1252)
    assert (f["ole:num_pages"].value, f["ole:num_pages"].field_type) == (3, "int")
    assert f["ole:num_words"].value == 250 and f["ole:num_chars"].value == 1400
    assert f["ole:security"].value == 0 and f["ole:security"].field_type == "int"


def test_thumbnail_is_a_binary_descriptor(doc_cp1252):
    _, f = _read(doc_cp1252)
    thumb = f["ole:thumbnail"]
    assert thumb.field_type == "binary" and not thumb.editable and thumb.deletable
    assert thumb.value == {"size": len(factory.THUMBNAIL_CF),
                           "sha256": hashlib.sha256(factory.THUMBNAIL_CF).hexdigest(),
                           "mime": "image/bmp"}
    assert thumb.binary == thumb.value


def test_document_summary_information(doc_cp1252):
    _, f = _read(doc_cp1252)
    assert f["ole:category"].value == "Categoria"
    assert f["ole:manager"].value == "Direttore Società"
    assert f["ole:company"].value == "Società €uro S.p.A."
    assert (f["ole:bytes"].value, f["ole:lines"].value, f["ole:paragraphs"].value) == (24576, 12, 4)
    assert f["ole:chars_with_spaces"].value == 1650
    assert f["ole:scale_crop"].value is False and f["ole:scale_crop"].field_type == "bool"
    assert f["ole:links_up_to_date"].value is True
    assert f["ole:app_version"].value == 0x000C0000
    parts = f["ole:titles_of_parts"]
    assert parts.multi and parts.value == ["Documento", "Foglio è", "Riepilogo"]
    pairs = f["ole:heading_pairs"]
    assert pairs.multi and pairs.value == ["Titolo (1)", "Fogli (2)"]


def test_user_defined_properties(doc_cp1252):
    _, f = _read(doc_cp1252)
    assert f["ole:custom:Progetto"].value == "Città nuova"
    assert f["ole:custom:Approvato"].value is True
    assert (f["ole:custom:Revisione"].value, f["ole:custom:Revisione"].field_type) == (-5, "int")
    assert f["ole:custom:Scadenza"].value == factory.MODIFIED.isoformat()
    assert f["ole:custom:Scadenza"].field_type == "datetime"
    assert f["ole:custom:Nota"].value == "日本 €"
    assert f["ole:custom:Progetto"].label == "Custom: Progetto"


def test_everything_is_read_only_unique_and_json(doc_cp1252):
    record, f = _read(doc_cp1252)
    assert not record.supports_write and not record.format_supports_write      # values are never edited
    assert record.supports_delete and record.format_supports_delete            # but they can be removed
    keys = [x.key for x in record.fields]
    assert len(keys) == len(set(keys))
    assert all(not x.editable and x.deletable and x.source == "ole" for x in record.fields)
    body = json.dumps(record.to_dict(), allow_nan=False, ensure_ascii=False)
    assert "�" not in body


def test_summary_only_file(tmp_path):
    path = factory.build_office_file(tmp_path / "s.doc", 1252, doc_summary=False)
    record, f = _read(path)
    assert record.read_errors == []
    assert "ole:title" in f and "ole:company" not in f


def test_undecodable_text_falls_back_without_loss(tmp_path):
    # Bytes invalid in the declared codepage (UTF-8): Latin-1 fallback + warning, no U+FFFD.
    raw = b"Jos\xe9\0"
    sec = factory.section({1: factory.typed(factory.VT_I2, 65001 - 65536),
                           2: factory.typed(factory.VT_LPSTR, struct.pack("<I", len(raw)) + raw),
                           4: factory.typed(factory.VT_LPSTR, ("ok", 65001))})
    path = tmp_path / "bad.doc"
    path.write_bytes(factory.compound_file(
        {factory.SUMMARY_STREAM: factory.property_set((factory.FMTID_SUMMARY, sec))}))
    record, f = _read(path)
    assert f["ole:title"].value == "José"
    assert f["ole:author"].value == "ok"
    assert record.read_errors == []
    assert any("ole:title" in w for w in record.warnings)


def test_malformed_property_is_isolated(tmp_path):
    sec = bytearray(factory.section({1: factory.typed(factory.VT_I2, 1252),
                                     2: factory.typed(factory.VT_LPSTR, ("Titolo", 1252)),
                                     4: factory.typed(factory.VT_LPSTR, ("Autore", 1252))}))
    # Point PID 2 beyond the end of the section.
    struct.pack_into("<I", sec, 8 + 8 + 4, 0xFFFF)
    path = tmp_path / "m.doc"
    path.write_bytes(factory.compound_file(
        {factory.SUMMARY_STREAM: factory.property_set((factory.FMTID_SUMMARY, bytes(sec)))}))
    record, f = _read(path)
    assert "ole:title" not in f and f["ole:author"].value == "Autore"
    assert record.read_errors == []
    assert record.warnings


def test_truncated_property_stream_reports_a_read_error(tmp_path):
    path = tmp_path / "t.doc"
    path.write_bytes(factory.compound_file({factory.SUMMARY_STREAM: b"\xfe\xff\0\0"}))
    record = OleHandler().read(path)
    assert record.read_errors and record.fields == []


def test_not_an_ole_file(tmp_path):
    path = tmp_path / "x.doc"
    path.write_bytes(b"not an OLE file at all")
    record = OleHandler().read(path)
    assert record.read_errors == ["Not a valid OLE file"]


def test_values_are_not_edited_but_can_be_removed(doc_cp1252):
    with pytest.raises(NotImplementedError):
        OleHandler().write(doc_cp1252, [])
    OleHandler().delete(doc_cp1252, ["ole:title"])
    assert "ole:title" not in {f.key for f in OleHandler().read(doc_cp1252).fields}
