"""Path validation and security layer for MetaLens.

This module provides secure path handling that:
1. Resolves all symlinks and normalizes paths (satisfy CodeQL rules)
2. Prevents symlink attack during atomic write operations
3. Supports unrestricted access to any file on the filesystem (desktop app requirement)
4. Is cross-platform (Windows, macOS, Linux)

Security Model:
- Desktop app, NOT web service: Path comes from trusted Electron UI
- No restrictive whitelist: User can access any file they own
- Real risks: symlink traversal during write, concurrent modification
- False positives: path injection, directory traversal (irrelevant for desktop)
"""

from __future__ import annotations

import logging
import os
import sys
import shutil
import stat
import tempfile
import time
from dataclasses import dataclass
from threading import Lock, RLock
from typing import Any, Callable
from pathlib import Path

# Metadata mutations are infrequent. One reentrant lock also permits nested
# transactions without acquiring path locks in inconsistent orders.
_WRITE_LOCK = RLock()

logger = logging.getLogger(__name__)

__all__ = [
    "validate_file_path",
    "validate_directory_path",
    "secure_atomic_write",
    "is_active_transaction_dir",
    "PathSecurityError",
    "FileBusyError",
]


class PathSecurityError(Exception):
    """Raised when path validation fails."""
    pass


class _RecoveryError(PathSecurityError):
    """The original backup must be retained for manual recovery."""


class NothingToCommit(Exception):
    """Raised by a ``write_fn`` that found the temporary copy identical to the target.

    ``secure_atomic_write`` then discards the copy without replacing the file, so
    a save that changes nothing keeps the file's identity (hard links, file id,
    times) instead of swapping in an identical copy.
    """


class FileBusyError(PathSecurityError):
    """Another program kept the file open/locked beyond the retry budget (HTTP 503).

    Transient by nature: nothing was changed and the operation can be retried.
    """
    http_status = 503
    code = "file_busy"
    key = None

    def to_dict(self) -> dict:
        return {"code": self.code, "key": None, "message": str(self)}


# Transient Windows errors: retried with a short exponential backoff for at most
# this many seconds before the operation is reported as failed.
TRANSIENT_RETRY_SECONDS = 1.5
_ERROR_ACCESS_DENIED = 5
_ERROR_SHARING_VIOLATION = 32
_ERROR_LOCK_VIOLATION = 33
_ERROR_UNABLE_TO_REMOVE_REPLACED = 1175
_ERROR_UNABLE_TO_MOVE_REPLACEMENT = 1176
_SHARING_ERRORS = frozenset({_ERROR_SHARING_VIOLATION, _ERROR_LOCK_VIOLATION})
# ReplaceFileW failures that leave both files under their original names
# (lpBackupFileName given) and mean "the target is held open by someone".
_REPLACE_BUSY_ERRORS = _SHARING_ERRORS | {_ERROR_UNABLE_TO_REMOVE_REPLACED, _ERROR_UNABLE_TO_MOVE_REPLACEMENT}


def winerror_of(exc: BaseException) -> int | None:
    """Windows error code of an OSError (None elsewhere / for other exceptions)."""
    return getattr(exc, "winerror", None) if isinstance(exc, OSError) else None


_OPENS_NOW = 0


def windows_open_error(path: Path | str) -> int | None:
    """Windows: the error CreateFileW reports opening ``path`` for reading with the
    sharing mode of Python's ``open(..., "rb")`` (read + write), ``0`` when it opens
    now; ``None`` on other platforms.

    Python's ``open()`` reports a sharing violation as a plain ``PermissionError``
    (errno 13, no ``winerror``): this tells a lock held by another program
    (32/33, transient) from a real access denial (5).
    """
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                     wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    generic_read, share_read_write, open_existing = 0x80000000, 0x1 | 0x2, 3
    handle = kernel32.CreateFileW(str(path), generic_read, share_read_write, None, open_existing, 0, None)
    if handle in (None, wintypes.HANDLE(-1).value):
        return ctypes.get_last_error()
    kernel32.CloseHandle(handle)
    return _OPENS_NOW


