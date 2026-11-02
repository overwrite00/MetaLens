"""secure_atomic_write: typed transaction errors and Windows file attributes (phase F3A).

Hidden/system files used to fail: ``shutil.copy2`` copies the HIDDEN/SYSTEM
attributes to the temporary file and libraries that rewrite it by pathname
(``open(..., "wb")`` = CREATE_ALWAYS) get "access denied". The transaction now
clears them on the temporary copy and puts the original attributes back.
"""
from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

from core.errors import ConcurrentModificationError, TargetNotWritableError
from core import path_security as security
from core.path_security import PathSecurityError, secure_atomic_write

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows file attributes")

HIDDEN, SYSTEM, READONLY, ARCHIVE = 0x2, 0x4, 0x1, 0x20


def _attrs(path: Path) -> int:
    import ctypes
    value = ctypes.windll.kernel32.GetFileAttributesW(str(path))
    assert value != 0xFFFFFFFF and value != -1
    return value


def _set_attrs(path: Path, value: int) -> None:
    import ctypes
    assert ctypes.windll.kernel32.SetFileAttributesW(str(path), value)


def _no_leftovers(path: Path) -> None:
    others = sorted(p.name for p in path.parent.iterdir() if p != path)
    assert not others, f"transaction leftovers: {others}"


def _rewrite(tmp: Path) -> None:
    tmp.write_bytes(b"rewritten by path")  # CREATE_ALWAYS, like pypdf/zipfile writers


# ──────────────────────── typed errors (all platforms) ───────────────────────

def test_read_only_target_raises_target_not_writable(tmp_path):
    if sys.platform != "win32" and hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("root ignores POSIX write permissions")
    target = tmp_path / "file.txt"
    target.write_text("original")
    os.chmod(target, stat.S_IREAD)
    try:
        with pytest.raises(TargetNotWritableError):
            secure_atomic_write(target, _rewrite)
        assert target.read_text() == "original"
        _no_leftovers(target)
    finally:
        os.chmod(target, stat.S_IREAD | stat.S_IWRITE)


def test_concurrent_change_raises_concurrent_modification(tmp_path):
    target = tmp_path / "file.txt"
    target.write_text("original")

    def racing(tmp):
        tmp.write_text("new")
        target.write_text("external change")
    with pytest.raises(ConcurrentModificationError):
        secure_atomic_write(target, racing)
    assert target.read_text() == "external change"
    _no_leftovers(target)


def test_typed_errors_remain_path_security_errors():
    assert issubclass(TargetNotWritableError, PathSecurityError)
    assert issubclass(ConcurrentModificationError, PathSecurityError)


# ──────────────────────── Windows attributes ─────────────────────────────────

@windows_only
@pytest.mark.parametrize("extra", [HIDDEN, HIDDEN | SYSTEM, SYSTEM])
def test_hidden_or_system_file_is_writable_and_keeps_attributes(tmp_path, extra):
    target = tmp_path / "file.bin"
    target.write_bytes(b"original")
    _set_attrs(target, _attrs(target) | extra)
    before = _attrs(target)
    seen = []

    def check_tmp(tmp):
        seen.append(_attrs(tmp))
        _rewrite(tmp)
    secure_atomic_write(target, check_tmp)
    assert target.read_bytes() == b"rewritten by path"
    assert seen and not seen[0] & (HIDDEN | SYSTEM | READONLY)
    assert _attrs(target) & (HIDDEN | SYSTEM) == before & (HIDDEN | SYSTEM)
    _no_leftovers(target)


@windows_only
def test_hidden_file_keeps_named_streams(tmp_path):
    target = tmp_path / "file.txt"
    target.write_text("original")
    stream = Path(str(target) + ":private")
    stream.write_text("preserve")
    _set_attrs(target, _attrs(target) | HIDDEN)
    secure_atomic_write(target, _rewrite)
    assert stream.read_text() == "preserve"
    assert _attrs(target) & HIDDEN
    _no_leftovers(target)


