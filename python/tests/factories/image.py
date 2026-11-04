"""Image samples (JPEG/TIFF/PNG/WebP/GIF/BMP/DNG, EXIF/XMP/IPTC).

Owned by phase F2A: only that phase appends samples here. Every builder is
deterministic (Pillow encodes a constant image, metadata blocks are assembled
byte by byte).

Register with `@sample("name", ...)`; see `tests/factories/__init__.py`.
"""
from __future__ import annotations

import io
import struct
from pathlib import Path

from . import registrar

SAMPLES: list = []
sample = registrar(SAMPLES, __name__)

# IIM dates have their own syntax: the generic contract mutation cannot produce them.
_DATE_MUTATIONS = {
    "iptc:DateCreated": "20260203",
    "iptc:TimeCreated": "040506+0100",
}
# Pillow derives img:dpi from EXIF X/YResolution when there is no JFIF density.
_JPEG_VOLATILE = ("img:dpi",)

XMP_PACKET = """<?xpacket begin="﻿" id="W5M0MpCehiHzreSzNTczkc9d"?>
<x:xmpmeta xmlns:x="adobe:ns:meta/" x:xmptk="MetaLens test">
 <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
  <!-- preserved comment -->
  <rdf:Description rdf:about=""
    xmlns:dc="http://purl.org/dc/elements/1.1/"
    xmlns:xmp="http://ns.adobe.com/xap/1.0/"
    xmlns:photoshop="http://ns.adobe.com/photoshop/1.0/"
    xmlns:xmpMM="http://ns.adobe.com/xap/1.0/mm/"
    xmlns:stEvt="http://ns.adobe.com/xap/1.0/sType/ResourceEvent#"
    xmlns:exif="http://ns.adobe.com/exif/1.0/"
    xmp:CreatorTool="Tool &amp; Co"
    xmp:Rating="3"
    photoshop:City="Zürich">
   <dc:title>
    <rdf:Alt>
     <rdf:li xml:lang="x-default">Titolo è</rdf:li>
     <rdf:li xml:lang="de">Titel</rdf:li>
    </rdf:Alt>
   </dc:title>
   <dc:subject>
    <rdf:Bag>
     <rdf:li>alpha</rdf:li>
     <rdf:li>beta ß</rdf:li>
    </rdf:Bag>
   </dc:subject>
   <dc:creator>
    <rdf:Seq>
     <rdf:li>Ada</rdf:li>
    </rdf:Seq>
   </dc:creator>
   <xmp:CreateDate>2026-01-02T03:04:05+02:00</xmp:CreateDate>
   <exif:GPSLatitude>45,30.5N</exif:GPSLatitude>
   <xmpMM:History>
    <rdf:Seq>
     <rdf:li rdf:parseType="Resource">
      <stEvt:action>created</stEvt:action>
     </rdf:li>
    </rdf:Seq>
   </xmpMM:History>
  </rdf:Description>
 </rdf:RDF>
</x:xmpmeta>
""" + " " * 200 + """
<?xpacket end="w"?>"""

ICC_SEGMENT_PAYLOAD = b"ICC_PROFILE\x00\x01\x01" + bytes(range(64))


# ──────────────────────── builders (also used by test_image_handler) ─────────

def pillow_jpeg(size=(16, 12), color=(200, 30, 60), **save) -> bytes:
    from PIL import Image
    img = Image.new("RGB", size, color)
    for x in range(size[0]):
        img.putpixel((x, x % size[1]), (x * 10 % 256, 255 - x, 40))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=90, **save)
    return buf.getvalue()


def segment(marker: int, payload: bytes) -> bytes:
    return bytes((0xFF, marker)) + struct.pack(">H", len(payload) + 2) + payload


def split_jpeg(data: bytes) -> tuple[list[bytes], bytes]:
    """(header segments raw, tail from SOS) of a simple Pillow JPEG."""
    pos, segs = 2, []
    while data[pos + 1] != 0xDA:
        length = struct.unpack(">H", data[pos + 2:pos + 4])[0]
        segs.append(data[pos:pos + 2 + length])
        pos += 2 + length
    return segs, data[pos:]