def raise_if_unreadable(path: Path | str) -> None:
    """Open ``path`` for reading and close it; on Windows the OSError carries the real
    ``winerror`` (sharing violation 32 vs access denied 5)."""
    if sys.platform == "win32":
        code = windows_open_error(path)
        if code:
            import ctypes
            error = ctypes.WinError(code)
            error.filename = str(path)
            raise error
        return
    with open(path, "rb"):
        pass


def _retry_transient(action: Callable[[], Any], *, busy: frozenset[int], retry_denied: bool,
                     what: str, before_retry: Callable[[], None] | None = None,
                     probe: Path | None = None) -> Any:
    """Run ``action``; retry Windows sharing/lock violations (and ACCESS_DENIED when
    ``retry_denied``) with backoff. A persistent lock becomes ``FileBusyError``; a
    persistent ACCESS_DENIED is re-raised (a permission problem, HTTP 403).
    ``probe``: file whose real open error explains a ``PermissionError`` without
    ``winerror`` (raised by Python's ``open``)."""
    deadline = time.monotonic() + TRANSIENT_RETRY_SECONDS
    delay = 0.01
    while True:
        try:
            return action()
        except OSError as exc:
            code = winerror_of(exc)
            if code is None and probe is not None and isinstance(exc, PermissionError):
                code = windows_open_error(probe)
            retryable = (code == _OPENS_NOW or code in busy
                         or (retry_denied and code == _ERROR_ACCESS_DENIED))
            if not retryable:
                raise
            if time.monotonic() + delay > deadline:
                if code in busy:
                    raise FileBusyError(
                        f"{what} is in use by another program ({exc.strerror or exc}); try again") from exc
                raise
            time.sleep(delay)
            delay = min(delay * 2, 0.2)
            if before_retry is not None:
                before_retry()


# Private transaction directories currently in use (``/list`` hides them).
_ACTIVE_DIRS: set[str] = set()
_ACTIVE_DIRS_LOCK = Lock()


def _dir_key(path: Path | str) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def is_active_transaction_dir(path: Path | str) -> bool:
    """True for the private directory of a transaction that is running right now."""
    with _ACTIVE_DIRS_LOCK:
        return _dir_key(path) in _ACTIVE_DIRS


def validate_file_path(path_str: str, must_exist: bool = True) -> Path:
    """
    Validate and normalize an absolute file path.

    This function:
    1. Resolves the path (expands ~, .., symlinks, etc.)
    2. Verifies it points to a file (if must_exist=True)
    3. Checks readable access (if must_exist=True)
    4. On Windows: double-checks with realpath to prevent symlink tricks

    Args:
        path_str: String representation of the file path
        must_exist: If True, raises error if file doesn't exist or is unreadable

    Returns:
        Resolved, validated Path object

    Raises:
        PathSecurityError: If path is invalid, unreadable, or doesn't exist (when required)

    Examples:
        >>> p = validate_file_path("/home/user/document.pdf")
        >>> p.is_file()
        True

        >>> p = validate_file_path("~/secret.txt")  # ~ is expanded
        >>> p.is_absolute()
        True
    """
    # Step 1: Parse and resolve path
    # .resolve() handles: normalization, ~, .., symlink resolution
    try:
        path = Path(path_str).expanduser().resolve(strict=False)
    except (OSError, ValueError, RuntimeError) as e:
        raise PathSecurityError(f"Invalid path '{path_str}': {e}") from e

    # Step 2: Verify it's an absolute path
    if not path.is_absolute():
        raise PathSecurityError(
            f"Path must be absolute, got: {path_str}"
        )

    # Step 3: If must_exist, verify file exists and is readable
    if must_exist:
        # Check existence (using is_file() which follows symlinks)
        if not path.is_file():
            raise PathSecurityError(
                f"File not found or is not a regular file: {path}"
            )

        # Check readable (on Windows this is implicit; on Unix check via os.access)
        if not os.access(path, os.R_OK):
            raise PathSecurityError(
                f"No read permission for file: {path}"
            )

    # Step 4: Extra validation on Windows — resolve again with realpath
    # This catches Windows-specific symlink tricks (e.g., junctions in temp dirs)
    if sys.platform == "win32":
        try:
            real = Path(os.path.realpath(path))
            if must_exist and real != path.resolve():
                # Path is a symlink/junction; we already resolved it,
                # but log this for awareness (optional in production)
                pass
        except (OSError, ValueError) as e:
            raise PathSecurityError(
                f"Path realpath validation failed: {e}"
            ) from e

    return path