@windows_only
def test_hidden_file_failure_keeps_original_and_attributes(tmp_path):
    target = tmp_path / "file.txt"
    target.write_bytes(b"original")
    _set_attrs(target, _attrs(target) | HIDDEN)
    before = _attrs(target)

    def fail(tmp):
        _rewrite(tmp)
        raise ValueError("handler failure")
    with pytest.raises(ValueError):
        secure_atomic_write(target, fail)
    assert target.read_bytes() == b"original"
    assert _attrs(target) == before
    _no_leftovers(target)


@windows_only
def test_attributes_applied_to_target_when_replace_drops_them(tmp_path, monkeypatch):
    target = tmp_path / "file.txt"
    target.write_bytes(b"original")
    _set_attrs(target, _attrs(target) | HIDDEN)
    real = security._windows_replace

    def replace_dropping_attributes(original, replacement, backup):
        _set_attrs(replacement, ARCHIVE)
        real(original, replacement, backup)
        _set_attrs(original, ARCHIVE)  # simulate a replace that loses HIDDEN
    monkeypatch.setattr(security, "_windows_replace", replace_dropping_attributes)
    secure_atomic_write(target, _rewrite)
    assert _attrs(target) & HIDDEN
    _no_leftovers(target)


@windows_only
def test_read_only_attribute_is_target_not_writable(tmp_path):
    target = tmp_path / "file.txt"
    target.write_bytes(b"original")
    _set_attrs(target, _attrs(target) | READONLY | HIDDEN)
    try:
        with pytest.raises(TargetNotWritableError):
            secure_atomic_write(target, _rewrite)
        assert target.read_bytes() == b"original"
    finally:
        _set_attrs(target, ARCHIVE)


# ──────────────────────── hidden real formats through the API ────────────────

def _jpeg(path: Path) -> tuple[str, str]:
    import piexif
    from PIL import Image
    Image.new("RGB", (4, 4), (10, 20, 30)).save(
        path, "JPEG", exif=piexif.dump({"0th": {piexif.ImageIFD.Make: b"Old"}}))
    return "exif:Make", "New"


def _pdf(path: Path) -> tuple[str, str]:
    from pypdf import PdfWriter
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    writer.add_metadata({"/Title": "Old"})
    with path.open("wb") as stream:
        writer.write(stream)
    return "pdf:Title", "New"


def _docx(path: Path) -> tuple[str, str]:
    from docx import Document
    doc = Document()
    doc.core_properties.title = "Old"
    doc.save(str(path))
    return "office:title", "New"


@windows_only
@pytest.mark.asyncio
@pytest.mark.parametrize("ext,build", [("jpg", _jpeg), ("pdf", _pdf), ("docx", _docx)])
async def test_api_writes_hidden_real_formats(client, tmp_path, ext, build):
    target = tmp_path / ("hidden." + ext)
    key, value = build(target)
    _set_attrs(target, _attrs(target) | HIDDEN)
    resp = await client.post("/write", json={"path": str(target), "fields": [{"key": key, "value": value}]})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # F3B: the success body also carries the Undo token of the change, why an
    # Undo is missing (null here) and post-commit warnings (none here).
    assert set(body) == {"ok", "errors", "undo_token", "undo_unavailable", "warnings"}
    assert body["ok"] is True and body["errors"] == []
    assert isinstance(body["undo_token"], str) and body["undo_token"]
    assert body["undo_unavailable"] is None and body["warnings"] == []
    assert _attrs(target) & HIDDEN
    fields = {f["key"]: f for f in (await client.get("/read", params={"path": str(target)})).json()["fields"]}
    assert fields[key]["value"] == value
    assert "HIDDEN" in fields["fs:win_attrs"]["value"]
    _no_leftovers(target)
    # The Undo transaction keeps the attribute too.
    resp = await client.post("/undo", json={"token": body["undo_token"]})
    assert resp.status_code == 200, resp.text
    assert resp.json()["keys"] == [key]
    assert _attrs(target) & HIDDEN
    _no_leftovers(target)