def assemble(segments: list[bytes], tail: bytes) -> bytes:
    return b"\xff\xd8" + b"".join(segments) + tail


def jpeg_with(extra_front: list[bytes], *, keep_app0: bool = True, base: bytes | None = None) -> bytes:
    """Pillow JPEG with ``extra_front`` segments inserted after SOI (and APP0)."""
    segs, tail = split_jpeg(base or pillow_jpeg())
    app0 = [s for s in segs if s[1] == 0xE0]
    rest = [s for s in segs if s[1] != 0xE0]
    return assemble((app0 if keep_app0 else []) + extra_front + rest, tail)


def thumbnail_jpeg() -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (4, 4), (10, 20, 30)).save(buf, "JPEG", quality=50)
    return buf.getvalue()


def camera_exif(*, gps: bool = True, thumbnail: bool = True, make: bytes = b"Canon",
                makernote: bytes = b"VENDOR\x00\x01\x02\x03" * 4) -> bytes:
    import piexif
    exif = {
        "0th": {
            piexif.ImageIFD.Make: make,
            piexif.ImageIFD.Model: b"EOS Test",
            piexif.ImageIFD.Orientation: 1,
            piexif.ImageIFD.XResolution: (72, 1),
            piexif.ImageIFD.YResolution: (72, 1),
            piexif.ImageIFD.ResolutionUnit: 2,
            piexif.ImageIFD.Artist: b"Jos\xe9",          # Latin-1, not UTF-8 (N12)
            piexif.ImageIFD.DateTime: b"2026:01:02 03:04:05",
            piexif.ImageIFD.YCbCrPositioning: 2,
        },
        "Exif": {
            piexif.ExifIFD.ExposureTime: (1, 125),
            piexif.ExifIFD.FNumber: (28, 10),
            piexif.ExifIFD.ISOSpeedRatings: 200,
            piexif.ExifIFD.ExifVersion: b"0231",
            piexif.ExifIFD.DateTimeOriginal: b"2026:01:02 03:04:05",
            piexif.ExifIFD.DateTimeDigitized: b"    :  :     :  :  ",
            piexif.ExifIFD.ExposureBiasValue: (-1, 3),
            piexif.ExifIFD.FocalLength: (1234, 100),
            piexif.ExifIFD.SubjectArea: (10, 20),
            piexif.ExifIFD.MakerNote: makernote,
            piexif.ExifIFD.UserComment: b"ASCII\x00\x00\x00hello camera",
            piexif.ExifIFD.ComponentsConfiguration: b"\x01\x02\x03\x00",
            piexif.ExifIFD.ColorSpace: 1,
            piexif.ExifIFD.PixelXDimension: 16,
            piexif.ExifIFD.PixelYDimension: 12,
        },
        "GPS": {}, "Interop": {piexif.InteropIFD.InteroperabilityIndex: b"R98"},
        "1st": {}, "thumbnail": None,
    }
    if gps:
        exif["GPS"] = {
            piexif.GPSIFD.GPSVersionID: (2, 2, 0, 0),
            piexif.GPSIFD.GPSLatitudeRef: b"N",
            piexif.GPSIFD.GPSLatitude: ((45, 1), (30, 1), (1234, 100)),
            piexif.GPSIFD.GPSLongitudeRef: b"E",
            piexif.GPSIFD.GPSLongitude: ((9, 1), (11, 1), (0, 1)),
            piexif.GPSIFD.GPSAltitudeRef: 0,
            piexif.GPSIFD.GPSAltitude: (12345, 100),
        }
    if thumbnail:
        exif["1st"] = {piexif.ImageIFD.Compression: 6, piexif.ImageIFD.XResolution: (72, 1),
                       piexif.ImageIFD.YResolution: (72, 1), piexif.ImageIFD.ResolutionUnit: 2}
        exif["thumbnail"] = thumbnail_jpeg()
    return piexif.dump(exif)   # starts with b"Exif\0\0"


def iim(datasets: list[tuple[int, int, bytes]]) -> bytes:
    return b"".join(bytes((0x1C, r, d)) + struct.pack(">H", len(v)) + v for r, d, v in datasets)