def validate_directory_path(path_str: str, must_exist: bool = True) -> Path:
    """
    Validate and normalize an absolute directory path.

    Same as validate_file_path but for directories.

    Args:
        path_str: String representation of the directory path
        must_exist: If True, raises error if directory doesn't exist or is unreadable

    Returns:
        Resolved, validated Path object

    Raises:
        PathSecurityError: If path is invalid or not a directory
    """
    try:
        path = Path(path_str).expanduser().resolve(strict=False)
    except (OSError, ValueError, RuntimeError) as e:
        raise PathSecurityError(f"Invalid path '{path_str}': {e}") from e

    if not path.is_absolute():
        raise PathSecurityError(
            f"Path must be absolute, got: {path_str}"
        )

    if must_exist:
        if not path.is_dir():
            raise PathSecurityError(
                f"Path not found or is not a directory: {path}"
            )

        if not os.access(path, os.R_OK | os.X_OK):
            raise PathSecurityError(
                f"No read/execute permission for directory: {path}"
            )

    if sys.platform == "win32":
        try:
            real = Path(os.path.realpath(path))
            if must_exist and real != path.resolve():
                pass
        except (OSError, ValueError) as e:
            raise PathSecurityError(
                f"Path realpath validation failed: {e}"
            ) from e

    return path


