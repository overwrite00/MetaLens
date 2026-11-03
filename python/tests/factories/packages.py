"""OpenDocument and EPUB samples (ZIP packages built with the standard library)."""
from __future__ import annotations

import zipfile
from pathlib import Path

from . import registrar

SAMPLES: list = []
sample = registrar(SAMPLES, __name__)

ODF_META = """<?xml version="1.0" encoding="UTF-8"?>
<office:document-meta xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
  xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:meta="urn:oasis:names:tc:opendocument:xmlns:meta:1.0">
 <office:meta>
  <meta:generator>LibreOffice/7.6</meta:generator>
  <dc:title>Relazione è</dc:title>
  <dc:subject>Budget</dc:subject>
  <dc:description>Descrizione</dc:description>
  <meta:initial-creator>Ada</meta:initial-creator>
  <dc:creator>Bruno</dc:creator>
  <meta:creation-date>2026-01-02T03:04:05</meta:creation-date>
  <dc:date>2026-02-03T04:05:06Z</dc:date>
  <dc:language>it-IT</dc:language>
  <meta:editing-cycles>3</meta:editing-cycles>
  <meta:editing-duration>PT1H2M3S</meta:editing-duration>
  <meta:keyword>alpha</meta:keyword>
  <meta:keyword>beta</meta:keyword>
  <meta:user-defined meta:name="Project">Mercury</meta:user-defined>
  <meta:document-statistic meta:page-count="4" meta:word-count="1200"/>
 </office:meta>
</office:document-meta>
"""

EPUB_CONTAINER = """<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
 <rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>
</container>
"""

EPUB_OPF = """<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id">
 <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
  <dc:identifier id="id">urn:isbn:9780000000002</dc:identifier>
  <dc:title>Il libro è</dc:title>
  <dc:creator>Ada Lovelace</dc:creator>
  <dc:creator>Charles Babbage</dc:creator>
  <dc:language>it</dc:language>
  <dc:publisher>Casa Editrice</dc:publisher>
  <dc:date>2026-03-04</dc:date>
  <dc:subject>Storia</dc:subject>
  <meta property="dcterms:modified">2026-03-05T06:07:08Z</meta>
 </metadata>
</package>
"""


def write_odf(path: Path, mimetype: str = "application/vnd.oasis.opendocument.text") -> Path:
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", mimetype, compress_type=zipfile.ZIP_STORED)
        z.writestr("content.xml", "<office:document-content xmlns:office='urn:oasis:names:tc:opendocument:xmlns:office:1.0'/>")
        z.writestr("meta.xml", ODF_META)
    return path


def write_epub(path: Path) -> Path:
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml", EPUB_CONTAINER)
        z.writestr("OEBPS/content.opf", EPUB_OPF)
    return path


@sample("odt_meta")
def odt_meta(tmp_path: Path) -> Path:
    return write_odf(tmp_path / "report.odt")


@sample("epub_book")
def epub_book(tmp_path: Path) -> Path:
    return write_epub(tmp_path / "book.epub")