def resource(rid: int, data: bytes, name: bytes = b"") -> bytes:
    pascal = bytes([len(name)]) + name
    if len(pascal) % 2:
        pascal += b"\x00"
    out = b"8BIM" + struct.pack(">H", rid) + pascal + struct.pack(">L", len(data)) + data
    return out + (b"\x00" if len(data) % 2 else b"")


IPTC_DATASETS = [
    (1, 90, b"\x1b%G"),
    (2, 0, b"\x00\x04"),
    (2, 5, "Oggetto è".encode()),
    (2, 25, b"one"),
    (2, 25, "due è".encode()),
    (2, 25, "三".encode()),
    (2, 55, b"20260102"),
    (2, 80, b"Ada Lovelace"),
    (2, 101, b"Italia"),
    (2, 120, "Riga 1\nRiga 2 €".encode()),
    (2, 135, b"it"),                                # unmapped: shown read-only
]
RESOLUTION_INFO = bytes(range(16))


def iptc_app13(datasets=IPTC_DATASETS) -> bytes:
    return segment(0xED, b"Photoshop 3.0\x00" + resource(0x03ED, RESOLUTION_INFO)
                   + resource(0x0404, iim(datasets)) + resource(0x0425, bytes(16)))


# ──────────────────────── vendor MakerNotes and MPO ──────────────────────────

CANON_MN_TEXT = b"Canon EOS TEST BODY\x00"     # 20 bytes, referenced from inside the MakerNote
NIKON_MN_TEXT = b"FINE   \x00"


def canon_makernote(offset: int) -> bytes:
    """Canon-style MakerNote: a bare big-endian IFD whose value offset is ABSOLUTE
    (from the TIFF header), pointing at a string stored inside the MakerNote."""
    ifd_len = 2 + 12 + 4
    return (struct.pack(">H", 1) + struct.pack(">HHLL", 0x0006, 2, len(CANON_MN_TEXT), offset + ifd_len)
            + bytes(4) + CANON_MN_TEXT)


def nikon_makernote() -> bytes:
    """Nikon type 3 MakerNote: own TIFF header; offsets are relative to that header."""
    entries = struct.pack(">HHL", 0x0001, 7, 4) + b"0210" + \
        struct.pack(">HHLL", 0x0004, 2, len(NIKON_MN_TEXT), 8 + 2 + 2 * 12 + 4)
    return b"Nikon\x00\x02\x10\x00\x00" + b"MM\x00*" + struct.pack(">L", 8) + \
        struct.pack(">H", 2) + entries + bytes(4) + NIKON_MN_TEXT


def makernote_location(tiff: bytes) -> tuple[int, int]:
    """(offset, count) of the MakerNote value in a TIFF block (independent parser)."""
    bo = "<" if tiff[:2] == b"II" else ">"

    def entries(off):
        for i in range(struct.unpack_from(bo + "H", tiff, off)[0]):
            yield struct.unpack_from(bo + "HHLL", tiff, off + 2 + 12 * i)
    ifd0 = struct.unpack_from(bo + "L", tiff, 4)[0]
    exif_ifd = next(v for t, _, _, v in entries(ifd0) if t == 0x8769)
    return next((v, c) for t, _, c, v in entries(exif_ifd) if t == 0x927C)


def canon_makernote_resolves(tiff: bytes) -> bool:
    """The Canon-style MakerNote's absolute pointer still reaches its string."""
    offset, count = makernote_location(tiff)
    mn = tiff[offset:offset + count]
    _tag, _typ, n, value = struct.unpack_from(">HHLL", mn, 2)
    return tiff[value:value + n] == CANON_MN_TEXT


def nikon_makernote_resolves(tiff: bytes) -> bool:
    offset, count = makernote_location(tiff)
    mn = tiff[offset:offset + count]
    base = 10                                      # embedded TIFF header
    _tag, _typ, n, value = struct.unpack_from(">HHLL", mn, base + 8 + 2 + 12)
    return mn[base + value:base + value + n] == NIKON_MN_TEXT