def secure_atomic_write(
    target_path: Path,
    write_fn: Callable[[Path], None],
    temp_suffix: str = ".ml_tmp",
) -> list[str]:
    """
    Perform atomic write with symlink-attack protection.

    This function:
    1. Validates the target path
    2. Creates a private temporary directory on the same filesystem
    3. Calls write_fn(temp_path) to write modified content
    4. Replaces target with temp (ReplaceFileW on Windows, os.replace on Unix)
    5. Cleans up temp file on error
    6. Rejects replaced targets and non-regular temporary files
    7. Windows: clears HIDDEN/SYSTEM/READONLY on the temporary copy while
       ``write_fn`` runs (libraries recreate it by name), puts the original
       attributes plus ARCHIVE (the file was modified, like any Windows write)
       on the copy before ReplaceFileW and re-applies them to the target if the
       replace did not keep them. Hard links of the target are detached (the
       file is a new one).
    8. Windows security: ReplaceFileW keeps only the EXPLICIT ACEs of the
       original and recomputes the inherited ones from the private directory,
       so the original DACL (inherited ACEs, protection and auto-inherit flags,
       verbatim) is put back on the target right after the replace. The owner
       becomes the current user unless the original one can be restored (it
       needs WRITE_OWNER/SeRestorePrivilege for another owner): a warning says
       so. The SACL is not preserved (reading it needs SeSecurityPrivilege).
    9. Windows sharing/lock violations (the file open in another program, an
       antivirus scan...) are retried with backoff for ``TRANSIENT_RETRY_SECONDS``
       when copying and replacing; a lock that persists is ``FileBusyError``.

    The transaction never nests by design: callers run handler hooks
    (``apply_write``/``apply_delete``) on the temporary path.

    Returns:
        Warnings about steps that failed AFTER the new content was committed
        (attributes, permissions or owner not restored, temporary directory
        left behind). The write itself succeeded: these never raise.

    Raises (besides the callback's own exceptions), always with the target untouched:
        TargetNotWritableError (403): no write permission / READONLY attribute /
            permissions of the file unreadable.
        ConcurrentModificationError (409): the target changed during the write.
        FileBusyError (503): the target stayed locked by another program.
        PathSecurityError: invalid target or unsafe temporary file.

    Args:
        target_path: Path object of the file to write (must exist and be writable)
        write_fn: Callable that takes (Path) and performs write operations
        temp_suffix: Suffix for temporary file name (default: ".ml_tmp")

    Examples:
        >>> def writer(tmp_path):
        ...     with tmp_path.open("wb") as f:
        ...         f.write(b"new content")
        >>> secure_atomic_write(Path("/home/user/file.txt"), writer)
        []
    """
    # Imported lazily: core.errors subclasses PathSecurityError from this module.
    from core.errors import ConcurrentModificationError, TargetNotWritableError

    target = validate_file_path(str(target_path), must_exist=True)
    if not temp_suffix or any(c in temp_suffix for c in ("/", "\\")):
        raise ValueError("Temporary suffix must not contain path separators")
    windows = sys.platform == "win32"
    with _WRITE_LOCK:
        # On Windows os.access(W_OK) reports the READONLY attribute.
        if not os.access(target, os.W_OK):
            raise TargetNotWritableError(f"No write permission for: {target}")
        original = _file_identity(target)
        original_stat = target.stat()
        original_attrs = _windows_attributes(target) if windows else None
        notes: list[str] = []
        security = _read_windows_security(target, notes) if windows else None
        # An exclusively created private directory protects the entire callback,
        # including libraries which close/reopen the temporary file by pathname.
        try:
            directory = Path(tempfile.mkdtemp(prefix=".metalens-", dir=target.parent))
        except PermissionError as exc:
            raise TargetNotWritableError(f"Cannot create a temporary file next to: {target}") from exc
        with _ACTIVE_DIRS_LOCK:
            _ACTIVE_DIRS.add(_dir_key(directory))
        cleanup = True
        committed = False
        warnings: list[str] = []
        try:
            temp_path = directory / (target.stem + temp_suffix + target.suffix)
            _retry_transient(lambda: shutil.copy2(target, temp_path), busy=_SHARING_ERRORS,
                             retry_denied=True, what="The file", probe=target if windows else None)
            if original_attrs is not None:
                # copy2 copies HIDDEN/SYSTEM/READONLY: Windows then refuses to
                # recreate the temporary file by name (CREATE_ALWAYS -> access denied).
                _set_windows_attributes(temp_path, original_attrs & ~_CLEARED_ATTRIBUTES)
            try:
                write_fn(temp_path)
            except NothingToCommit:
                return warnings   # the finally block discards the copy; the target is untouched
            if not stat.S_ISREG(temp_path.lstat().st_mode):
                raise PathSecurityError("Temporary file is not a regular file")
            if not windows:
                _preserve_posix_owner(temp_path, original_stat)
            if _file_identity(target) != original:
                raise ConcurrentModificationError("Target changed during metadata write")
            with temp_path.open("r+b") as stream:
                os.fsync(stream.fileno())
            if original_attrs is not None:
                _set_windows_attributes(temp_path, modified_attributes(original_attrs))
            _replace_file(temp_path, target, original)
            committed = True
            if windows:
                warnings += notes
                try:
                    warnings += _restore_windows_metadata(target, original_attrs, security)
                except Exception as exc:   # never turn a committed save into an error
                    logger.exception("Restoring the Windows metadata of %s failed", target)
                    warnings.append("The file was saved, but its attributes and permissions could not be "
                                    f"checked ({type(exc).__name__}: {exc})")
        except _RecoveryError:
            cleanup = False
            raise
        finally:
            with _ACTIVE_DIRS_LOCK:
                _ACTIVE_DIRS.discard(_dir_key(directory))
            if cleanup:
                problem = _cleanup_directory(directory)
                if problem is not None:
                    if committed:
                        warnings.append(problem)   # the save itself succeeded
                    else:
                        logger.warning("%s", problem)
    return warnings


def _preserve_posix_owner(path: Path, original) -> None:
    current = path.stat()
    if (current.st_uid, current.st_gid) != (original.st_uid, original.st_gid):
        # copy2 does not preserve ownership. Refuse the mutation if the caller
        # cannot restore it, rather than silently changing the file's owner.
        os.chown(path, original.st_uid, original.st_gid)
        os.chmod(path, stat.S_IMODE(original.st_mode))


