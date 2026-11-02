"""PDF handler (phase F2D): typed dates, read-only /Trapped, signatures (D5),
XMP synchronisation (D6), exact snapshot/restore, encrypted files stay read-only."""
from __future__ import annotations

import io
import os
import sys
from dataclasses import replace
from pathlib import Path
from xml.dom import minidom

import pytest

from core.base_handler import ABSENT
from core.errors import MetadataValueError
from core.handlers import pdf_handler
from core.handlers.pdf_handler import PdfHandler
from core.models import MetadataField
from core.path_security import secure_atomic_write
from tests.factories import pdf as factory

RDF = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
DC = "http://purl.org/dc/elements/1.1/"
XMP = "http://ns.adobe.com/xap/1.0/"
PDFNS = "http://ns.adobe.com/pdf/1.3/"


def _reader(path: Path):
    from pypdf import PdfReader
    return PdfReader(io.BytesIO(path.read_bytes()))


def _info(path: Path) -> dict:
    info = _reader(path).trailer.get("/Info")
    return {} if info is None else dict(info.get_object())


def _xmp(path: Path) -> minidom.Document:
    root = _reader(path).trailer["/Root"]
    return minidom.parseString(root["/Metadata"].get_object().get_data())


def _xmp_texts(doc, ns, local) -> list[str]:
    out = []
    for el in doc.getElementsByTagNameNS(ns, local):
        out.append("".join(n.data for n in _walk(el) if n.nodeType == n.TEXT_NODE).strip())
    return out


def _walk(node):
    for child in node.childNodes:
        yield child
        yield from _walk(child)


def _fields(path: Path) -> dict[str, MetadataField]:
    return {f.key: f for f in PdfHandler().read(path).fields}


def _txn(path, fn):
    out = []
    secure_atomic_write(path, lambda tmp: out.append(fn(tmp)))
    return out[0] if out else None


def _page_body(path: Path) -> tuple:
    reader = _reader(path)
    return tuple((p.get_contents().get_data(), p.extract_text(), tuple(p.mediabox)) for p in reader.pages)


@pytest.fixture
def basic(tmp_path):
    return factory.pdf_basic(tmp_path)


@pytest.fixture
def xmp(tmp_path):
    return factory.pdf_xmp(tmp_path)


# ──────────────────────── PDF date <-> ISO ───────────────────────────────────

@pytest.mark.parametrize("raw, iso", [
    ("D:20240102030405+01'00'", "2024-01-02T03:04:05+01:00"),
    ("D:20240102030405Z", "2024-01-02T03:04:05+00:00"),
    ("D:20240102030405Z00'00'", "2024-01-02T03:04:05+00:00"),
    ("D:20240102030405-05'30'", "2024-01-02T03:04:05-05:30"),
    ("D:20240102030405+0100", "2024-01-02T03:04:05+01:00"),
    ("D:20240102030405+01'00", "2024-01-02T03:04:05+01:00"),
    ("20240102030405+01'00'", "2024-01-02T03:04:05+01:00"),
    ("D:20240102030405", "2024-01-02T03:04:05"),
    ("D:2024", "2024-01-01T00:00:00"),
    ("D:202401021504Z", "2024-01-02T15:04:00+00:00"),
])
def test_pdf_date_parsing(raw, iso):
    assert pdf_handler.pdf_date_to_iso(raw) == iso


@pytest.mark.parametrize("raw", ["garbage", "D:2024130101", "D:20240230", "D:20240102030405+25'00'", "", "D:20"])
def test_pdf_date_parsing_rejects_invalid(raw):
    assert pdf_handler.pdf_date_to_iso(raw) is None


@pytest.mark.parametrize("iso, raw", [
    ("2025-03-04T05:06:07+02:00", "D:20250304050607+02'00'"),
    ("2025-03-04T05:06:07-05:30", "D:20250304050607-05'30'"),
    ("2025-03-04T05:06:07+00:00", "D:20250304050607Z"),
    ("2025-03-04T05:06:07", "D:20250304050607"),
])
def test_iso_to_pdf_date(iso, raw):
    assert pdf_handler.iso_to_pdf_date(iso) == raw
    assert pdf_handler.pdf_date_to_iso(raw) == iso


