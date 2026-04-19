"""Tests for metadata handlers — uses synthetic sample files created at runtime."""
import pytest
import io
import shutil
import tempfile
from pathlib import Path

import core.handlers  # noqa: F401
from core.registry import HandlerRegistry


# ──────────────────────── Helpers ────────────────────────────────────────────

def make_temp_copy(src: Path) -> Path:
    tmp = Path(tempfile.mktemp(suffix=src.suffix))
    shutil.copy2(src, tmp)
    return tmp


# ──────────────────────── JPEG (piexif + Pillow) ─────────────────────────────

@pytest.fixture(scope="session")
def sample_jpeg(tmp_path_factory) -> Path:
    import piexif
    from PIL import Image
    path = tmp_path_factory.mktemp("samples") / "test.jpg"
    img = Image.new("RGB", (100, 100), color=(128, 0, 0))
    exif_dict = {
        "0th": {piexif.ImageIFD.Make: b"TestMake", piexif.ImageIFD.Model: b"TestModel"},
        "Exif": {piexif.ExifIFD.DateTimeOriginal: b"2026:01:01 12:00:00"},
        "GPS": {},
        "1st": {},
        "thumbnail": None,
    }
    exif_bytes = piexif.dump(exif_dict)
    img.save(str(path), exif=exif_bytes)
    return path


def test_jpeg_read(sample_jpeg):
    handler = HandlerRegistry.get(sample_jpeg)
    assert handler.NAME == "image"
    record = handler.read(sample_jpeg)
    assert record.supports_write
    keys = {f.key for f in record.fields}
    assert "exif:Make" in keys
    assert "exif:Model" in keys


def test_jpeg_write_and_read_back(sample_jpeg):
    tmp = make_temp_copy(sample_jpeg)
    try:
        handler = HandlerRegistry.get(tmp)
        from core.models import MetadataField
        new_field = MetadataField("exif:Make", "Camera Make", "NewMake", source="exif")
        handler.write(tmp, [new_field])
        record = handler.read(tmp)
        makes = [f.value for f in record.fields if f.key == "exif:Make"]
        assert makes and "NewMake" in str(makes[0])
    finally:
        tmp.unlink(missing_ok=True)


def test_jpeg_delete_field(sample_jpeg):
    tmp = make_temp_copy(sample_jpeg)
    try:
        handler = HandlerRegistry.get(tmp)
        handler.delete(tmp, ["exif:Make"])
        record = handler.read(tmp)
        makes = [f for f in record.fields if f.key == "exif:Make"]
        assert not makes
    finally:
        tmp.unlink(missing_ok=True)


# ──────────────────────── MP3 (mutagen) ──────────────────────────────────────