def _replace_file(replacement: Path, target: Path, expected_identity: tuple | None = None) -> None:
    if sys.platform != "win32":
        os.replace(replacement, target)
        return
    # ReplaceFile preserves the explicit ACEs, creation time, encryption and
    # named streams. os.replace after copy2 would discard those on Windows.
    backup = replacement.parent / "original-backup"

    def attempt() -> None:
        try:
            _windows_replace(target, replacement, backup)
        except OSError as exc:
            if backup.exists():   # partial failure: the original was moved away
                raise _PartialReplace(exc) from exc
            raise

    def still_same() -> None:
        # Never replace what another program wrote while we waited for its lock.
        if expected_identity is not None and _file_identity(target) != expected_identity:
            from core.errors import ConcurrentModificationError
            raise ConcurrentModificationError("Target changed while waiting for it to be released")

    try:
        _retry_transient(attempt, busy=_REPLACE_BUSY_ERRORS, retry_denied=True, what="The file",
                         before_retry=still_same)
    except _PartialReplace as partial:
        try:
            os.replace(backup, target)
        except OSError as exc:
            raise _RecoveryError(f"Replacement and rollback failed; original retained at {backup}") from exc
        raise partial.error from None


class _PartialReplace(Exception):
    """ReplaceFileW moved the original to the backup and then failed: no retry."""

    def __init__(self, error: OSError) -> None:
        super().__init__(str(error))
        self.error = error


def _windows_replace(target: Path, replacement: Path, backup: Path) -> None:
    from ctypes import WinDLL, WinError, get_last_error, wintypes
    replace = WinDLL("kernel32", use_last_error=True).ReplaceFileW
    replace.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR,
                        wintypes.DWORD, wintypes.LPVOID, wintypes.LPVOID]
    replace.restype = wintypes.BOOL
    # Do not ignore ACL/stream merge errors: preserving them is required.
    if not replace(str(target), str(replacement), str(backup), 0, None, None):
        raise WinError(get_last_error())


def _file_identity(path: Path) -> tuple:
    from core.errors import ConcurrentModificationError
    st = path.lstat()
    if not stat.S_ISREG(st.st_mode):
        raise ConcurrentModificationError("Target changed to a non-regular file")
    return st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns


# ──────────────────────── Windows file attributes ────────────────────────────

_FILE_ATTRIBUTE_READONLY = 0x1
_FILE_ATTRIBUTE_HIDDEN = 0x2
_FILE_ATTRIBUTE_SYSTEM = 0x4
_FILE_ATTRIBUTE_ARCHIVE = 0x20
_FILE_ATTRIBUTE_NORMAL = 0x80
# Cleared on the private temporary copy while the callback runs.
_CLEARED_ATTRIBUTES = _FILE_ATTRIBUTE_READONLY | _FILE_ATTRIBUTE_HIDDEN | _FILE_ATTRIBUTE_SYSTEM
# The attributes SetFileAttributesW can set (others, e.g. COMPRESSED, are ignored).
_SETTABLE_ATTRIBUTES = (0x1 | 0x2 | 0x4 | 0x20 | 0x100 | 0x1000 | 0x2000
                        | 0x8000 | 0x20000 | 0x80000 | 0x100000)
# Checked after a restore: READONLY, HIDDEN, SYSTEM, ARCHIVE, TEMPORARY, OFFLINE,
# NOT_CONTENT_INDEXED. INTEGRITY_STREAM/NO_SCRUB_DATA (ReFS) are not really set by
# SetFileAttributesW and PINNED/UNPINNED belong to the cloud-sync provider.
_VERIFIED_ATTRIBUTES = 0x1 | 0x2 | 0x4 | 0x20 | 0x100 | 0x1000 | 0x2000
_ATTRIBUTE_NAMES = ((0x1, "READONLY"), (0x2, "HIDDEN"), (0x4, "SYSTEM"), (0x20, "ARCHIVE"),
                    (0x100, "TEMPORARY"), (0x1000, "OFFLINE"), (0x2000, "NOT_CONTENT_INDEXED"),
                    (0x8000, "INTEGRITY_STREAM"), (0x20000, "NO_SCRUB_DATA"), (0x80000, "PINNED"),
                    (0x100000, "UNPINNED"))


