"""Filesystem metadata layer: size, timestamps, POSIX mode/owner, Windows
attributes and extended attributes.

This is a *layer*, not a primary handler (``PRIMARY = False``, D3): the registry
picks a format handler for every file (hachoir as catch-all) and the API adds
these fields to the record (``HandlerRegistry.filesystem_layer()``).

* Timestamps come from ``st_*_ns`` through ``values.utc_iso_from_ns``: exact,
  UTC, valid before 1970 on Windows.
* Each field is read on its own: a failure drops only that field and is reported
  in ``record.warnings``. ``read_errors`` is used only when the file cannot be
  stat'ed at all.
* Writable: ``fs:mtime`` (``os.utime(ns=...)``, offset required, verified after
  the write) and UTF-8 ``user.`` extended attributes. Non-UTF-8 values are
  binary (read-only, deletable); other namespaces are read-only (D18).
* A file that cannot be written (Windows READONLY attribute, no write
  permission on the file or, on POSIX, on its folder: the transaction replaces
  the file) is read with nothing modifiable and the reason in
  ``read_only_reason`` (``write_block_reason``); the API then makes the whole
  record read-only and answers 403 to mutations.
* In a combined transaction the filesystem fields must be applied LAST: any
  later change to the temporary file would move its mtime again.
"""
from __future__ import annotations

import errno
import os
import stat
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable

from core import values
from core.base_handler import ABSENT, BaseMetadataHandler, Snapshot
from core.errors import ErrorCode, MetadataValueError, MetadataValueErrors, TargetNotWritableError
from core.models import MetadataField, MetadataRecord
from core.values import utc_iso_from_ns

MTIME_KEY = "fs:mtime"
XATTR_PREFIX = "xattr:"
WRITABLE_XATTR_NAMESPACE = "user."
SOURCE = "filesystem"

# Windows FILETIME counts from 1601-01-01; FILETIME 0 means "do not change" to
# SetFileTime, so the first storable instant is strictly after that epoch.
WINDOWS_EPOCH_NS = -11_644_473_600 * 10**9

_MTIME_SCHEMA = MetadataField(MTIME_KEY, "Modified", None, field_type="datetime", source=SOURCE)

# Read-only fs: keys (anything else under fs: is unknown).
_READ_ONLY_KEYS = frozenset({
    "fs:size", "fs:ctime", "fs:atime", "fs:permissions", "fs:owner", "fs:group", "fs:win_attrs",
})

_WIN_ATTRIBUTES: tuple[tuple[int, str], ...] = (
    (0x1, "READONLY"), (0x2, "HIDDEN"), (0x4, "SYSTEM"), (0x10, "DIRECTORY"),
    (0x20, "ARCHIVE"), (0x40, "DEVICE"), (0x80, "NORMAL"), (0x100, "TEMPORARY"),
    (0x200, "SPARSE_FILE"), (0x400, "REPARSE_POINT"), (0x800, "COMPRESSED"),
    (0x1000, "OFFLINE"), (0x2000, "NOT_CONTENT_INDEXED"), (0x4000, "ENCRYPTED"),
    (0x8000, "INTEGRITY_STREAM"), (0x10000, "VIRTUAL"), (0x20000, "NO_SCRUB_DATA"),
    (0x40000, "RECALL_ON_OPEN"), (0x80000, "PINNED"), (0x100000, "UNPINNED"),
    (0x400000, "RECALL_ON_DATA_ACCESS"),
)

# Errors meaning "this filesystem has no (such) extended attributes".
_XATTR_UNSUPPORTED = frozenset(
    getattr(errno, name) for name in ("ENOTSUP", "EOPNOTSUPP") if hasattr(errno, name))
_XATTR_MISSING = frozenset(
    getattr(errno, name) for name in ("ENODATA", "ENOATTR") if hasattr(errno, name))
_XATTR_TOO_LARGE = frozenset(
    getattr(errno, name) for name in ("E2BIG", "ENOSPC", "ERANGE", "EDQUOT") if hasattr(errno, name))


# ──────────────────────── extended attribute backends ────────────────────────

class _OsXattr:
    """Linux: ``os.*xattr`` (namespaced names: user., security., trusted., system.)."""
    namespaced = True

    def list(self, path: Path) -> list[str]:
        return os.listxattr(path)

    def get(self, path: Path, name: str) -> bytes:
        return os.getxattr(path, name)

    def set(self, path: Path, name: str, value: bytes) -> None:
        os.setxattr(path, name, value)

    def remove(self, path: Path, name: str) -> None:
        os.removexattr(path, name)