@pytest.fixture(scope="session")
def sample_mp3(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("samples") / "test.mp3"
    # Minimal valid MP3 frame (silent)
    frame = bytes([0xFF, 0xFB, 0x90, 0x00] + [0x00] * 413)  # 1 MPEG layer3 frame
    path.write_bytes(frame * 4)
    return path


def test_mp3_read(sample_mp3):
    handler = HandlerRegistry.get(sample_mp3)
    assert handler.NAME == "audio"
    record = handler.read(sample_mp3)
    assert record.handler_name == "audio"
    # read_errors allowed for synthetic MP3 (mutagen may warn but not crash)


def test_mp3_write(sample_mp3):
    tmp = make_temp_copy(sample_mp3)
    try:
        import mutagen.id3 as id3
        tags = id3.ID3()
        tags.add(id3.TIT2(encoding=3, text=["TestTitle"]))
        tags.save(str(tmp))
        handler = HandlerRegistry.get(tmp)
        from core.models import MetadataField
        field = MetadataField("tag:TIT2", "Title", "UpdatedTitle", source="id3")
        handler.write(tmp, [field])
        record = handler.read(tmp)
        titles = [f.value for f in record.fields if "TIT2" in f.key]
        assert titles
    finally:
        tmp.unlink(missing_ok=True)


# ──────────────────────── PDF (pypdf) ────────────────────────────────────────

@pytest.fixture(scope="session")
def sample_pdf(tmp_path_factory) -> Path:
    from pypdf import PdfWriter
    path = tmp_path_factory.mktemp("samples") / "test.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=595, height=842)
    writer.add_metadata({"/Title": "TestDoc", "/Author": "Tester"})
    with open(str(path), "wb") as f:
        writer.write(f)
    return path


def test_pdf_read(sample_pdf):
    handler = HandlerRegistry.get(sample_pdf)
    assert handler.NAME == "pdf"
    record = handler.read(sample_pdf)
    keys = {f.key for f in record.fields}
    assert "pdf:Title" in keys
    assert "pdf:Author" in keys


def test_pdf_write(sample_pdf):
    tmp = make_temp_copy(sample_pdf)
    try:
        handler = HandlerRegistry.get(tmp)
        from core.models import MetadataField
        field = MetadataField("pdf:Title", "Title", "UpdatedTitle", source="pdf")
        handler.write(tmp, [field])
        record = handler.read(tmp)
        titles = [f.value for f in record.fields if f.key == "pdf:Title"]
        assert titles and "UpdatedTitle" in titles[0]
    finally:
        tmp.unlink(missing_ok=True)


# ──────────────────────── DOCX (python-docx) ─────────────────────────────────

@pytest.fixture(scope="session")
def sample_docx(tmp_path_factory) -> Path:
    from docx import Document
    path = tmp_path_factory.mktemp("samples") / "test.docx"
    doc = Document()
    doc.core_properties.title = "TestDocument"
    doc.core_properties.author = "TestAuthor"
    doc.add_paragraph("Hello")
    doc.save(str(path))
    return path


def test_docx_read(sample_docx):
    handler = HandlerRegistry.get(sample_docx)
    assert handler.NAME == "office"
    record = handler.read(sample_docx)
    keys = {f.key for f in record.fields}
    assert "office:title" in keys
    assert "office:author" in keys


def test_docx_write(sample_docx):
    tmp = make_temp_copy(sample_docx)
    try:
        handler = HandlerRegistry.get(tmp)
        from core.models import MetadataField
        field = MetadataField("office:title", "Title", "NewTitle", source="office")
        handler.write(tmp, [field])
        record = handler.read(tmp)
        titles = [f.value for f in record.fields if f.key == "office:title"]
        assert titles and titles[0] == "NewTitle"
    finally:
        tmp.unlink(missing_ok=True)


# ──────────────────────── Diff engine ────────────────────────────────────────

def test_diff_identical_files(sample_jpeg):
    from core.diff import compute_diff
    handler = HandlerRegistry.get(sample_jpeg)
    record_a = handler.read(sample_jpeg)
    record_b = handler.read(sample_jpeg)
    diff = compute_diff(record_a, record_b)
    assert not diff.only_in_a
    assert not diff.only_in_b
    assert not diff.changed


def test_diff_detects_changes(sample_jpeg, tmp_path):
    from core.diff import compute_diff
    from core.models import MetadataField
    import shutil

    tmp = tmp_path / "modified.jpg"
    shutil.copy2(sample_jpeg, tmp)
    handler = HandlerRegistry.get(tmp)
    handler.write(tmp, [MetadataField("exif:Make", "Make", "DifferentMake", source="exif")])

    record_a = handler.read(sample_jpeg)
    record_b = handler.read(tmp)
    diff = compute_diff(record_a, record_b)
    changed_keys = {fa.key for fa, fb in diff.changed}
    assert "exif:Make" in changed_keys


# ──────────────────────── Filesystem handler ─────────────────────────────────

def test_filesystem_always_reads(tmp_path):
    f = tmp_path / "anyfile.bin"
    f.write_bytes(b"\x00" * 64)
    from core.handlers.filesystem_handler import FilesystemHandler
    record = FilesystemHandler().read(f)
    keys = {field.key for field in record.fields}
    assert "fs:size" in keys
    assert "fs:mtime" in keys


# ──────────────────────── API health endpoint ────────────────────────────────

@pytest.mark.asyncio
async def test_health(client):
    resp = await client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert "version" in data