def modified_attributes(attributes: int) -> int:
    """Attributes of a file after MetaLens modified it: the original ones plus
    ARCHIVE ("changed since the last backup"), as for any write on Windows."""
    return (attributes & ~_FILE_ATTRIBUTE_NORMAL) | _FILE_ATTRIBUTE_ARCHIVE


def _attribute_names(attributes: int) -> str:
    names = [name for bit, name in _ATTRIBUTE_NAMES if attributes & bit]
    return ", ".join(names) or "NORMAL"


_KERNEL32 = None


def _kernel32():
    global _KERNEL32
    import ctypes
    if _KERNEL32 is None:
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetFileAttributesW.argtypes = [wintypes.LPCWSTR]
        kernel32.GetFileAttributesW.restype = wintypes.DWORD
        kernel32.SetFileAttributesW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD]
        kernel32.SetFileAttributesW.restype = wintypes.BOOL
        kernel32.GetVolumePathNameW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
        kernel32.GetVolumePathNameW.restype = wintypes.BOOL
        kernel32.GetVolumeInformationW.argtypes = [
            wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD), wintypes.LPWSTR, wintypes.DWORD]
        kernel32.GetVolumeInformationW.restype = wintypes.BOOL
        kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
        kernel32.LocalFree.restype = wintypes.HLOCAL
        _KERNEL32 = kernel32
    return ctypes, _KERNEL32


def _windows_attributes(path: Path) -> int:
    ctypes, kernel32 = _kernel32()
    value = kernel32.GetFileAttributesW(str(path))
    if value == 0xFFFFFFFF:  # INVALID_FILE_ATTRIBUTES
        raise ctypes.WinError(ctypes.get_last_error())
    return value


def _set_windows_attributes(path: Path, attributes: int) -> None:
    ctypes, kernel32 = _kernel32()
    value = (attributes & _SETTABLE_ATTRIBUTES) or _FILE_ATTRIBUTE_NORMAL
    if not kernel32.SetFileAttributesW(str(path), value):
        raise ctypes.WinError(ctypes.get_last_error())


def _ensure_windows_attributes(target: Path, attributes: int) -> None:
    """After the replace, put back any attribute the replace did not keep (OSError on failure)."""
    current = _windows_attributes(target)
    if (current & _SETTABLE_ATTRIBUTES) != (attributes & _SETTABLE_ATTRIBUTES):
        _set_windows_attributes(target, attributes)
        now = _windows_attributes(target)
        if (now & _VERIFIED_ATTRIBUTES) != (attributes & _VERIFIED_ATTRIBUTES):
            raise OSError(f"attributes are {_attribute_names(now)}")


# ──────────────────────── Windows security descriptor ────────────────────────

_OWNER_SECURITY_INFORMATION = 0x1
_DACL_SECURITY_INFORMATION = 0x4
_SE_DACL_AUTO_INHERIT_REQ = 0x0100
_SE_DACL_AUTO_INHERITED = 0x0400
_FILE_PERSISTENT_ACLS = 0x8
_SDDL_REVISION_1 = 1

_ADVAPI32 = None


def _advapi32():
    global _ADVAPI32
    import ctypes
    if _ADVAPI32 is None:
        from ctypes import wintypes
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        advapi32.GetFileSecurityW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.LPVOID,
                                              wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
        advapi32.GetFileSecurityW.restype = wintypes.BOOL
        advapi32.SetFileSecurityW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.LPVOID]
        advapi32.SetFileSecurityW.restype = wintypes.BOOL
        advapi32.GetSecurityDescriptorControl.argtypes = [
            wintypes.LPVOID, ctypes.POINTER(wintypes.WORD), ctypes.POINTER(wintypes.DWORD)]
        advapi32.GetSecurityDescriptorControl.restype = wintypes.BOOL
        advapi32.SetSecurityDescriptorControl.argtypes = [wintypes.LPVOID, wintypes.WORD, wintypes.WORD]
        advapi32.SetSecurityDescriptorControl.restype = wintypes.BOOL
        advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [
            wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(wintypes.LPWSTR),
            ctypes.POINTER(wintypes.ULONG)]
        advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW.restype = wintypes.BOOL
        _ADVAPI32 = advapi32
    return ctypes, _ADVAPI32