# ──────────────────────── read model ─────────────────────────────────────────

def test_read_types(basic):
    f = _fields(basic)
    assert f["pdf:Title"].value == "Basic Title" and f["pdf:Title"].editable
    assert f["pdf:CreationDate"].field_type == "datetime"
    assert f["pdf:CreationDate"].value == "2024-01-02T03:04:05+01:00"
    assert f["pdf:ModDate"].value == "2024-01-02T03:04:05+00:00"
    assert f["pdf:CreationDate"].raw_value == "D:20240102030405+01'00'"
    assert f["pdf:encrypted"].field_type == "bool" and f["pdf:encrypted"].value is False
    assert f["pdf:signed"].field_type == "bool" and f["pdf:signed"].value is False
    assert f["pdf:page_count"].value == 1
    assert f["pdf:Custom"].value == "custom value" and f["pdf:Custom"].editable
    record = PdfHandler().read(basic)
    assert record.format_supports_write and record.read_only_reason is None


def test_trapped_is_read_only_name(basic):
    trapped = _fields(basic)["pdf:Trapped"]
    assert trapped.value == "False" and not trapped.editable
    with pytest.raises(MetadataValueError) as exc:
        PdfHandler().write(basic, [MetadataField("pdf:Trapped", "Trapped", "True")])
    assert exc.value.key == "pdf:Trapped"


def test_trapped_stays_a_name_after_other_writes(basic):
    from pypdf.generic import NameObject
    PdfHandler().write(basic, [MetadataField("pdf:Title", "Title", "New")])
    trapped = _info(basic)["/Trapped"]
    assert isinstance(trapped, NameObject) and trapped == "/False"


def test_offsetless_date_is_localdatetime(xmp):
    mod = _fields(xmp)["pdf:ModDate"]
    assert (mod.field_type, mod.value, mod.editable) == ("localdatetime", "2024-01-02T03:04:05", True)
    with pytest.raises(MetadataValueError):
        PdfHandler().write(xmp, [replace(mod, value="2024-01-02T03:04:05+01:00")])
    PdfHandler().write(xmp, [replace(mod, value="2025-06-07T08:09:10")])
    assert _info(xmp)["/ModDate"] == "D:20250607080910"


def test_unparsable_date_is_read_only_text(tmp_path):
    writer = factory.new_writer()
    factory.set_info(writer, {"/Title": "t", "/CreationDate": "yesterday"})
    path = tmp_path / "bad_date.pdf"
    writer.write(path)
    record = PdfHandler().read(path)
    f = {x.key: x for x in record.fields}["pdf:CreationDate"]
    assert (f.field_type, f.value, f.editable, f.deletable) == ("str", "yesterday", False, True)
    assert record.read_errors == [] and any("CreationDate" in w for w in record.warnings)


# ──────────────────────── validation ─────────────────────────────────────────

@pytest.mark.parametrize("key, value", [
    ("pdf:Title", None), ("pdf:CreationDate", None), ("pdf:CreationDate", "garbage"),
    ("pdf:CreationDate", "2024-01-02T03:04:05"),          # offset required (D7)
    ("pdf:CreationDate", "2024-01-02T03:04:05.5+01:00"),  # PDF dates have 1 s precision
    ("pdf:Title", 12), ("pdf:Title", ["a"]), ("pdf:Title", "bad \ud800 surrogate"),
])
def test_invalid_values_rejected_by_handler(basic, key, value):
    before = basic.read_bytes()
    with pytest.raises(MetadataValueError) as exc:
        PdfHandler().write(basic, [MetadataField(key, key, value)])
    assert exc.value.key == key
    assert basic.read_bytes() == before


@pytest.mark.asyncio
@pytest.mark.parametrize("key, value", [("pdf:Title", None), ("pdf:CreationDate", "garbage"),
                                        ("pdf:ModDate", None), ("pdf:CreationDate", "2024-01-02T03:04:05")])
async def test_api_rejects_invalid_values(client, basic, key, value):
    before = basic.read_bytes()
    resp = await client.post("/write", json={"path": str(basic), "fields": [{"key": key, "value": value}]})
    assert resp.status_code == 422, resp.text
    assert basic.read_bytes() == before