@windows_only
@pytest.mark.asyncio
async def test_api_read_only_attribute_is_403(client, tmp_path):
    target = tmp_path / "file.pdf"
    key, value = _pdf(target)
    before = target.read_bytes()
    _set_attrs(target, _attrs(target) | READONLY)
    try:
        resp = await client.post("/write", json={"path": str(target), "fields": [{"key": key, "value": value}]})
        assert resp.status_code == 403, resp.text
        body = resp.json()
        assert body["ok"] is False and body["errors"][0]["code"] == "not_writable"
        assert isinstance(body["detail"], str)
        assert target.read_bytes() == before
    finally:
        _set_attrs(target, ARCHIVE)


# ──────────────────────── ARCHIVE attribute after a write ────────────────────

NORMAL, NOT_CONTENT_INDEXED = 0x80, 0x2000


@windows_only
@pytest.mark.parametrize("initial", [NORMAL, HIDDEN, HIDDEN | SYSTEM | NOT_CONTENT_INDEXED, ARCHIVE])
def test_modified_file_gets_archive_attribute(tmp_path, initial):
    """Like any Windows write, a modified file is marked ARCHIVE ("changed since the
    last backup"); every other attribute is kept."""
    target = tmp_path / "file.bin"
    target.write_bytes(b"original")
    _set_attrs(target, initial)
    assert _attrs(target) == initial
    secure_atomic_write(target, _rewrite)
    assert _attrs(target) == (initial & ~NORMAL) | ARCHIVE
    _no_leftovers(target)


@windows_only
@pytest.mark.asyncio
async def test_api_write_sets_archive_attribute(client, tmp_path):
    target = tmp_path / "file.pdf"
    key, value = _pdf(target)
    _set_attrs(target, NORMAL)
    resp = await client.post("/write", json={"path": str(target), "fields": [{"key": key, "value": value}]})
    assert resp.status_code == 200, resp.text
    assert _attrs(target) == ARCHIVE
    fields = {f["key"]: f for f in (await client.get("/read", params={"path": str(target)})).json()["fields"]}
    assert fields["fs:win_attrs"]["value"] == ["ARCHIVE"]


# ──────────────────────── DACL preservation ──────────────────────────────────

def _sddl(path: Path) -> str:
    import win32security
    info = win32security.DACL_SECURITY_INFORMATION
    sd = win32security.GetFileSecurity(str(path), info)
    return win32security.ConvertSecurityDescriptorToStringSecurityDescriptor(
        sd, win32security.SDDL_REVISION_1, info)


def _sid(text: str):
    import win32security
    return win32security.ConvertStringSidToSid(text)


USERS, GUESTS, REMOTE_DESKTOP_USERS = "S-1-5-32-545", "S-1-5-32-546", "S-1-5-32-555"


def _add_ace(path: Path, sid: str, mask: int, flags: int = 0) -> None:
    """Add one explicit allow ACE, keeping inheritance (like ``icacls /grant``)."""
    import win32security
    dacl = win32security.GetFileSecurity(
        str(path), win32security.DACL_SECURITY_INFORMATION).GetSecurityDescriptorDacl()
    dacl.AddAccessAllowedAceEx(win32security.ACL_REVISION_DS, flags, mask, _sid(sid))
    win32security.SetNamedSecurityInfo(
        str(path), win32security.SE_FILE_OBJECT,
        win32security.DACL_SECURITY_INFORMATION | win32security.UNPROTECTED_DACL_SECURITY_INFORMATION,
        None, None, dacl, None)


def _shared_folder(tmp_path: Path, name: str = "shared", sid: str = USERS) -> Path:
    """A folder granting an inheritable read ACE, like a shared photo folder."""
    import ntsecuritycon
    import win32security
    folder = tmp_path / name
    folder.mkdir()
    _add_ace(folder, sid, ntsecuritycon.FILE_GENERIC_READ | ntsecuritycon.FILE_GENERIC_EXECUTE,
             win32security.OBJECT_INHERIT_ACE | win32security.CONTAINER_INHERIT_ACE)
    return folder


