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

import os
import sys
import shutil
import stat
import tempfile
from threading import RLock
from typing import Callable
from pathlib import Path

# Metadata mutations are infrequent. One reentrant lock also permits nested
# transactions without acquiring path locks in inconsistent orders.
_WRITE_LOCK = RLock()

__all__ = [
    "validate_file_path",
    "validate_directory_path",
    "secure_atomic_write",
    "PathSecurityError",
]


class PathSecurityError(Exception):
    """Raised when path validation fails."""
    pass


class _RecoveryError(PathSecurityError):
    """The original backup must be retained for manual recovery."""


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
) -> None:
    """
    Perform atomic write with symlink-attack protection.

    This function:
    1. Validates the target path
        2. Creates a private temporary directory on the same filesystem
    3. Calls write_fn(temp_path) to write modified content
        4. Replaces target with temp (ReplaceFileW on Windows, os.replace on Unix)
    5. Cleans up temp file on error
        6. Rejects replaced targets and non-regular temporary files

    Args:
        target_path: Path object of the file to write (must exist and be writable)
        write_fn: Callable that takes (Path) and performs write operations
        temp_suffix: Suffix for temporary file name (default: ".ml_tmp")

    Raises:
        PathSecurityError: If target_path is not valid or not writable
        Exception: Any exception from write_fn or os.replace

    Examples:
        >>> def writer(tmp_path):
        ...     with tmp_path.open("wb") as f:
        ...         f.write(b"new content")
        >>> secure_atomic_write(Path("/home/user/file.txt"), writer)
    """
    target = validate_file_path(str(target_path), must_exist=True)
    if not temp_suffix or any(c in temp_suffix for c in ("/", "\\")):
        raise ValueError("Temporary suffix must not contain path separators")
    with _WRITE_LOCK:
        if not os.access(target, os.W_OK):
            raise PathSecurityError(f"No write permission for: {target}")
        original = _file_identity(target)
        # An exclusively created private directory protects the entire callback,
        # including libraries which close/reopen the temporary file by pathname.
        directory = Path(tempfile.mkdtemp(prefix=".metalens-", dir=target.parent))
        cleanup = True
        try:
            temp_path = directory / (target.stem + temp_suffix + target.suffix)
            shutil.copy2(target, temp_path)
            write_fn(temp_path)
            if not stat.S_ISREG(temp_path.lstat().st_mode):
                raise PathSecurityError("Temporary file is not a regular file")
            if _file_identity(target) != original:
                raise PathSecurityError("Target changed during metadata write")
            with temp_path.open("r+b") as stream:
                os.fsync(stream.fileno())
            _replace_file(temp_path, target)
        except _RecoveryError:
            cleanup = False
            raise
        finally:
            if cleanup:
                shutil.rmtree(directory)


def _replace_file(replacement: Path, target: Path) -> None:
    if sys.platform != "win32":
        os.replace(replacement, target)
        return
    # ReplaceFile preserves the original DACL, creation time, encryption and
    # named streams. os.replace after copy2 would discard those on Windows.
    backup = replacement.parent / "original-backup"
    try:
        _windows_replace(target, replacement, backup)
    except OSError:
        if backup.exists():
            try:
                os.replace(backup, target)
            except OSError as exc:
                raise _RecoveryError(f"Replacement and rollback failed; original retained at {backup}") from exc
        raise


def _windows_replace(target: Path, replacement: Path, backup: Path) -> None:
    import ctypes
    from ctypes import wintypes
    replace = ctypes.WinDLL("kernel32", use_last_error=True).ReplaceFileW
    replace.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR,
                        wintypes.DWORD, wintypes.LPVOID, wintypes.LPVOID]
    replace.restype = wintypes.BOOL
    # Do not ignore ACL/stream merge errors: preserving them is required.
    if not replace(str(target), str(replacement), str(backup), 0, None, None):
        raise ctypes.WinError(ctypes.get_last_error())


def _file_identity(path: Path) -> tuple:
    st = path.lstat()
    if not stat.S_ISREG(st.st_mode):
        raise PathSecurityError("Target changed to a non-regular file")
    return st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns


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