@pytest.mark.asyncio
async def test_api_iso_date_roundtrip(client, basic):
    resp = await client.post("/write", json={"path": str(basic), "fields": [
        {"key": "pdf:CreationDate", "value": "2025-03-04T05:06:07+02:00"}]})
    assert resp.status_code == 200, resp.text
    assert _info(basic)["/CreationDate"] == "D:20250304050607+02'00'"
    body = (await client.get("/read", params={"path": str(basic)})).json()
    served = {f["key"]: f for f in body["fields"]}
    assert served["pdf:CreationDate"]["value"] == "2025-03-04T05:06:07+02:00"
    assert served["pdf:CreationDate"]["field_type"] == "datetime"


def test_add_missing_standard_field(tmp_path):
    writer = factory.new_writer()
    factory.set_info(writer, {"/Title": "only"})
    path = tmp_path / "f.pdf"
    writer.write(path)
    handler = PdfHandler()
    record = handler.read(path)
    addable = {f.key: f for f in handler.writable_fields(path, record)}
    assert "pdf:Title" not in addable and "pdf:Trapped" not in addable
    assert addable["pdf:CreationDate"].field_type == "datetime"
    assert addable["pdf:Author"].field_type == "str"
    handler.write(path, [replace(addable["pdf:CreationDate"], value="2020-01-01T00:00:00+00:00")])
    assert _info(path)["/CreationDate"] == "D:20200101000000Z"


# ──────────────────────── preservation ───────────────────────────────────────

def test_write_preserves_page_content_and_other_info(basic):
    body = _page_body(basic)
    before = _info(basic)
    PdfHandler().write(basic, [MetadataField("pdf:Title", "Title", "MetaLens è 日本 €")])
    after = _info(basic)
    assert after["/Title"] == "MetaLens è 日本 €"
    assert {k: v for k, v in after.items() if k != "/Title"} == {k: v for k, v in before.items() if k != "/Title"}
    assert "/Producer" in after and after["/Producer"] == "Producer 2"  # no library producer injected
    assert _page_body(basic) == body


def test_write_of_current_values_does_not_touch_the_file(basic):
    before = basic.read_bytes()
    handler = PdfHandler()
    handler.write(basic, [f for f in handler.read(basic).fields if f.editable])
    assert basic.read_bytes() == before


def test_delete_preserves_other_entries(basic):
    PdfHandler().delete(basic, ["pdf:Trapped", "pdf:Custom"])
    info = _info(basic)
    assert "/Trapped" not in info and "/Custom" not in info and info["/Title"] == "Basic Title"


# ──────────────────────── snapshot / restore ─────────────────────────────────

def test_snapshot_restore_exact_utf16_bytes(tmp_path):
    from pypdf.generic import ByteStringObject, NameObject
    import codecs
    raw_title = codecs.BOM_UTF16_LE + "Tïtle".encode("utf-16-le")
    writer = factory.new_writer()
    factory.set_info(writer, {"/Author": "a"})
    writer._info[NameObject("/Title")] = ByteStringObject(raw_title)
    path = tmp_path / "utf16.pdf"
    writer.write(path)
    handler = PdfHandler()
    assert _fields(path)["pdf:Title"].value == "Tïtle"
    snap = _txn(path, lambda tmp: (handler.snapshot(tmp, ["pdf:Title"]),
                                   handler.apply_delete(tmp, ["pdf:Title"]))[0])
    assert "/Title" not in _info(path)
    _txn(path, lambda tmp: handler.restore(tmp, snap))
    assert _info(path)["/Title"].original_bytes == raw_title
    assert handler.snapshot(path, ["pdf:Title"]) == snap


def test_snapshot_of_absent_key(basic):
    handler = PdfHandler()
    handler.delete(basic, ["pdf:Subject"])
    assert handler.snapshot(basic, ["pdf:Subject"]) == {"pdf:Subject": ABSENT}


def test_restore_trapped_name_after_delete(basic):
    from pypdf.generic import NameObject
    handler = PdfHandler()
    snap = handler.snapshot(basic, ["pdf:Trapped"])
    handler.delete(basic, ["pdf:Trapped"])
    _txn(basic, lambda tmp: handler.restore(tmp, snap))
    assert isinstance(_info(basic)["/Trapped"], NameObject)


