"""Office Open XML samples (phase F2E):
docx, xlsx, pptx, xlsm_macro, docx_signed_like, docx_app_custom.

Built with python-docx / openpyxl / python-pptx at runtime; extra package parts
(macros, signatures, app.xml/custom.xml) are added by rewriting the ZIP.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from . import registrar

SAMPLES: list = []
sample = registrar(SAMPLES, __name__)

APP_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"'
    ' xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">'
    '<Template>Normal.dotm</Template><TotalTime>42</TotalTime><Pages>3</Pages>'
    '<Words>120</Words><Application>Microsoft Office Word</Application>'
    '<DocSecurity>0</DocSecurity><ScaleCrop>false</ScaleCrop>'
    '<HeadingPairs><vt:vector size="2" baseType="variant"><vt:variant><vt:lpstr>Title</vt:lpstr>'
    '</vt:variant><vt:variant><vt:i4>1</vt:i4></vt:variant></vt:vector></HeadingPairs>'
    '<TitlesOfParts><vt:vector size="2" baseType="lpstr"><vt:lpstr>Part One</vt:lpstr>'
    '<vt:lpstr>Part Two</vt:lpstr></vt:vector></TitlesOfParts>'
    '<Company>ACME Srl è</Company><Manager>Grace</Manager><LinksUpToDate>false</LinksUpToDate>'
    '<SharedDoc>false</SharedDoc><HyperlinksChanged>false</HyperlinksChanged>'
    '<AppVersion>16.0000</AppVersion></Properties>'
).encode("utf-8")

CUSTOM_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/custom-properties"'
    ' xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">'
    '<property fmtid="{D5CDD505-2E9C-101B-9397-08002B2CF9AE}" pid="2" name="Project">'
    '<vt:lpwstr>MetaLens 日本</vt:lpwstr></property>'
    '<property fmtid="{D5CDD505-2E9C-101B-9397-08002B2CF9AE}" pid="3" name="Count">'
    '<vt:i4>7</vt:i4></property>'
    '<property fmtid="{D5CDD505-2E9C-101B-9397-08002B2CF9AE}" pid="4" name="Approved">'
    '<vt:bool>true</vt:bool></property>'
    '<property fmtid="{D5CDD505-2E9C-101B-9397-08002B2CF9AE}" pid="5" name="Due">'
    '<vt:filetime>2026-03-04T05:06:07Z</vt:filetime></property>'
    '<property fmtid="{D5CDD505-2E9C-101B-9397-08002B2CF9AE}" pid="6" name="Ratio">'
    '<vt:r8>0.25</vt:r8></property>'
    '</Properties>'
).encode("utf-8")

_CUSTOM_CT = "application/vnd.openxmlformats-officedocument.custom-properties+xml"
_CUSTOM_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/custom-properties"


def fill_core(props) -> None:
    """Set every core property python-docx / python-pptx expose."""
    props.title = "Quarterly report"
    props.author = "Ada Lovelace"
    props.subject = "Finance è"
    props.keywords = "alpha; beta"
    props.comments = "Line one"
    props.last_modified_by = "Grace"
    props.revision = 3
    props.category = "Reports"
    props.content_status = "Draft"
    props.identifier = "ID-1"
    props.language = "it-IT"
    props.version = "1.0"
    props.created = datetime(2024, 1, 2, 3, 4, 5)   # naive = UTC for these libraries
    props.modified = datetime(2024, 2, 3, 4, 5, 6)


def rewrite_zip(path: Path, replace: dict[str, bytes] | None = None,
                add: dict[str, bytes] | None = None) -> None:
    """Rewrite the package with some members replaced and others added."""
    replace, add = replace or {}, add or {}
    with ZipFile(path) as source:
        members = [(i, source.read(i)) for i in source.infolist()]
    with ZipFile(path, "w", ZIP_DEFLATED) as dest:
        for info, data in members:
            dest.writestr(info, replace.get(info.filename, data))
        for name, data in add.items():
            dest.writestr(name, data)


def build_docx(path: Path) -> Path:
    from docx import Document
    doc = Document()
    fill_core(doc.core_properties)
    doc.add_heading("MetaLens heading", level=1)
    doc.add_paragraph("First paragraph with è and 日本.")
    table = doc.add_table(rows=2, cols=2)
    for r in range(2):
        for c in range(2):
            table.cell(r, c).text = f"cell {r}{c}"
    doc.save(path)
    return path


@sample("docx")
def docx(tmp_path: Path) -> Path:
    return build_docx(tmp_path / "report.docx")


@sample("xlsx")
def xlsx(tmp_path: Path) -> Path:
    from openpyxl import Workbook
    path = tmp_path / "book.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.title = "Data"
    ws["A1"], ws["B1"], ws["A2"], ws["B2"] = "name", "value", "è 日本", 42
    ws["C2"] = "=B2*2"
    wb.properties.title = "Workbook title"
    wb.properties.creator = "Ada Lovelace"
    wb.properties.subject = "Numbers"
    wb.properties.keywords = "k1"
    wb.properties.description = "Desc"
    wb.properties.lastModifiedBy = "Grace"
    wb.properties.category = "Sheets"
    wb.properties.created = datetime(2024, 1, 2, 3, 4, 5)
    wb.properties.modified = datetime(2024, 2, 3, 4, 5, 6)
    wb.save(path)
    return path


@sample("pptx")
def pptx(tmp_path: Path) -> Path:
    from pptx import Presentation
    path = tmp_path / "deck.pptx"
    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[1])
    slide.shapes.title.text = "Slide title è"
    slide.placeholders[1].text = "Bullet 日本"
    fill_core(prs.core_properties)
    prs.save(path)
    return path


@sample("xlsm_macro")
def xlsm_macro(tmp_path: Path) -> Path:
    from openpyxl import Workbook
    path = tmp_path / "macro.xlsm"
    wb = Workbook()
    wb.active["A1"] = "macro book"
    wb.properties.title = "Macro workbook"
    wb.properties.creator = "Ada"
    wb.save(path)
    rewrite_zip(path, add={"xl/vbaProject.bin": b"\xd0\xcf\x11\xe0 fake macro project",
                           "customXml/item1.xml": b"<preserve/>"})
    return path


@sample("docx_signed_like", tags={"read_only"})
def docx_signed_like(tmp_path: Path) -> Path:
    path = build_docx(tmp_path / "signed.docx")
    rewrite_zip(path, add={"_xmlsignatures/origin.sigs": b"",
                           "_xmlsignatures/sig1.xml": b"<Signature/>"})
    return path


@sample("docx_app_custom")
def docx_app_custom(tmp_path: Path) -> Path:
    path = build_docx(tmp_path / "app_custom.docx")
    with ZipFile(path) as z:
        names = set(z.namelist())
        types = z.read("[Content_Types].xml").decode("utf-8")
        rels = z.read("_rels/.rels").decode("utf-8")
    types = types.replace("</Types>", f'<Override PartName="/docProps/custom.xml" ContentType="{_CUSTOM_CT}"/></Types>')
    rels = rels.replace("</Relationships>",
                        f'<Relationship Id="rIdCustom1" Type="{_CUSTOM_REL}" Target="docProps/custom.xml"/>'
                        "</Relationships>")
    replace = {"[Content_Types].xml": types.encode("utf-8"), "_rels/.rels": rels.encode("utf-8")}
    add = {"docProps/custom.xml": CUSTOM_XML}
    if "docProps/app.xml" in names:
        replace["docProps/app.xml"] = APP_XML
    else:
        add["docProps/app.xml"] = APP_XML
    rewrite_zip(path, replace=replace, add=add)
    return path