def _dacl_case(folder_root: Path, case: str, build=lambda p: p.write_bytes(b"original"),
               name: str = "file.txt") -> Path:
    """File whose DACL is inherited / inherited + explicit / protected / stale-inherited."""
    import ntsecuritycon
    import win32security
    folder = _shared_folder(folder_root)
    target = folder / name
    if case == "moved":
        # Inherited ACEs of ANOTHER folder: a move on the same volume keeps them.
        source = _shared_folder(folder_root, "elsewhere", GUESTS)
        build(source / name)
        os.rename(source / name, target)
    else:
        build(target)
    if case == "explicit":
        _add_ace(target, REMOTE_DESKTOP_USERS, ntsecuritycon.FILE_GENERIC_READ | ntsecuritycon.FILE_GENERIC_WRITE)
    elif case == "protected":
        owner = win32security.GetFileSecurity(
            str(target), win32security.OWNER_SECURITY_INFORMATION).GetSecurityDescriptorOwner()
        acl = win32security.ACL()
        acl.AddAccessAllowedAce(win32security.ACL_REVISION, ntsecuritycon.FILE_ALL_ACCESS, owner)
        acl.AddAccessAllowedAce(win32security.ACL_REVISION, ntsecuritycon.FILE_GENERIC_READ, _sid(USERS))
        win32security.SetNamedSecurityInfo(
            str(target), win32security.SE_FILE_OBJECT,
            win32security.DACL_SECURITY_INFORMATION | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
            None, None, acl, None)
    before = _sddl(target)
    expected_marker = {"inherited": "(A;ID;0x1200a9;;;BU)", "explicit": ";;;RD)",
                       "protected": "D:PAI(", "moved": "(A;ID;0x1200a9;;;BG)"}[case]
    assert expected_marker in before, before
    return target


DACL_CASES = ["inherited", "explicit", "protected", "moved"]


@windows_only
@pytest.mark.parametrize("case", DACL_CASES)
def test_write_keeps_the_exact_dacl(tmp_path, case):
    """ReplaceFileW keeps only the explicit ACEs and recomputes the inherited ones
    from the private directory: Users (and anyone the folder grants access to) used
    to lose access to every modified file."""
    target = _dacl_case(tmp_path, case)
    before = _sddl(target)
    warnings = secure_atomic_write(target, _rewrite)
    assert target.read_bytes() == b"rewritten by path"
    assert _sddl(target) == before
    assert warnings == []
    _no_leftovers(target)


@windows_only
@pytest.mark.asyncio
@pytest.mark.parametrize("case", DACL_CASES)
async def test_api_write_and_undo_keep_the_exact_dacl(client, tmp_path, case):
    target = _dacl_case(tmp_path, case, build=_pdf, name="file.pdf")
    before = _sddl(target)
    resp = await client.post("/write", json={"path": str(target),
                                             "fields": [{"key": "pdf:Title", "value": "New"}]})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert _sddl(target) == before and body["warnings"] == []
    resp = await client.post("/undo", json={"token": body["undo_token"]})
    assert resp.status_code == 200, resp.text
    assert _sddl(target) == before and resp.json()["warnings"] == []
    _no_leftovers(target)


@windows_only
def test_dacl_restore_failure_is_a_warning_not_an_error(tmp_path, monkeypatch):
    """The content is already committed when the permissions are restored: a failure
    is reported in the returned warnings, never as a failed write."""
    target = _dacl_case(tmp_path, "inherited")

    def denied(path, sec):
        raise PermissionError(13, "simulated: access denied")
    monkeypatch.setattr(security, "_set_windows_security", denied)
    warnings = secure_atomic_write(target, _rewrite)
    assert target.read_bytes() == b"rewritten by path"
    assert len(warnings) == 1 and "permissions" in warnings[0] and "saved" in warnings[0]
    _no_leftovers(target)


# ──────────────────────── post-commit failures are warnings (16b) ────────────