# ──────────────────────── XMP synchronisation (D6) ───────────────────────────

def test_xmp_title_author_and_tool_are_synchronised(xmp):
    PdfHandler().write(xmp, [MetadataField("pdf:Title", "Title", "New Title è"),
                             MetadataField("pdf:Author", "Author", "Grace"),
                             MetadataField("pdf:Creator", "Creator", "Tool 2")])
    doc = _xmp(xmp)
    lis = {li.getAttributeNS("http://www.w3.org/XML/1998/namespace", "lang"): li.firstChild.data
           for li in doc.getElementsByTagNameNS(RDF, "li") if li.parentNode.localName == "Alt"}
    assert lis == {"x-default": "New Title è", "it-IT": "Titolo"}
    assert _xmp_texts(doc, DC, "creator") == ["Grace"]
    desc = doc.getElementsByTagNameNS(RDF, "Description")[0]
    assert desc.getAttributeNS(XMP, "CreatorTool") == "Tool 2"
    # untouched XMP survives: custom namespace, attributes, packet wrapper
    custom = doc.getElementsByTagNameNS("http://example.com/metalens/test/", "Custom")[0]
    assert custom.firstChild.data == "keep me" and custom.getAttributeNS("http://example.com/metalens/test/", "flag") == "1"
    assert _xmp_texts(doc, PDFNS, "Producer") == ["Producer 2"]
    assert [n.target for n in doc.childNodes if n.nodeType == n.PROCESSING_INSTRUCTION_NODE] == ["xpacket", "xpacket"]


def test_xmp_dates_and_new_properties(xmp):
    PdfHandler().write(xmp, [
        MetadataField("pdf:CreationDate", "C", "2025-01-01T10:00:00-03:00"),
        MetadataField("pdf:ModDate", "M", "2025-02-02T11:12:13"),
        MetadataField("pdf:Keywords", "K", "x, y"),
    ])
    doc = _xmp(xmp)
    assert _xmp_texts(doc, XMP, "CreateDate") == ["2025-01-01T10:00:00-03:00"]
    assert _xmp_texts(doc, XMP, "ModifyDate") == ["2025-02-02T11:12:13"]
    assert _xmp_texts(doc, PDFNS, "Keywords") == ["x, y"]


def test_xmp_delete_and_restore_exact(xmp):
    handler = PdfHandler()
    keys = ["pdf:Title", "pdf:Creator", "pdf:Producer"]
    original_xmp = _xmp(xmp).toxml()
    snap = _txn(xmp, lambda tmp: (handler.snapshot(tmp, keys), handler.apply_delete(tmp, keys))[0])
    doc = _xmp(xmp)
    assert doc.getElementsByTagNameNS(DC, "title").length == 0
    assert doc.getElementsByTagNameNS(PDFNS, "Producer").length == 0
    assert not doc.getElementsByTagNameNS(RDF, "Description")[0].hasAttributeNS(XMP, "CreatorTool")
    assert _xmp_texts(doc, DC, "creator") == ["Ada"]
    _txn(xmp, lambda tmp: handler.restore(tmp, snap))
    assert handler.snapshot(xmp, keys) == snap
    doc = _xmp(xmp)
    assert doc.getElementsByTagNameNS(RDF, "Description")[0].getAttributeNS(XMP, "CreatorTool") == "Writer 1.0"
    assert _xmp_texts(doc, PDFNS, "Producer") == ["Producer 2"]
    assert _xmp_texts(doc, DC, "title") == ["Old TitleTitolo"]
    assert len(original_xmp) > 0


def test_xmp_restore_after_write(xmp):
    handler = PdfHandler()
    snap = _txn(xmp, lambda tmp: (handler.snapshot(tmp, ["pdf:Title"]),
                                  handler.apply_write(tmp, [MetadataField("pdf:Title", "T", "Z")]))[0])
    assert _xmp_texts(_xmp(xmp), DC, "title") == ["ZTitolo"]
    _txn(xmp, lambda tmp: handler.restore(tmp, snap))
    assert handler.snapshot(xmp, ["pdf:Title"]) == snap
    assert _info(xmp)["/Title"] == "Old Title"