def vendor_camera_exif(vendor: str) -> bytes:
    """Camera EXIF (piexif layout) with a Canon-like or Nikon-like MakerNote and the
    Make NUL-padded as cameras store it (``b"Canon\\0\\0\\0\\0"``, shown as "Canon")."""
    if vendor == "nikon":
        return camera_exif(make=b"NIKON\x00\x00\x00", makernote=nikon_makernote())
    placeholder = canon_makernote(0)
    offset, _ = makernote_location(camera_exif(make=b"Canon\x00\x00\x00", makernote=placeholder)[6:])
    exif = camera_exif(make=b"Canon\x00\x00\x00", makernote=canon_makernote(offset))
    assert makernote_location(exif[6:])[0] == offset and canon_makernote_resolves(exif[6:])
    return exif


def mpo_frames():
    """The two frames of the MPO sample (RGB images with some structure)."""
    from PIL import Image
    first = Image.new("RGB", (48, 32), (10, 200, 30))
    second = Image.new("RGB", (32, 24), (250, 0, 0))
    for x in range(32):
        first.putpixel((x, x % 32), (x * 5 % 256, 20, 220))
        second.putpixel((x, x % 24), (x * 7 % 256, 40, 200))
    return first, second


def mpo_jpeg(*, exif: bytes | None = None) -> bytes:
    """Two-frame MPO written by Pillow: APP2 MPF in the first image, the second frame after its EOI."""
    first, second = mpo_frames()
    buf = io.BytesIO()
    options = {"exif": exif} if exif is not None else {}
    first.save(buf, "MPO", save_all=True, append_images=[second], quality=90, **options)
    return buf.getvalue()


# ──────────────────────── samples ────────────────────────────────────────────

@sample("jpeg_jfif_exif", volatile=_JPEG_VOLATILE, mutations=_DATE_MUTATIONS)
def jpeg_jfif_exif(tmp_path: Path) -> Path:
    import piexif
    path = tmp_path / "jfif_exif.jpg"
    exif = piexif.dump({"0th": {piexif.ImageIFD.Make: b"Maker", piexif.ImageIFD.Model: b"Model 1",
                                piexif.ImageIFD.Orientation: 1, piexif.ImageIFD.Copyright: b"(c) 2026"},
                        "Exif": {piexif.ExifIFD.DateTimeOriginal: b"2026:01:01 12:00:00"}})
    path.write_bytes(pillow_jpeg(exif=exif))
    return path


@sample("jpeg_camera", volatile=_JPEG_VOLATILE, mutations=_DATE_MUTATIONS)
def jpeg_camera(tmp_path: Path) -> Path:
    path = tmp_path / "camera.jpg"
    path.write_bytes(jpeg_with([segment(0xE1, camera_exif()), segment(0xE2, ICC_SEGMENT_PAYLOAD)],
                               keep_app0=False))
    return path


@sample("jpeg_xmp_first", volatile=_JPEG_VOLATILE, mutations=_DATE_MUTATIONS)
def jpeg_xmp_first(tmp_path: Path) -> Path:
    path = tmp_path / "xmp_first.jpg"
    xmp_seg = segment(0xE1, b"http://ns.adobe.com/xap/1.0/\x00" + XMP_PACKET.encode("utf-8"))
    path.write_bytes(jpeg_with([xmp_seg, segment(0xE1, camera_exif(thumbnail=False))]))
    return path


@sample("jpeg_iptc", volatile=_JPEG_VOLATILE, mutations=_DATE_MUTATIONS)
def jpeg_iptc(tmp_path: Path) -> Path:
    path = tmp_path / "iptc.jpg"
    path.write_bytes(jpeg_with([
        segment(0xE1, camera_exif(gps=False, thumbnail=False)),
        segment(0xEF, b"APP15 vendor data"),
        iptc_app13(),
        segment(0xFE, b"a JPEG comment"),
    ]))
    return path


@sample("jpeg_no_exif", volatile=_JPEG_VOLATILE, mutations=_DATE_MUTATIONS)
def jpeg_no_exif(tmp_path: Path) -> Path:
    path = tmp_path / "plain.jpg"
    path.write_bytes(pillow_jpeg())
    return path