class _PackageXattr:
    """macOS/BSD: the ``xattr`` package. No namespaces: everything stays read-only (D18)."""

    def __init__(self, module) -> None:
        self._x = module
        self.namespaced = sys.platform.startswith("linux")

    def list(self, path: Path) -> list[str]:
        return list(self._x.listxattr(str(path)))

    def get(self, path: Path, name: str) -> bytes:
        return self._x.getxattr(str(path), name)

    def set(self, path: Path, name: str, value: bytes) -> None:
        self._x.setxattr(str(path), name, value)

    def remove(self, path: Path, name: str) -> None:
        self._x.removexattr(str(path), name)


def _load_xattr_backend():
    if sys.platform == "win32":
        return None
    if hasattr(os, "listxattr"):
        return _OsXattr()
    try:
        import xattr as module
    except ImportError:
        return None
    return _PackageXattr(module)


XATTR_BACKEND = _load_xattr_backend()


def _xattr_name(key: str) -> str | None:
    return key[len(XATTR_PREFIX):] if key.startswith(XATTR_PREFIX) else None


def _xattr_writable(name: str) -> bool:
    backend = XATTR_BACKEND
    return (backend is not None and getattr(backend, "namespaced", False)
            and name.startswith(WRITABLE_XATTR_NAMESPACE) and len(name) > len(WRITABLE_XATTR_NAMESPACE))


def _xattr_get(path: Path, name: str) -> bytes | None:
    """Raw value, or None when the attribute does not exist."""
    try:
        return bytes(XATTR_BACKEND.get(path, name))
    except OSError as exc:
        if exc.errno in _XATTR_MISSING:
            return None
        raise


def _xattr_os_error(key: str, exc: OSError) -> Exception:
    if exc.errno in (errno.EPERM, errno.EACCES):
        return TargetNotWritableError(f"{key}: {exc.strerror or exc}")
    if exc.errno in _XATTR_TOO_LARGE:
        return MetadataValueError(key, ErrorCode.TOO_LARGE, f"{key}: value too large for this filesystem")
    if exc.errno in _XATTR_UNSUPPORTED:
        return MetadataValueError(key, ErrorCode.UNSUPPORTED,
                                  f"{key}: extended attributes are not supported here")
    return exc


# ──────────────────────── helpers ────────────────────────────────────────────

def _time_field(key: str, label: str, ns: int, *, editable: bool = False) -> MetadataField:
    value = utc_iso_from_ns(ns)
    return MetadataField(key, label, value, raw_value=ns, editable=editable, deletable=False,
                         field_type="datetime", source=SOURCE)


def _ro(key: str, label: str, value: Any, field_type: str = "str", **kw) -> MetadataField:
    return MetadataField(key, label, value, editable=False, deletable=False,
                         field_type=field_type, source=SOURCE, **kw)


def _win_attribute_names(flags: int) -> list[str]:
    names = []
    for bit, name in _WIN_ATTRIBUTES:
        if flags & bit:
            names.append(name)
            flags &= ~bit
    bit = 1
    while flags:  # keep unknown bits visible instead of dropping them
        if flags & bit:
            names.append(f"0x{bit:08X}")
            flags &= ~bit
        bit <<= 1
    return names


def _created_ns(st) -> tuple[str, int]:
    if sys.platform == "win32":
        return "Created", getattr(st, "st_birthtime_ns", st.st_ctime_ns)
    return "Metadata Changed", st.st_ctime_ns


def _check_mtime_range(key: str, ns: int) -> None:
    if sys.platform == "win32" and ns <= WINDOWS_EPOCH_NS:
        raise MetadataValueError(key, ErrorCode.OUT_OF_RANGE,
                                 f"{key}: Windows stores file times only after 1601-01-01T00:00:00Z")


_FILE_ATTRIBUTE_READONLY = 0x1