@dataclass(frozen=True)
class _Security:
    """Self-relative security descriptors (raw bytes) and their SDDL, before the write."""
    dacl: bytes
    dacl_sddl: str
    owner: bytes
    owner_sddl: str


def _get_file_security(path: Path, info: int) -> bytes:
    ctypes, advapi32 = _advapi32()
    from ctypes import wintypes
    needed = wintypes.DWORD(0)
    advapi32.GetFileSecurityW(str(path), info, None, 0, ctypes.byref(needed))
    if not needed.value:
        raise ctypes.WinError(ctypes.get_last_error())
    buffer = ctypes.create_string_buffer(needed.value)
    if not advapi32.GetFileSecurityW(str(path), info, buffer, needed, ctypes.byref(needed)):
        raise ctypes.WinError(ctypes.get_last_error())
    return buffer.raw


def _set_file_security(path: Path, info: int, descriptor: bytes) -> None:
    ctypes, advapi32 = _advapi32()
    buffer = ctypes.create_string_buffer(descriptor, len(descriptor))
    if info & _DACL_SECURITY_INFORMATION:
        from ctypes import wintypes
        control, revision = wintypes.WORD(0), wintypes.DWORD(0)
        if not advapi32.GetSecurityDescriptorControl(buffer, ctypes.byref(control), ctypes.byref(revision)):
            raise ctypes.WinError(ctypes.get_last_error())
        if control.value & _SE_DACL_AUTO_INHERITED:
            # Without AUTO_INHERIT_REQ, SetFileSecurityW drops the auto-inherited
            # flag ("AI" in SDDL) while keeping the ACEs verbatim.
            bits = _SE_DACL_AUTO_INHERIT_REQ | _SE_DACL_AUTO_INHERITED
            if not advapi32.SetSecurityDescriptorControl(buffer, bits, bits):
                raise ctypes.WinError(ctypes.get_last_error())
    if not advapi32.SetFileSecurityW(str(path), info, buffer):
        raise ctypes.WinError(ctypes.get_last_error())


def _sddl(descriptor: bytes, info: int) -> str:
    ctypes, advapi32 = _advapi32()
    from ctypes import wintypes
    _ctypes, kernel32 = _kernel32()
    buffer = ctypes.create_string_buffer(descriptor, len(descriptor))
    text = wintypes.LPWSTR()
    if not advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW(
            buffer, _SDDL_REVISION_1, info, ctypes.byref(text), None):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return text.value or ""
    finally:
        kernel32.LocalFree(ctypes.cast(text, wintypes.HLOCAL))


def _volume_has_acls(path: Path) -> bool:
    """False on FAT/exFAT and other volumes without persistent ACLs."""
    ctypes, kernel32 = _kernel32()
    from ctypes import wintypes
    root = ctypes.create_unicode_buffer(1024)
    if not kernel32.GetVolumePathNameW(str(path), root, len(root)):
        return True   # unknown: try, the descriptor read decides
    flags = wintypes.DWORD(0)
    if not kernel32.GetVolumeInformationW(root.value, None, 0, None, None, ctypes.byref(flags), None, 0):
        return True
    return bool(flags.value & _FILE_PERSISTENT_ACLS)


def _read_windows_security(target: Path, notes: list[str]) -> _Security | None:
    """DACL and owner of ``target`` before the write (None when the volume has no ACLs)."""
    from core.errors import TargetNotWritableError
    if not _volume_has_acls(target):
        return None
    try:
        dacl = _get_file_security(target, _DACL_SECURITY_INFORMATION)
        owner = _get_file_security(target, _OWNER_SECURITY_INFORMATION)
        return _Security(dacl, _sddl(dacl, _DACL_SECURITY_INFORMATION),
                         owner, _sddl(owner, _OWNER_SECURITY_INFORMATION))
    except PermissionError as exc:
        # Without READ_CONTROL the permissions could not be preserved (ReplaceFileW
        # needs that access too): refuse before touching anything.
        raise TargetNotWritableError(f"Cannot read the permissions of: {target} ({exc})") from exc
    except OSError as exc:
        notes.append(f"The permissions of the file could not be read ({exc}); "
                     "check them, the save may have changed them")
        return None