@sample("jpeg_canon_makernote", volatile=(*_JPEG_VOLATILE, "maker:*"), mutations=_DATE_MUTATIONS)
def jpeg_canon_makernote(tmp_path: Path) -> Path:
    """Camera JPEG whose MakerNote holds ABSOLUTE offsets (Canon style); Make is
    stored NUL-padded, thumbnail in IFD1."""
    path = tmp_path / "canon.jpg"
    path.write_bytes(jpeg_with([segment(0xE1, vendor_camera_exif("canon"))]))
    return path


@sample("jpeg_nikon_makernote", volatile=(*_JPEG_VOLATILE, "maker:*"), mutations=_DATE_MUTATIONS)
def jpeg_nikon_makernote(tmp_path: Path) -> Path:
    """Camera JPEG whose MakerNote has its own TIFF header (Nikon type 3, relative offsets)."""
    path = tmp_path / "nikon.jpg"
    path.write_bytes(jpeg_with([segment(0xE1, vendor_camera_exif("nikon"))]))
    return path


@sample("jpeg_mpo", volatile=_JPEG_VOLATILE + ("mpf:image*:Size",), mutations=_DATE_MUTATIONS)
def jpeg_mpo(tmp_path: Path) -> Path:
    """Two-frame MPO (APP1 Exif, APP2 MPF, second frame after the first EOI)."""
    path = tmp_path / "stereo.jpg"
    path.write_bytes(mpo_jpeg(exif=camera_exif(gps=True, thumbnail=False)))
    return path


def icc_profile(description: str = "MetaLens test RGB", copyright_text: str = "Public domain") -> bytes:
    """Minimal valid ICC v2 display profile with ``desc`` and ``cprt`` tags."""
    def desc(text: str) -> bytes:
        raw = text.encode("ascii") + bytes(1)
        body = b"desc" + bytes(4) + struct.pack(">I", len(raw)) + raw + bytes(4 + 4 + 2 + 1 + 67)
        return body + bytes(-len(body) % 4)

    def text_tag(text: str) -> bytes:
        body = b"text" + bytes(4) + text.encode("ascii") + bytes(1)
        return body + bytes(-len(body) % 4)

    tags = [(b"desc", desc(description)), (b"cprt", text_tag(copyright_text))]
    table_size = 4 + 12 * len(tags)
    offset = 128 + table_size
    table, body = struct.pack(">I", len(tags)), b""
    for signature, data in tags:
        table += struct.pack(">4sII", signature, offset + len(body), len(data))
        body += data
    header = bytearray(128)
    struct.pack_into(">I", header, 0, 128 + len(table) + len(body))
    header[8:12] = bytes([2, 0x10, 0, 0])
    header[12:16], header[16:20], header[20:24] = b"mntr", b"RGB ", b"XYZ "
    struct.pack_into(">6H", header, 24, 2026, 1, 2, 3, 4, 5)
    header[36:40], header[40:44] = b"acsp", b"MSFT"
    struct.pack_into(">I", header, 64, 1)
    return bytes(header) + table + body


@sample("jpeg_icc_comment", volatile=_JPEG_VOLATILE, mutations=_DATE_MUTATIONS)
def jpeg_icc_comment(tmp_path: Path) -> Path:
    """JPEG with an ICC profile split in two APP2 segments, two comments and camera EXIF."""
    profile = icc_profile()
    half = len(profile) // 2
    parts = [segment(0xE2, b"ICC_PROFILE" + bytes(1) + bytes([n, 2]) + chunk)
             for n, chunk in ((1, profile[:half]), (2, profile[half:]))]
    comments = [segment(0xFE, "First comment é".encode("utf-8")), segment(0xFE, b"Second")]
    path = tmp_path / "profile.jpg"
    path.write_bytes(jpeg_with([segment(0xE1, camera_exif(thumbnail=False))] + parts + comments))
    return path


@sample("photo_no_extension", volatile=_JPEG_VOLATILE, mutations=_DATE_MUTATIONS)
def photo_no_extension(tmp_path: Path) -> Path:
    path = tmp_path / "photo"
    path.write_bytes(jpeg_with([segment(0xE1, camera_exif(thumbnail=False))]))
    return path