def test_unparsable_xmp_is_a_warning_and_info_still_writes(tmp_path):
    writer = factory.new_writer()
    factory.set_info(writer, {"/Title": "t"})
    factory.add_xmp(writer, b"<not xml")
    path = tmp_path / "badxmp.pdf"
    writer.write(path)
    record = PdfHandler().read(path)
    assert record.read_errors == [] and any("XMP" in w for w in record.warnings)
    PdfHandler().write(path, [MetadataField("pdf:Title", "T", "new")])
    assert _info(path)["/Title"] == "new"


# ──────────────────────── signatures (D5) and encryption ─────────────────────

def _signed(tmp_path, variant) -> Path:
    from pypdf.generic import DictionaryObject, NameObject
    writer = factory.new_writer()
    factory.set_info(writer, {"/Title": "Signed"})
    if variant == "sigflags":
        factory.add_signature_field(writer, sig_flags=1, signed=False)
    elif variant == "sig_value":
        factory.add_signature_field(writer, sig_flags=0, signed=True)
    elif variant == "perms":
        writer._root_object[NameObject("/Perms")] = DictionaryObject(
            {NameObject("/DocMDP"): writer._add_object(DictionaryObject())})
    path = tmp_path / f"{variant}.pdf"
    writer.write(path)
    return path


@pytest.mark.parametrize("variant", ["sigflags", "sig_value", "perms"])
def test_signed_pdf_is_read_only(tmp_path, variant):
    path = _signed(tmp_path, variant)
    handler = PdfHandler()
    record = handler.read(path)
    assert not record.format_supports_write and not record.format_supports_delete
    assert record.read_only_reason and "sign" in record.read_only_reason.lower()
    assert all(not f.editable and not f.deletable for f in record.fields)
    assert {f.key: f.value for f in record.fields}["pdf:signed"] is True
    assert handler.writable_fields(path, record) == []
    before = path.read_bytes()
    with pytest.raises(ValueError, match="signed"):
        handler.write(path, [MetadataField("pdf:Title", "Title", "changed")])
    with pytest.raises(ValueError, match="signed"):
        handler.delete(path, ["pdf:Title"])
    assert path.read_bytes() == before


def test_unsigned_signature_field_stays_writable(tmp_path):
    writer = factory.new_writer()
    factory.set_info(writer, {"/Title": "t"})
    factory.add_signature_field(writer, sig_flags=0, signed=False)
    path = tmp_path / "empty_sig.pdf"
    writer.write(path)
    assert PdfHandler().read(path).format_supports_write


@pytest.mark.asyncio
async def test_api_refuses_signed_pdf(client, tmp_path):
    path = factory.pdf_signed_like(tmp_path)
    before = path.read_bytes()
    resp = await client.post("/write", json={"path": str(path), "fields": [{"key": "pdf:Title", "value": "x"}]})
    assert resp.status_code == 422
    resp = await client.post("/delete", json={"path": str(path), "keys": ["pdf:Title"]})
    assert resp.status_code == 422
    assert path.read_bytes() == before
    body = (await client.get("/read", params={"path": str(path)})).json()
    assert body["format_supports_write"] is False and "sign" in body["read_only_reason"].lower()


def test_encrypted_pdf_read_only_with_reason(tmp_path):
    path = factory.pdf_encrypted(tmp_path)
    handler = PdfHandler()
    record = handler.read(path)
    fields = {f.key: f for f in record.fields}
    assert fields["pdf:encrypted"].value is True and fields["pdf:encrypted"].field_type == "bool"
    assert fields["pdf:Title"].value == "Secret"
    assert record.read_only_reason and "encrypt" in record.read_only_reason.lower()
    assert all(not f.editable and not f.deletable for f in record.fields)
    before = path.read_bytes()
    with pytest.raises(ValueError, match="ncrypted"):
        _txn(path, lambda tmp: handler.apply_write(tmp, [MetadataField("pdf:Title", "T", "x")]))
    with pytest.raises(ValueError, match="ncrypted"):
        _txn(path, lambda tmp: handler.restore(tmp, {"pdf:Title": ABSENT}))
    assert path.read_bytes() == before