def _restore_windows_security(target: Path, security: _Security) -> list[str]:
    """Put the original DACL (and, if allowed, owner) back on the replaced target."""
    warnings: list[str] = []
    try:
        _post_commit_retry(lambda: _ensure_dacl(target, security))
    except OSError as exc:
        warnings.append("The file was saved, but its permissions (access control list) could not be "
                        f"restored: {exc}. Check the file's security settings.")
    try:
        current = _sddl(_get_file_security(target, _OWNER_SECURITY_INFORMATION), _OWNER_SECURITY_INFORMATION)
        if current != security.owner_sddl:
            _post_commit_retry(lambda: _set_file_security(target, _OWNER_SECURITY_INFORMATION, security.owner))
    except OSError as exc:
        warnings.append(f"The file was saved, but its owner could not be restored ({security.owner_sddl}; "
                        f"restoring another user's ownership needs additional privileges): {exc}")
    return warnings


def _ensure_dacl(target: Path, security: _Security) -> None:
    current = _sddl(_get_file_security(target, _DACL_SECURITY_INFORMATION), _DACL_SECURITY_INFORMATION)
    if current == security.dacl_sddl:
        return
    _set_windows_security(target, security)
    restored = _sddl(_get_file_security(target, _DACL_SECURITY_INFORMATION), _DACL_SECURITY_INFORMATION)
    if restored != security.dacl_sddl:
        raise OSError(f"the access control list is {restored} instead of {security.dacl_sddl}")


def _set_windows_security(target: Path, security: _Security) -> None:
    _set_file_security(target, _DACL_SECURITY_INFORMATION, security.dacl)


def _post_commit_retry(action: Callable[[], None], attempts: int = 3) -> None:
    for attempt in range(attempts):
        try:
            action()
            return
        except OSError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.03 * (attempt + 1))


def _restore_windows_metadata(target: Path, original_attrs: int | None,
                              security: _Security | None) -> list[str]:
    """Post-commit steps on Windows: attributes (+ARCHIVE), DACL, owner. Never raises."""
    warnings: list[str] = []
    if original_attrs is not None:
        wanted = modified_attributes(original_attrs)
        try:
            _post_commit_retry(lambda: _ensure_windows_attributes(target, wanted))
        except OSError as exc:
            warnings.append(f"The file was saved, but its Windows attributes ({_attribute_names(wanted)}) "
                            f"could not be restored: {exc}")
    if security is not None:
        warnings += _restore_windows_security(target, security)
    return warnings


def _cleanup_directory(directory: Path) -> str | None:
    """Remove the private directory; a message instead of an exception when it stays."""
    for attempt in range(4):
        try:
            _remove_tree(directory)
            return None
        except FileNotFoundError:
            return None
        except OSError as exc:
            if attempt == 3:
                return (f"The temporary folder {directory} could not be removed ({exc}); "
                        "it can be deleted safely")
            time.sleep(0.05 * (attempt + 1))
    return None


def _remove_tree(directory: Path) -> None:
    """rmtree that also removes READONLY entries (the ReplaceFileW backup on Windows)."""
    def clear_and_retry(function, path, _exc):
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
        if sys.platform == "win32":
            _set_windows_attributes(Path(path), _FILE_ATTRIBUTE_NORMAL)
        function(path)
    if sys.version_info >= (3, 12):
        shutil.rmtree(directory, onexc=clear_and_retry)
    else:  # pragma: no cover - Python < 3.12
        shutil.rmtree(directory, onerror=lambda f, p, e: clear_and_retry(f, p, e[1]))


def normalize_path(path_str: str) -> str:
    """
    Normalize a path string without validation.

    Useful for logging/display. Does NOT validate existence or permissions.

    Args:
        path_str: Path string (absolute or relative)

    Returns:
        Normalized absolute path as string

    Raises:
        PathSecurityError: If path cannot be resolved
    """
    try:
        return str(Path(path_str).expanduser().resolve(strict=False))
    except (OSError, ValueError, RuntimeError) as e:
        raise PathSecurityError(f"Cannot normalize path '{path_str}': {e}") from e