@sample("tiff_dpi")
def tiff_dpi(tmp_path: Path) -> Path:
    from PIL import Image
    from PIL.TiffImagePlugin import ImageFileDirectory_v2
    path = tmp_path / "scan.tif"
    info = ImageFileDirectory_v2()
    info[0x010F] = "ScannerCo"
    info[0x0131] = "Scan 1.0"
    Image.new("RGB", (8, 8), (1, 2, 3)).save(path, "TIFF", dpi=(300, 300), tiffinfo=info)
    return path


@sample("tiff_xmp_iptc", volatile=("exif:XMLPacket", "exif:tag_33723", "iptc:*"))
def tiff_xmp_iptc(tmp_path: Path) -> Path:
    """TIFF as saved by an editor: XMP packet (tag 700) and IPTC-NAA record (tag 33723)."""
    from PIL import Image
    from PIL.TiffImagePlugin import ImageFileDirectory_v2
    path = tmp_path / "edited.tif"
    info = ImageFileDirectory_v2()
    info[0x010F] = "EditorCo"
    info.tagtype[700] = 1
    info[700] = XMP_PACKET.encode("utf-8")
    info.tagtype[33723] = 7
    info[33723] = iim(IPTC_DATASETS)
    Image.new("RGB", (8, 8), (1, 2, 3)).save(path, "TIFF", tiffinfo=info)
    return path


@sample("dng_like")
def dng_like(tmp_path: Path) -> Path:
    from PIL import Image
    path = tmp_path / "raw.dng"
    Image.new("RGB", (8, 8), (4, 5, 6)).save(path, "TIFF", dpi=(240, 240))
    return path


def le_raw_tiff(magic: bytes, make: bytes, model: bytes) -> bytes:
    """Little-endian TIFF-like raw file (``magic`` in place of ``II*\0``) with a Make/Model IFD0 and
    a UTF-8 (Exif 3.0, type 129) ImageDescription."""
    entries = []
    body = bytearray()
    ifd_at, count = 8, 3
    data_at = ifd_at + 2 + 12 * count + 4
    for tag, typ, payload in ((0x010E, 129, "Descrizione è".encode("utf-8") + b"\x00"),
                              (0x010F, 2, make + b"\x00"), (0x0110, 2, model + b"\x00")):
        entries.append(struct.pack("<HHII", tag, typ, len(payload), data_at + len(body)))
        body += payload
    return magic + struct.pack("<I", ifd_at) + struct.pack("<H", count) + b"".join(entries)         + struct.pack("<I", 0) + bytes(body)


@sample("orf_like")
def orf_like(tmp_path: Path) -> Path:
    """Olympus-style raw: big-endian EXIF structure under the ``MMOR`` header."""
    path = tmp_path / "photo.orf"
    tiff = camera_exif(thumbnail=False)[6:]
    path.write_bytes(b"MMOR" + tiff[4:])
    return path


@sample("rw2_like")
def rw2_like(tmp_path: Path) -> Path:
    """Panasonic-style raw: little-endian IFD0 under the ``IIU\0`` header."""
    path = tmp_path / "photo.rw2"
    path.write_bytes(le_raw_tiff(b"IIU\x00", b"Panasonic", b"DC-Test"))
    return path


@sample("png_text_exif", mutations=_DATE_MUTATIONS)
def png_text_exif(tmp_path: Path) -> Path:
    from PIL import Image
    from PIL.PngImagePlugin import PngInfo
    path = tmp_path / "text.png"
    info = PngInfo()
    info.add_text("Title", "PNG title")
    info.add_text("Comment", "compressed comment " * 4, zip=True)
    info.add_itxt("Description", "Descrizione è 日本", lang="it", tkey="Beschreibung")
    info.add_itxt("Description", "Description", lang="en", tkey="Description")
    info.add_itxt("XML:com.adobe.xmp", XMP_PACKET)
    exif = camera_exif(gps=False, thumbnail=False)
    Image.new("RGB", (8, 8), (7, 8, 9)).save(path, "PNG", pnginfo=info, exif=exif)
    return path