def _dropping_replace(monkeypatch):
    """ReplaceFileW stand-in that loses HIDDEN on the target (forces the post-commit step)."""
    real = security._windows_replace

    def replace(original, replacement, backup):
        real(original, replacement, backup)
        _set_attrs(original, ARCHIVE)
    monkeypatch.setattr(security, "_windows_replace", replace)


def _failing_attributes_on(monkeypatch, target: Path):
    real = security._set_windows_attributes

    def set_attributes(path, value):
        if Path(path) == target:
            raise PermissionError(13, "simulated: attributes locked")
        real(path, value)
    monkeypatch.setattr(security, "_set_windows_attributes", set_attributes)


@windows_only
def test_attribute_restore_failure_after_replace_is_a_warning(tmp_path, monkeypatch):
    target = tmp_path / "file.txt"
    target.write_bytes(b"original")
    _set_attrs(target, HIDDEN)
    _dropping_replace(monkeypatch)
    _failing_attributes_on(monkeypatch, target)
    warnings = secure_atomic_write(target, _rewrite)
    assert target.read_bytes() == b"rewritten by path"   # the write happened
    assert len(warnings) == 1 and "attributes" in warnings[0] and "HIDDEN" in warnings[0]
    _no_leftovers(target)


@windows_only
@pytest.mark.asyncio
async def test_api_reports_post_commit_failure_as_success_with_warning(client, tmp_path):
    """Used to be a 409 although the file had been modified (and without Undo)."""
    target = tmp_path / "file.pdf"
    key, value = _pdf(target)
    _set_attrs(target, _attrs(target) | HIDDEN)
    with pytest.MonkeyPatch.context() as patch:
        _dropping_replace(patch)
        _failing_attributes_on(patch, target)
        resp = await client.post("/write", json={"path": str(target), "fields": [{"key": key, "value": value}]})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True and isinstance(body["undo_token"], str) and body["undo_unavailable"] is None
    assert len(body["warnings"]) == 1 and "HIDDEN" in body["warnings"][0]
    fields = {f["key"]: f for f in (await client.get("/read", params={"path": str(target)})).json()["fields"]}
    assert fields[key]["value"] == value
    _set_attrs(target, _attrs(target) | HIDDEN)
    resp = await client.post("/undo", json={"token": body["undo_token"]})
    assert resp.status_code == 200, resp.text
    assert resp.json()["warnings"] == []
    assert _attrs(target) & HIDDEN
    _no_leftovers(target)


@windows_only
def test_cleanup_failure_after_replace_is_a_warning(tmp_path, monkeypatch):
    target = tmp_path / "file.txt"
    target.write_bytes(b"original")
    real = security._remove_tree
    calls = []

    def stuck(directory):
        calls.append(directory)
        raise PermissionError(13, "simulated: backup still open")
    monkeypatch.setattr(security, "_remove_tree", stuck)
    warnings = secure_atomic_write(target, _rewrite)
    assert target.read_bytes() == b"rewritten by path"
    assert calls and len(warnings) == 1 and "temporary folder" in warnings[0]
    real(calls[0])
    _no_leftovers(target)


# ──────────────────────── transient locks: retry, then 503 (10b) ─────────────

GENERIC_READ, OPEN_EXISTING = 0x80000000, 3


class _ExclusiveHandle:
    """Open ``path`` with share mode 0 (no other open succeeds), like an editor lock."""

    def __init__(self, path: Path) -> None:
        import ctypes
        import ctypes.wintypes as wintypes
        self._k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                          wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        self._k32.CreateFileW.restype = wintypes.HANDLE
        self._k32.CloseHandle.argtypes = [wintypes.HANDLE]
        self.handle = self._k32.CreateFileW(str(path), GENERIC_READ, 0, None, OPEN_EXISTING, 0, None)
        assert self.handle not in (None, wintypes.HANDLE(-1).value), ctypes.get_last_error()

    def close(self) -> None:
        if self.handle:
            self._k32.CloseHandle(self.handle)
            self.handle = None