# ──────────────────────── Windows hidden files ───────────────────────────────

@pytest.mark.skipif(sys.platform != "win32", reason="hidden attribute is Windows-specific")
def test_apply_write_on_hidden_file(basic):
    import ctypes
    FILE_ATTRIBUTE_HIDDEN = 0x2
    assert ctypes.windll.kernel32.SetFileAttributesW(str(basic), FILE_ATTRIBUTE_HIDDEN)
    handler = PdfHandler()
    handler.apply_write(basic, [MetadataField("pdf:Title", "T", "hidden ok")])
    handler.apply_delete(basic, ["pdf:Custom"])
    assert _info(basic)["/Title"] == "hidden ok" and "/Custom" not in _info(basic)
    assert ctypes.windll.kernel32.GetFileAttributesW(str(basic)) & FILE_ATTRIBUTE_HIDDEN


# ──────────────────────── pypdf safety limits (never a 500) ──────────────────

def _raw_pdf(objects: dict[int, bytes], info: int | None = None) -> bytes:
    """Minimal PDF with a classic xref table; object 1 is the catalog."""
    out = io.BytesIO()
    out.write(b"%PDF-1.7\n")
    offsets = {}
    for number in sorted(objects):
        offsets[number] = out.tell()
        out.write(b"%d 0 obj\n" % number + objects[number] + b"\nendobj\n")
    size = max(objects) + 1
    xref = out.tell()
    out.write(b"xref\n0 %d\n" % size)
    for i in range(size):
        out.write(b"%010d 00000 n \n" % offsets[i] if i in offsets else b"0000000000 65535 f \n")
    trailer = b"<< /Size %d /Root 1 0 R" % size + (b" /Info %d 0 R" % info if info else b"") + b" >>"
    out.write(b"trailer\n" + trailer + b"\nstartxref\n%d\n%%%%EOF\n" % xref)
    return out.getvalue()


def _stream(dictionary: bytes, data: bytes) -> bytes:
    return b"<< " + dictionary + b" /Length %d >>\nstream\n" % len(data) + data + b"\nendstream"


def _image_pdf(image: bytes, catalog_extra: bytes = b"", kids: int = 1) -> bytes:
    content = b"q 200 0 0 200 0 0 cm /Im0 Do Q"
    objects = {
        1: b"<< /Type /Catalog /Pages 2 0 R " + catalog_extra + b" >>",
        4: _stream(b"/Type /XObject /Subtype /Image /Width %d /Height 1 /ColorSpace /DeviceGray "
                   b"/BitsPerComponent 8" % len(image), image),
        5: _stream(b"", content),
        6: b"<< /Title (Big) /Producer (hand) >>",
        7: b"[" + b" ".join(b"%d" % i for i in range(20)) + b"]",
    }
    pages = [3] + list(range(10, 10 + kids - 1))
    for number in pages:
        objects[number] = (b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] "
                           b"/Resources << /XObject << /Im0 4 0 R >> >> /Contents 5 0 R >>")
    objects[2] = b"<< /Type /Pages /Kids [" + b" ".join(b"%d 0 R" % n for n in pages) + \
        b"] /Count %d >>" % len(pages)
    return _raw_pdf(objects, info=6)


@pytest.fixture
def pypdf_defaults(monkeypatch):
    """Lower pypdf's default limits for every thread, so a small file exceeds them."""
    from contextvars import ContextVar
    import pypdf._configuration as configuration

    def lower(**limits):
        default = configuration.DEFAULT_CONFIGURATION.with_overwrites(**limits)
        monkeypatch.setattr(configuration, "CURRENT_CONFIGURATION",
                            ContextVar("pypdf_test_configuration", default=default))
    return lower


def _image_data(path: Path) -> bytes:
    from pypdf import PdfReader
    with pdf_handler._pypdf_limits(path):
        reader = PdfReader(io.BytesIO(path.read_bytes()))
        return reader.pages[0]["/Resources"]["/XObject"]["/Im0"].get_object().get_data()