@sample("webp_exif", mutations=_DATE_MUTATIONS)
def webp_exif(tmp_path: Path) -> Path:
    from PIL import Image
    path = tmp_path / "photo.webp"
    Image.new("RGB", (8, 8), (9, 8, 7)).save(path, "WEBP", exif=camera_exif(thumbnail=False),
                                             xmp=XMP_PACKET.encode("utf-8"), lossless=True)
    return path


@sample("gif_comment", tags={"read_only"})
def gif_comment(tmp_path: Path) -> Path:
    from PIL import Image
    path = tmp_path / "anim.gif"
    Image.new("P", (8, 8), 3).save(path, "GIF", comment=b"GIF comment \xe8")
    return path


@sample("bmp", tags={"read_only"})
def bmp(tmp_path: Path) -> Path:
    from PIL import Image
    path = tmp_path / "image.bmp"
    Image.new("RGB", (8, 8), (1, 1, 1)).save(path, "BMP")
    return path


# ── Canon CR3 and Fujifilm RAF (read-only samples) ──

def simple_tiff(entries: list[tuple[int, int, bytes]]) -> bytes:
    """Little-endian TIFF with one IFD: (tag, type, value bytes) entries, values stored after the table."""
    count = len(entries)
    data_at = 8 + 2 + 12 * count + 4
    table, body = b"", b""
    for tag, typ, value in sorted(entries):
        width = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8}[typ]
        n = len(value) // width
        if len(value) <= 4:
            table += struct.pack("<HHI", tag, typ, n) + value.ljust(4, bytes(1))
        else:
            table += struct.pack("<HHII", tag, typ, n, data_at + len(body))
            body += value
    return b"II*" + bytes(1) + struct.pack("<I", 8) + struct.pack("<H", count) + table + struct.pack("<I", 0) + body


def cr3_bytes() -> bytes:
    def atom(name: bytes, payload: bytes) -> bytes:       # ISO box: size + type + payload
        return struct.pack(">I", 8 + len(payload)) + name + payload

    cmt1 = simple_tiff([(0x010F, 2, b"Canon" + bytes(1)), (0x0110, 2, b"Canon EOS R5" + bytes(1)),
                        (0x013B, 2, b"Jane Doe" + bytes(1))])
    cmt2 = simple_tiff([(0x829A, 5, struct.pack("<II", 1, 250)), (0x8827, 3, struct.pack("<H", 400)),
                        (0xA431, 2, b"012345678" + bytes(1))])                        # body serial number
    cmt4 = simple_tiff([(0x0001, 2, b"N" + bytes(1)), (0x0002, 5, struct.pack("<IIIIII", 45, 1, 27, 1, 3000, 100)),
                        (0x0003, 2, b"E" + bytes(1)), (0x0004, 5, struct.pack("<IIIIII", 9, 1, 11, 1, 0, 1))])
    canon = atom(b"uuid", bytes.fromhex("85c0b687820f11e08111f4ce462b6a48") + atom(b"CNCV", b"CanonCR3_001/00.09.00/00.00.00")
                 + atom(b"CMT1", cmt1) + atom(b"CMT2", cmt2) + atom(b"CMT4", cmt4))
    return atom(b"ftyp", b"crx " + struct.pack(">I", 1) + b"crx isom") + atom(b"moov", canon) + atom(b"mdat", bytes(64))


def raf_bytes() -> bytes:
    preview = jpeg_with([segment(0xE1, camera_exif(thumbnail=False))])
    header = bytearray(0x100)
    header[:16] = b"FUJIFILMCCD-RAW "
    header[16:20] = b"0201"
    struct.pack_into(">II", header, 0x54, 0x100, len(preview))
    return bytes(header) + preview + bytes(64)


@sample("cr3_like", tags={"read_only"})
def cr3_like(tmp_path: Path) -> Path:
    path = tmp_path / "IMG_0001.cr3"
    path.write_bytes(cr3_bytes())
    return path


@sample("raf_like", tags={"read_only"})
def raf_like(tmp_path: Path) -> Path:
    path = tmp_path / "DSCF0001.raf"
    path.write_bytes(raf_bytes())
    return path