@windows_only
def test_replace_waits_for_a_reader_to_release_the_file(tmp_path):
    """A plain ``open(..., "rb")`` (no FILE_SHARE_DELETE: a running /hash, another
    program) made ReplaceFileW fail at once with WinError 32 -> 403."""
    import threading
    import time
    target = tmp_path / "file.txt"
    target.write_bytes(b"original")
    reader = target.open("rb")
    threading.Timer(0.3, reader.close).start()
    start = time.monotonic()
    try:
        assert secure_atomic_write(target, _rewrite) == []
    finally:
        reader.close()
    assert time.monotonic() - start >= 0.25
    assert target.read_bytes() == b"rewritten by path"
    _no_leftovers(target)


@windows_only
def test_persistent_lock_is_file_busy_and_changes_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(security, "TRANSIENT_RETRY_SECONDS", 0.2)
    target = tmp_path / "file.txt"
    target.write_bytes(b"original")
    with target.open("rb"):                  # blocks the replace
        with pytest.raises(security.FileBusyError):
            secure_atomic_write(target, _rewrite)
    assert target.read_bytes() == b"original"
    _no_leftovers(target)
    lock = _ExclusiveHandle(target)          # blocks even the copy
    try:
        with pytest.raises(security.FileBusyError):
            secure_atomic_write(target, _rewrite)
    finally:
        lock.close()
    assert target.read_bytes() == b"original"
    _no_leftovers(target)


@windows_only
def test_transient_access_denied_is_retried_then_reported_as_permission(tmp_path, monkeypatch):
    import ctypes
    monkeypatch.setattr(security, "TRANSIENT_RETRY_SECONDS", 0.2)
    target = tmp_path / "file.txt"
    target.write_bytes(b"original")
    real = security._windows_replace
    failures = [5, 5]

    def flaky(original, replacement, backup):
        if failures:
            raise ctypes.WinError(failures.pop())
        real(original, replacement, backup)
    monkeypatch.setattr(security, "_windows_replace", flaky)
    secure_atomic_write(target, _rewrite)
    assert target.read_bytes() == b"rewritten by path" and failures == []

    def always_denied(original, replacement, backup):
        raise ctypes.WinError(5)
    monkeypatch.setattr(security, "_windows_replace", always_denied)
    with pytest.raises(PermissionError):
        secure_atomic_write(target, lambda tmp: tmp.write_bytes(b"third"))
    assert target.read_bytes() == b"rewritten by path"
    _no_leftovers(target)


@windows_only
def test_file_changed_while_waiting_for_the_lock_is_409(tmp_path, monkeypatch):
    import ctypes
    target = tmp_path / "file.txt"
    target.write_bytes(b"original")

    def busy_then_changed(original, replacement, backup):
        with target.open("r+b") as stream:   # the lock holder writes the file
            stream.write(b"EXTERNAL")
        raise ctypes.WinError(32)
    monkeypatch.setattr(security, "_windows_replace", busy_then_changed)
    with pytest.raises(ConcurrentModificationError):
        secure_atomic_write(target, _rewrite)
    assert target.read_bytes() == b"EXTERNAL"
    _no_leftovers(target)


@windows_only
@pytest.mark.asyncio
async def test_api_write_on_locked_file_is_503_file_busy(client, tmp_path, monkeypatch):
    monkeypatch.setattr(security, "TRANSIENT_RETRY_SECONDS", 0.2)
    target = tmp_path / "file.pdf"
    key, value = _pdf(target)
    before = target.read_bytes()
    lock = _ExclusiveHandle(target)
    try:
        resp = await client.post("/write", json={"path": str(target), "fields": [{"key": key, "value": value}]})
    finally:
        lock.close()
    assert resp.status_code == 503, resp.text
    body = resp.json()
    assert body["ok"] is False and [e["code"] for e in body["errors"]] == ["file_busy"]
    assert isinstance(body["detail"], str) and body["detail"]
    assert resp.headers.get("retry-after")
    assert target.read_bytes() == before
    _no_leftovers(target)