@pytest.mark.asyncio
async def test_pdf_with_a_stream_longer_than_the_pypdf_limit_is_editable(client, tmp_path, pypdf_defaults):
    """A stream longer than pypdf's maximum declared length (75 MB by default) made
    the record editable but every write a 500 LimitReachedError."""
    from pypdf import PdfReader, PdfWriter
    from pypdf.errors import LimitReachedError
    pypdf_defaults(maximum_declared_stream_length=4096)
    image = os.urandom(10_000)
    path = tmp_path / "big.pdf"
    path.write_bytes(_image_pdf(image))
    with pytest.raises(LimitReachedError):      # what the handler used to hit
        PdfWriter(clone_from=PdfReader(io.BytesIO(path.read_bytes())))
    body = (await client.get("/read", params={"path": str(path)})).json()
    assert body["format_supports_write"] is True and body["read_errors"] == [] and body["read_only_reason"] is None
    resp = await client.post("/write", json={"path": str(path), "fields": [{"key": "pdf:Title", "value": "Nuovo"}]})
    assert resp.status_code == 200, resp.text
    assert _info(path)["/Title"] == "Nuovo"
    assert _image_data(path) == image           # the large stream is copied, not decoded
    resp = await client.post("/undo", json={"token": resp.json()["undo_token"]})
    assert resp.status_code == 200, resp.text
    assert _info(path)["/Title"] == "Big" and _image_data(path) == image


@pytest.mark.asyncio
async def test_other_pypdf_limit_while_rewriting_is_422_never_500(client, tmp_path, pypdf_defaults):
    pypdf_defaults(array_maximum_element_count=10)
    path = tmp_path / "array.pdf"
    path.write_bytes(_image_pdf(b"\x00" * 16, catalog_extra=b"/Custom 7 0 R"))   # 20-item array
    before = path.read_bytes()
    assert (await client.get("/read", params={"path": str(path)})).json()["format_supports_write"] is True
    resp = await client.post("/write", json={"path": str(path), "fields": [{"key": "pdf:Title", "value": "x"}]})
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert [e["code"] for e in body["errors"]] == ["too_large"]
    assert "safety limit" in body["detail"]
    assert path.read_bytes() == before
    others = sorted(p.name for p in path.parent.iterdir() if p != path)
    assert not others, others


@pytest.mark.asyncio
async def test_pypdf_limit_while_reading_makes_the_pdf_read_only(client, tmp_path, pypdf_defaults):
    pypdf_defaults(array_maximum_element_count=10)
    path = tmp_path / "pages.pdf"
    path.write_bytes(_image_pdf(b"\x00" * 16, kids=12))   # /Kids with 12 pages
    record = PdfHandler().read(path)
    assert record.read_errors == []
    assert record.read_only_reason and "safety limit" in record.read_only_reason
    assert not any(f.editable or f.deletable for f in record.fields)
    assert {f.key: f.value for f in record.fields}["pdf:Title"] == "Big"
    body = (await client.get("/read", params={"path": str(path)})).json()
    assert body["format_supports_write"] is False and "safety limit" in body["read_only_reason"]
    resp = await client.post("/write", json={"path": str(path), "fields": [{"key": "pdf:Title", "value": "x"}]})
    assert resp.status_code == 422 and resp.json()["errors"][0]["code"] == "read_only", resp.text


def test_pdf_limit_is_raised_only_to_the_file_size(tmp_path):
    """The declared-length limit follows the file (already fully in memory); the
    decompression limits keep pypdf's defaults."""
    from pypdf import get_configuration
    small = tmp_path / "small.pdf"
    small.write_bytes(b"x" * 10)
    default = get_configuration()
    with pdf_handler._pypdf_limits(small):
        assert get_configuration() is default
    big = tmp_path / "big.bin"
    with big.open("wb") as stream:
        stream.truncate(default.maximum_declared_stream_length + 5)   # sparse
    with pdf_handler._pypdf_limits(big):
        current = get_configuration()
        assert current.maximum_declared_stream_length == default.maximum_declared_stream_length + 5
        assert current.zlib_maximum_output_length == default.zlib_maximum_output_length
        assert current.xmp_maximum_input_length == default.xmp_maximum_input_length
    assert get_configuration() is default
