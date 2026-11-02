"""Filesystem-layer samples.

Owned by phase F2H: only that phase appends samples here.

The filesystem layer is not a primary handler (D3): through the registry these
files are read by their format handler (hachoir for the extension-less/unknown
ones, image/pdf/office for the hidden ones). ``tests/test_contract.py`` therefore
checks their format level, while ``tests/test_filesystem_handler.py`` runs the
same contract checkers on ``FilesystemHandler`` for every sample of this module.

``valid=False`` on the ``.txt`` samples only means "no recognizable format": the
hachoir fallback may report that as a read error. The filesystem layer itself
must read every sample here without read_errors or warnings (checked by
``test_filesystem_handler.py``).

Register with `@sample("name", ...)`; see `tests/factories/__init__.py`.
"""
from __future__ import annotations

import os
import stat
from pathlib import Path

from . import registrar

SAMPLES: list = []
sample = registrar(SAMPLES, __name__)

# fs:atime and fs:ctime legitimately change when a transaction replaces the file.
FS_VOLATILE = ("fs:atime", "fs:ctime")

# Fixed, deterministic times (ns since the epoch). The sub-microsecond part of
# MTIME_NS checks that an idempotent write does not truncate the stored value.
MTIME_NS = 1_700_000_000_123_456_789
ATIME_NS = 1_700_000_100 * 10**9
PRE1970_MTIME_NS = -2_208_988_800 * 10**9   # 1900-01-01T00:00:00Z
PRE1970_ATIME_NS = -631_152_000 * 10**9     # 1950-01-01T00:00:00Z

FILE_ATTRIBUTE_HIDDEN = 0x2

XATTR_TEXT = ("user.metalens.comment", "MetaLens è 日本".encode("utf-8"))
XATTR_BINARY = ("user.metalens.blob", b"\xff\xfe\x00\x80binary")


def _text_file(directory: Path, name: str) -> Path:
    path = directory / name
    path.write_bytes(b"MetaLens filesystem sample\n")
    return path


def _set_times(path: Path, atime_ns: int = ATIME_NS, mtime_ns: int = MTIME_NS) -> None:
    os.utime(path, ns=(atime_ns, mtime_ns))


def set_hidden(path: Path) -> None:
    """Add FILE_ATTRIBUTE_HIDDEN (Windows only), keeping the other attributes."""
    import ctypes
    from ctypes import wintypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.SetFileAttributesW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD]
    kernel32.SetFileAttributesW.restype = wintypes.BOOL
    attributes = os.stat(path).st_file_attributes | FILE_ATTRIBUTE_HIDDEN
    if not kernel32.SetFileAttributesW(str(path), attributes):
        raise ctypes.WinError(ctypes.get_last_error())


@sample("fs_plain", valid=False, volatile=FS_VOLATILE, tags={"fs_layer"})
def fs_plain(directory: Path) -> Path:
    path = _text_file(directory, "plain.txt")
    _set_times(path)
    return path


@sample("fs_readonly", valid=False, volatile=FS_VOLATILE, tags={"fs_layer", "read_only_target"})
def fs_readonly(directory: Path) -> Path:
    """Read-only file: READONLY attribute on Windows, mode 0o444 on POSIX."""
    path = _text_file(directory, "readonly.txt")
    _set_times(path)
    os.chmod(path, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
    return path


@sample("fs_pre1970", valid=False, volatile=FS_VOLATILE, tags={"fs_layer"})
def fs_pre1970(directory: Path) -> Path:
    """mtime 1900-01-01, atime 1950-01-01 (negative timestamps; Windows-safe reading)."""
    path = _text_file(directory, "pre1970.txt")
    _set_times(path, PRE1970_ATIME_NS, PRE1970_MTIME_NS)
    return path


@sample("fs_xattr_binary", platforms=("linux",), valid=False, volatile=FS_VOLATILE,
        tags={"fs_layer", "xattr"})
def fs_xattr_binary(directory: Path) -> Path:
    """One UTF-8 and one non-UTF-8 ``user.`` extended attribute (needs user xattr support)."""
    path = _text_file(directory, "xattr.txt")
    for name, value in (XATTR_TEXT, XATTR_BINARY):
        os.setxattr(path, name, value)
    _set_times(path)
    return path


@sample("fs_hidden_jpg", platforms=("win32",), volatile=FS_VOLATILE, tags={"fs_layer", "hidden"})
def fs_hidden_jpg(directory: Path) -> Path:
    import piexif
    from PIL import Image
    path = directory / "hidden.jpg"
    exif = piexif.dump({"0th": {piexif.ImageIFD.Make: b"MetaLens"}})
    Image.new("RGB", (4, 4), (10, 20, 30)).save(path, "JPEG", exif=exif)
    _set_times(path)
    set_hidden(path)
    return path


@sample("fs_hidden_pdf", platforms=("win32",), volatile=FS_VOLATILE, tags={"fs_layer", "hidden"})
def fs_hidden_pdf(directory: Path) -> Path:
    from pypdf import PdfWriter
    path = directory / "hidden.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    writer.add_metadata({"/Title": "Hidden", "/Author": "MetaLens"})
    with path.open("wb") as stream:
        writer.write(stream)
    _set_times(path)
    set_hidden(path)
    return path


@sample("fs_hidden_docx", platforms=("win32",), volatile=FS_VOLATILE, tags={"fs_layer", "hidden"})
def fs_hidden_docx(directory: Path) -> Path:
    import docx
    from datetime import datetime, timezone
    path = directory / "hidden.docx"
    document = docx.Document()
    document.add_paragraph("MetaLens")
    props = document.core_properties
    props.title = "Hidden"
    props.author = "MetaLens"
    props.created = props.modified = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    document.save(str(path))
    _set_times(path)
    set_hidden(path)
    return path