def write_block_reason(path: Path, st: os.stat_result | None = None) -> str | None:
    """Why ``path`` cannot be modified at all, or None.

    Mirrors the checks of ``secure_atomic_write`` (403): ``os.access(W_OK)``
    (the READONLY attribute on Windows, the permission bits on POSIX) and, on
    POSIX, write access to the folder where the private copy is created. An ACL
    denying writes on Windows is only discovered by the write itself.
    """
    path = Path(path)
    try:
        st = os.stat(path) if st is None else st
    except OSError:
        return None   # unreadable: the read reports it
    if sys.platform == "win32" and getattr(st, "st_file_attributes", 0) & _FILE_ATTRIBUTE_READONLY:
        return "The file is read-only (READONLY attribute): clear it to modify the metadata"
    if not os.access(path, os.W_OK):
        return "No write permission for the file: its metadata cannot be modified"
    if sys.platform != "win32" and not os.access(path.parent, os.W_OK | os.X_OK):
        return ("No write permission for the folder of the file: its metadata cannot be modified "
                "(saving replaces the file atomically)")
    return None


# ──────────────────────── handler ────────────────────────────────────────────

class FilesystemHandler(BaseMetadataHandler):
    NAME = "filesystem"
    EXTENSIONS = frozenset()  # applies to every file, as a layer
    PRIMARY = False           # never returned by HandlerRegistry.get (D3)
    PRIORITY = 110            # after the hachoir catch-all (100)

    def can_handle(self, path: Path) -> bool:
        return True

    # ── read ────────────────────────────────────────────────────────────────

    def read(self, path: Path) -> MetadataRecord:
        path = Path(path)
        try:
            st = os.stat(path)
        except OSError as exc:
            message = f"Filesystem metadata unavailable: {exc}"
            return MetadataRecord(file_path=path, file_size=0, mtime=0.0, handler_name=self.NAME,
                                  read_errors=[message], warnings=[message])
        fields: list[MetadataField] = []
        warnings: list[str] = []
        blocked = write_block_reason(path, st)

        def add(key: str, build: Callable[[], MetadataField | list[MetadataField] | None]) -> None:
            try:
                result = build()
            except Exception as exc:  # isolate the failure to this field
                warnings.append(f"{key}: cannot read ({type(exc).__name__}: {exc})")
                return
            if result is not None:
                fields.extend(result if isinstance(result, list) else [result])

        add("fs:size", lambda: _ro("fs:size", "File Size (bytes)", st.st_size, "int"))
        add(MTIME_KEY, lambda: _time_field(MTIME_KEY, "Modified", st.st_mtime_ns, editable=blocked is None))
        add("fs:ctime", lambda: _time_field("fs:ctime", *_created_ns(st)))
        add("fs:atime", lambda: _time_field("fs:atime", "Last Accessed", st.st_atime_ns))
        if sys.platform == "win32":
            add("fs:win_attrs", lambda: self._win_attrs_field(st))
        else:
            add("fs:permissions", lambda: _ro("fs:permissions", "Permissions", stat.filemode(st.st_mode)))
            add("fs:owner", lambda: _ro("fs:owner", "Owner", _user_name(st.st_uid)))
            add("fs:group", lambda: _ro("fs:group", "Group", _group_name(st.st_gid)))
        self._read_xattrs(path, add, warnings)
        if blocked:
            for f in fields:
                f.editable = f.deletable = False

        record = MetadataRecord(
            file_path=path, file_size=st.st_size, mtime=st.st_mtime, handler_name=self.NAME,
            fields=fields, warnings=warnings,
            supports_write=any(f.editable for f in fields),
            supports_delete=any(f.deletable for f in fields),
            read_only_reason=blocked,
        )
        return record

    @staticmethod
    def _win_attrs_field(st) -> MetadataField | None:
        names = _win_attribute_names(st.st_file_attributes)
        if not names:
            return None
        return _ro("fs:win_attrs", "Windows Attributes", names, multi=True)

    def _read_xattrs(self, path: Path, add, warnings: list[str]) -> None:
        if XATTR_BACKEND is None:
            return
        try:
            names = list(XATTR_BACKEND.list(path))
        except OSError as exc:
            if exc.errno not in _XATTR_UNSUPPORTED:
                warnings.append(f"xattr: cannot list extended attributes ({exc})")
            return
        for name in names:
            add(f"{XATTR_PREFIX}{name}", lambda name=name: self._xattr_field(path, name))

    @staticmethod
    def _xattr_field(path: Path, name: str) -> MetadataField | None:
        data = _xattr_get(path, name)
        if data is None:  # removed between list and get
            return None
        key, label = f"{XATTR_PREFIX}{name}", f"xattr: {name}"
        writable = _xattr_writable(name)
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return MetadataField.binary_field(key, label, data, deletable=writable, source="xattr")
        return MetadataField(key, label, text, raw_value=text, editable=writable, deletable=writable,
                             field_type="str", source="xattr")

    # ── schema and validation ───────────────────────────────────────────────

    def writable_field(self, path: Path, key: str) -> MetadataField | None:
        """Schema for adding a new ``xattr:user.<name>`` (absent, writable namespace)."""
        name = _xattr_name(key)
        if name is None or not _xattr_writable(name) or "\x00" in name:
            return None
        try:
            if _xattr_get(Path(path), name) is not None:
                return None
        except OSError:
            return None
        return MetadataField(key, f"xattr: {name}", None, field_type="str", source="xattr")

    def canonical(self, field: MetadataField, value: Any) -> Any:
        if field.key == MTIME_KEY:
            canonical = values.canonicalize(_MTIME_SCHEMA, value)
            ns = values.ns_from_iso(canonical)
            _check_mtime_range(MTIME_KEY, ns)
            return utc_iso_from_ns(ns)  # what is read back: UTC
        if _xattr_name(field.key) is not None:
            return values.canonicalize(replace(field, field_type="str", multi=False, length=None,
                                               choices=[]), value)
        return values.canonicalize(field, value)

    def _validate_write(self, tmp: Path, fields: list[MetadataField]) -> list[Callable[[], None]]:
        errors: list[MetadataValueError] = []
        xattr_ops: list[Callable[[], None]] = []
        mtime_ops: list[Callable[[], None]] = []
        seen: set[str] = set()
        for f in fields:
            try:
                if f.key in seen:
                    raise MetadataValueError(f.key, ErrorCode.INVALID_VALUE, f"{f.key}: duplicate key")
                seen.add(f.key)
                name = _xattr_name(f.key)
                if f.key == MTIME_KEY:
                    canonical = self.canonical(f, f.value)
                    mtime_ops.append(lambda c=canonical: self._set_mtime(tmp, c))
                elif name is not None:
                    xattr_ops.append(self._prepare_xattr_write(tmp, f, name))
                elif f.key in _READ_ONLY_KEYS:
                    raise MetadataValueError(f.key, ErrorCode.READ_ONLY, f"{f.key}: read-only")
                else:
                    raise MetadataValueError(f.key, ErrorCode.UNKNOWN_FIELD, f"{f.key}: unknown field")
            except MetadataValueError as exc:
                errors.append(exc)
        _raise_collected(errors)
        return xattr_ops + mtime_ops  # mtime last: setting xattrs must not move it again

    def _prepare_xattr_write(self, tmp: Path, f: MetadataField, name: str) -> Callable[[], None]:
        if not _xattr_writable(name):
            raise MetadataValueError(f.key, ErrorCode.READ_ONLY,
                                     f"{f.key}: only {WRITABLE_XATTR_NAMESPACE} attributes are writable")
        try:
            current = _xattr_get(tmp, name)
        except OSError as exc:
            raise _xattr_os_error(f.key, exc) from exc
        if current is not None:
            try:
                current.decode("utf-8")
            except UnicodeDecodeError:
                raise MetadataValueError(f.key, ErrorCode.READ_ONLY,
                                         f"{f.key}: binary value, read-only") from None
        data = self.canonical(f, f.value).encode("utf-8")
        return lambda: self._set_xattr(tmp, f.key, name, data)

    # ── mutation hooks ──────────────────────────────────────────────────────

    def apply_write(self, tmp: Path, fields: list[MetadataField]) -> None:
        for op in self._validate_write(Path(tmp), fields):
            op()

    def apply_delete(self, tmp: Path, keys: list[str]) -> None:
        tmp = Path(tmp)
        errors: list[MetadataValueError] = []
        names: list[tuple[str, str]] = []
        for key in keys:
            name = _xattr_name(key)
            try:
                if name is None:
                    code = ErrorCode.NOT_DELETABLE if key == MTIME_KEY or key in _READ_ONLY_KEYS \
                        else ErrorCode.UNKNOWN_FIELD
                    raise MetadataValueError(key, code)
                if not _xattr_writable(name):
                    raise MetadataValueError(key, ErrorCode.NOT_DELETABLE,
                                             f"{key}: only {WRITABLE_XATTR_NAMESPACE} attributes are deletable")
                try:
                    present = _xattr_get(tmp, name) is not None
                except OSError as exc:
                    raise _xattr_os_error(key, exc) from exc
                if not present:
                    raise MetadataValueError(key, ErrorCode.UNKNOWN_FIELD, f"{key}: not present")
                names.append((key, name))
            except MetadataValueError as exc:
                errors.append(exc)
        _raise_collected(errors)
        for key, name in names:
            self._remove_xattr(tmp, key, name)

    def snapshot(self, tmp: Path, keys: list[str]) -> Snapshot:
        tmp = Path(tmp)
        snap: Snapshot = {}
        for key in keys:
            name = _xattr_name(key)
            if key == MTIME_KEY:
                snap[key] = os.stat(tmp).st_mtime_ns
            elif name is not None and XATTR_BACKEND is not None:
                data = _xattr_get(tmp, name)
                snap[key] = ABSENT if data is None else data
            else:
                raise MetadataValueError(key, ErrorCode.READ_ONLY, f"{key}: no snapshot for read-only keys")
        return snap

    def restore(self, tmp: Path, snap: Snapshot) -> None:
        tmp = Path(tmp)
        for key, raw in snap.items():
            name = _xattr_name(key)
            if name is None:
                continue
            if raw is ABSENT:
                if _xattr_get(tmp, name) is not None:
                    self._remove_xattr(tmp, key, name)
            else:
                self._set_xattr(tmp, key, name, raw)
        if MTIME_KEY in snap:  # last, after the xattrs
            self._utime_verified(tmp, snap[MTIME_KEY])

    # ── public wrappers (own transaction) ───────────────────────────────────

    def write(self, path: Path, fields: list[MetadataField]) -> None:
        self._validate_write(Path(path), fields)  # reject before opening the transaction
        self._atomic_write(path, lambda tmp: self.apply_write(tmp, fields))

    def delete(self, path: Path, keys: list[str]) -> None:
        self._atomic_write(path, lambda tmp: self.apply_delete(tmp, keys))

    # ── primitives ──────────────────────────────────────────────────────────

    def _set_mtime(self, tmp: Path, canonical: str) -> None:
        current = os.stat(tmp).st_mtime_ns
        if utc_iso_from_ns(current) == canonical:
            return  # unchanged value: keep the stored sub-microsecond precision
        self._utime_verified(tmp, values.ns_from_iso(canonical))

    @staticmethod
    def _utime_verified(tmp: Path, ns: int) -> None:
        _check_mtime_range(MTIME_KEY, ns)
        try:
            os.utime(tmp, ns=(os.stat(tmp).st_atime_ns, ns))
        except (OverflowError, ValueError) as exc:
            raise MetadataValueError(MTIME_KEY, ErrorCode.OUT_OF_RANGE, f"{MTIME_KEY}: {exc}") from exc
        except OSError as exc:
            if exc.errno == errno.EINVAL or getattr(exc, "winerror", None) == 87:
                raise MetadataValueError(MTIME_KEY, ErrorCode.OUT_OF_RANGE,
                                         f"{MTIME_KEY}: not storable on this filesystem") from exc
            raise
        stored = os.stat(tmp).st_mtime_ns
        if stored != ns:
            raise MetadataValueError(
                MTIME_KEY, ErrorCode.LOSSY_WRITE,
                f"{MTIME_KEY}: the filesystem stored {utc_iso_from_ns(stored)} "
                f"instead of {utc_iso_from_ns(ns)}")

    @staticmethod
    def _set_xattr(tmp: Path, key: str, name: str, data: bytes) -> None:
        try:
            XATTR_BACKEND.set(tmp, name, data)
            stored = _xattr_get(tmp, name)
        except OSError as exc:
            raise _xattr_os_error(key, exc) from exc
        if stored != data:
            raise MetadataValueError(key, ErrorCode.LOSSY_WRITE, f"{key}: value not stored exactly")

    @staticmethod
    def _remove_xattr(tmp: Path, key: str, name: str) -> None:
        try:
            XATTR_BACKEND.remove(tmp, name)
        except OSError as exc:
            raise _xattr_os_error(key, exc) from exc


def _raise_collected(errors: list[MetadataValueError]) -> None:
    if len(errors) == 1:
        raise errors[0]
    if errors:
        raise MetadataValueErrors(errors)


def _user_name(uid: int) -> str:
    try:
        import pwd
        return pwd.getpwuid(uid).pw_name
    except (ImportError, KeyError):
        return str(uid)  # no passwd entry (containers): show the numeric id


def _group_name(gid: int) -> str:
    try:
        import grp
        return grp.getgrgid(gid).gr_name
    except (ImportError, KeyError):
        return str(gid)
