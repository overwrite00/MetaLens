"""PDF samples (phase F2D): pdf_basic, pdf_xmp, pdf_encrypted, pdf_signed_like.

Built with pypdf at runtime. Every sample has one page with a real content
stream (text drawn with Helvetica) so tests can check that the document body
survives metadata edits.
"""
from __future__ import annotations

from pathlib import Path

from . import registrar

SAMPLES: list = []
sample = registrar(SAMPLES, __name__)

CONTENT = b"BT /F1 12 Tf 10 50 Td (Hello MetaLens) Tj ET"

XMP_PACKET = (
    '<?xpacket begin="﻿" id="W5M0MpCehiHzreSzNTczkc9d"?>\n'
    '<x:xmpmeta xmlns:x="adobe:ns:meta/">\n'
    ' <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">\n'
    '  <rdf:Description rdf:about=""'
    ' xmlns:dc="http://purl.org/dc/elements/1.1/"'
    ' xmlns:xmp="http://ns.adobe.com/xap/1.0/"'
    ' xmlns:pdf="http://ns.adobe.com/pdf/1.3/"'
    ' xmlns:ml="http://example.com/metalens/test/"'
    ' xmp:CreatorTool="Writer 1.0">\n'
    '   <dc:title><rdf:Alt><rdf:li xml:lang="x-default">Old Title</rdf:li>'
    '<rdf:li xml:lang="it-IT">Titolo</rdf:li></rdf:Alt></dc:title>\n'
    '   <dc:creator><rdf:Seq><rdf:li>Ada</rdf:li></rdf:Seq></dc:creator>\n'
    '   <pdf:Producer>Producer 2</pdf:Producer>\n'
    '   <xmp:CreateDate>2024-01-02T03:04:05+01:00</xmp:CreateDate>\n'
    '   <ml:Custom ml:flag="1">keep me</ml:Custom>\n'
    '  </rdf:Description>\n'
    ' </rdf:RDF>\n'
    '</x:xmpmeta>\n'
    '<?xpacket end="w"?>'
).encode("utf-8")


def new_writer():
    """PdfWriter with one page that draws "Hello MetaLens"."""
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
    writer = PdfWriter()
    page = writer.add_blank_page(width=200, height=100)
    font = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
    })
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)}),
    })
    stream = DecodedStreamObject()
    stream.set_data(CONTENT)
    page[NameObject("/Contents")] = writer._add_object(stream)
    return writer


def set_info(writer, info: dict) -> None:
    """Replace /Info with ``info`` (str values become text strings, PdfObjects stay)."""
    from pypdf.generic import DictionaryObject, NameObject, PdfObject, TextStringObject
    writer._info = DictionaryObject({
        NameObject(k): v if isinstance(v, PdfObject) else TextStringObject(v)
        for k, v in info.items()
    })


def add_xmp(writer, packet: bytes = XMP_PACKET) -> None:
    from pypdf.generic import DecodedStreamObject, NameObject
    stream = DecodedStreamObject()
    stream[NameObject("/Type")] = NameObject("/Metadata")
    stream[NameObject("/Subtype")] = NameObject("/XML")
    stream.set_data(packet)
    writer._root_object[NameObject("/Metadata")] = writer._add_object(stream)


def add_signature_field(writer, *, sig_flags: int = 3, signed: bool = True) -> None:
    """AcroForm with one /FT /Sig field; ``signed`` adds the /V signature dictionary."""
    from pypdf.generic import (ArrayObject, ByteStringObject, DictionaryObject, NameObject,
                               NumberObject, TextStringObject)
    field = DictionaryObject({
        NameObject("/FT"): NameObject("/Sig"),
        NameObject("/T"): TextStringObject("Signature1"),
    })
    if signed:
        field[NameObject("/V")] = writer._add_object(DictionaryObject({
            NameObject("/Type"): NameObject("/Sig"),
            NameObject("/Filter"): NameObject("/Adobe.PPKLite"),
            NameObject("/SubFilter"): NameObject("/adbe.pkcs7.detached"),
            NameObject("/ByteRange"): ArrayObject([NumberObject(0), NumberObject(10),
                                                   NumberObject(20), NumberObject(30)]),
            NameObject("/Contents"): ByteStringObject(bytes(64)),
        }))
    acro = DictionaryObject({NameObject("/Fields"): ArrayObject([writer._add_object(field)])})
    if sig_flags:
        acro[NameObject("/SigFlags")] = NumberObject(sig_flags)
    writer._root_object[NameObject("/AcroForm")] = writer._add_object(acro)


def basic_info() -> dict:
    from pypdf.generic import NameObject
    return {
        "/Title": "Basic Title",
        "/Author": "Ada Lovelace",
        "/Subject": "Subject è",
        "/Keywords": "alpha, beta",
        "/Creator": "Writer 1.0",
        "/Producer": "Producer 2",
        "/CreationDate": "D:20240102030405+01'00'",
        "/ModDate": "D:20240102030405Z",
        "/Trapped": NameObject("/False"),
        "/Custom": "custom value",
    }


def _save(writer, path: Path) -> Path:
    with path.open("wb") as fh:
        writer.write(fh)
    return path


@sample("pdf_basic")
def pdf_basic(tmp_path: Path) -> Path:
    writer = new_writer()
    set_info(writer, basic_info())
    return _save(writer, tmp_path / "basic.pdf")


@sample("pdf_xmp", volatile=("pdf:@xmp", "xmp:*"))
def pdf_xmp(tmp_path: Path) -> Path:
    writer = new_writer()
    set_info(writer, {
        "/Title": "Old Title",
        "/Author": "Ada",
        "/Creator": "Writer 1.0",
        "/Producer": "Producer 2",
        "/CreationDate": "D:20240102030405+01'00'",
        "/ModDate": "D:20240102030405",  # no offset: a local date-time
    })
    add_xmp(writer)
    return _save(writer, tmp_path / "xmp.pdf")


@sample("pdf_encrypted", tags={"read_only"})
def pdf_encrypted(tmp_path: Path) -> Path:
    writer = new_writer()
    set_info(writer, {"/Title": "Secret", "/Author": "Ada"})
    writer.encrypt(user_password="", owner_password="owner")
    return _save(writer, tmp_path / "encrypted.pdf")


@sample("pdf_signed_like", tags={"read_only"})
def pdf_signed_like(tmp_path: Path) -> Path:
    writer = new_writer()
    set_info(writer, {"/Title": "Signed", "/Author": "Ada"})
    add_signature_field(writer)
    return _save(writer, tmp_path / "signed.pdf")
