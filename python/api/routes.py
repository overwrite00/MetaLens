"""HTTP API of the sidecar.

Error contract (plan B4): every error response has the uniform body
``{"ok": false, "errors": [{"code", "key", "message"}], "detail": "<summary>"}``
(``detail`` is always a string); successful mutations return
``{"ok": true, "errors": []}``. Status codes: 400 invalid path/parameters,
401/403/503 authentication (``main.py``), 403 target not writable,
409 concurrent modification, 422 validation (all errors collected before the
file is touched), 500 unexpected.

Mutations run in ONE ``secure_atomic_write`` transaction per request (plan B5):
validation -> ``snapshot`` of the touched keys -> ``apply_delete``/``apply_write``
hooks on the private temporary copy -> read-after-write verification -> replace.
Only handlers implementing the hooks (``apply_write``/``apply_delete`` plus
``snapshot``/``restore``) can modify a file; any other handler is read-only.

Exact Undo (plan F3B, D1): a save returns
``{"ok": true, "errors": [], "undo_token": <str|null>, "undo_unavailable": <null|{code, message}>,
"warnings": [str]}``:

* ``undo_token``: opaque token when at least one key changed and the change can
  be undone; ``null`` otherwise.
* ``undo_unavailable``: ``null`` when there is a token or the save was a real
  no-op (no key changed); ``{"code", "message"}`` when something changed but no
  Undo is possible: ``undo_too_large`` (the pre/post images exceed the journal
  budget, e.g. deleting a 70 MiB cover; the deletion is still performed, by
  design) or ``undo_snapshot_failed`` (the raw images could not be captured:
  logged as an error).
* ``warnings``: steps that failed AFTER the new content was committed (Windows
  attributes, permissions or owner not restored, temporary folder left behind).
  The save succeeded: it is never reported as an error.

``POST /undo {"token"}`` restores the raw pre-images from the in-memory journal
(``api.undo_journal``) in one verified transaction and answers
``{"ok": true, "errors": [], "keys": [...], "undo_token": null, "warnings": [...]}``.
410 ``undo_expired``: unknown/used/evicted token; 409 ``concurrent_modification``:
a touched key no longer holds its post-value or the file was replaced (the
entry is kept). The mtime is restored only when ``fs:mtime`` was touched (D19).

Concurrency: reads (/read, /diff, /hash) of a file share a per-file lock and a
mutation (/write, /delete, /undo) holds it exclusively, so the API never reads
a file it is replacing. Transient conditions caused by OTHER programs are
retried with backoff (``READ_RETRY_SECONDS``) and never produce a 500: a file
still locked is 503 ``file_busy`` (with ``Retry-After``), a file that vanished
is 404 ``file_not_found``, a persistent access denial 403 ``access_denied``.

A file that cannot be written (READONLY attribute, no write permission on the
file or its folder) is served with every field read-only, ``format_supports_*``
false and ``read_only_reason`` explaining why; /write and /delete answer 403
``not_writable`` before any validation.

/diff compares the same records as /read except the volatile filesystem times
``fs:atime`` (changed by reading) and ``fs:ctime`` (creation / metadata-change
time of the file instance, different for every copy): ``DIFF_IGNORED_KEYS``.
"""
from __future__ import annotations

import filecmp
import hashlib
import importlib
import json
import logging
import os
import shutil
import struct
import time
from contextlib import contextmanager
from dataclasses import dataclass, field as dc_field, replace
from pathlib import Path
from threading import Condition, Lock, RLock
from typing import Any, Callable, Iterator, TypeVar

from fastapi import APIRouter, FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator
from starlette.exceptions import HTTPException as StarletteHTTPException

from config import settings
from core.registry import HandlerRegistry

# Trigger handler registration as an explicit side effect.
importlib.import_module("core.handlers")
from api.byte_patch import apply_patch, make_patch
from api.undo_journal import UndoJournal, UndoTooLargeError
from core.base_handler import ABSENT, BaseMetadataHandler
from core.diff import compute_diff
from core.errors import (ConcurrentModificationError, ErrorCode, MetadataValueError,
                         MetadataValueErrors, TargetNotWritableError, describe_error, error_entry)
from core.handlers.filesystem_handler import MTIME_KEY, FilesystemHandler, write_block_reason
from core.models import MetadataField, MetadataRecord
from core import path_security
from core.path_security import (validate_file_path, validate_directory_path,
                                secure_atomic_write, FileBusyError, NothingToCommit, PathSecurityError,
                                is_active_transaction_dir, raise_if_unreadable, winerror_of)

router = APIRouter()
logger = logging.getLogger(__name__)

T = TypeVar("T")


# ──────────────────────────────── errors ─────────────────────────────────────

class ApiCode:
    """API-level error codes (field-level codes are in ``core.errors.ErrorCode``)."""
    INVALID_PATH = "invalid_path"              # 400
    INVALID_PARAMETER = "invalid_parameter"    # 400
    INVALID_REQUEST = "invalid_request"        # 422 request schema (pydantic)
    DUPLICATE_KEY = "duplicate_key"            # 422
    NO_HANDLER = "no_handler"                  # 422
    ACCESS_DENIED = "access_denied"            # 403 (file or folder not readable)
    UNAUTHORIZED = "unauthorized"              # 401
    ORIGIN_NOT_ALLOWED = "origin_not_allowed"  # 403
    AUTH_NOT_CONFIGURED = "auth_not_configured"  # 503
    FILE_BUSY = ErrorCode.FILE_BUSY            # 503 locked/changing because of another program (retry)
    FILE_NOT_FOUND = ErrorCode.FILE_NOT_FOUND  # 404 the file/folder vanished during the request
    NOT_FOUND = "not_found"                    # 404 unknown route
    METHOD_NOT_ALLOWED = "method_not_allowed"  # 405
    HTTP_ERROR = "http_error"
    INTERNAL_ERROR = "internal_error"          # 500


class UndoCode:
    """``undo_unavailable.code`` of a save that changed something without an Undo."""
    TOO_LARGE = "undo_too_large"               # pre/post images exceed the journal budget
    SNAPSHOT_FAILED = "undo_snapshot_failed"   # the raw images could not be captured (logged)


class ApiError(Exception):
    """An error response: HTTP status + ``errors`` entries."""

    def __init__(self, status: int, errors: list[dict], detail: str | None = None,
                 headers: dict[str, str] | None = None) -> None:
        self.status = status
        self.errors = errors
        self.detail = detail or "; ".join(e["message"] for e in errors)
        self.headers = headers
        super().__init__(self.detail)

    @classmethod
    def one(cls, status: int, code: str, message: str, key: str | None = None,
            headers: dict[str, str] | None = None) -> "ApiError":
        return cls(status, [error_entry(code, key, message)], headers=headers)


def error_response(status: int, errors: list[dict], detail: str | None = None,
                   headers: dict[str, str] | None = None) -> JSONResponse:
    detail = detail or "; ".join(e["message"] for e in errors) or "Error"
    return JSONResponse({"ok": False, "errors": errors, "detail": detail}, status_code=status,
                        headers=headers)


_RETRY_AFTER = {"Retry-After": "1"}


def _busy(message: str) -> ApiError:
    return ApiError.one(503, ApiCode.FILE_BUSY, message, headers=_RETRY_AFTER)


def _internal(exc: BaseException, context: str) -> ApiError:
    """500 for a bug: logged with its traceback, uniform body."""
    logger.error("%s: %s: %s", context, type(exc).__name__, exc,
                 exc_info=(type(exc), exc, exc.__traceback__))
    return ApiError.one(500, ApiCode.INTERNAL_ERROR, f"Unexpected error: {type(exc).__name__}: {exc}")


_HTTP_CODES = {400: ApiCode.INVALID_PARAMETER, 401: ApiCode.UNAUTHORIZED, 403: ApiCode.ACCESS_DENIED,
               404: ApiCode.NOT_FOUND, 405: ApiCode.METHOD_NOT_ALLOWED, 422: ApiCode.INVALID_REQUEST}


def install_error_handlers(app: FastAPI) -> None:
    """Register the handlers producing the uniform error body."""

    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError):
        return error_response(exc.status, exc.errors, exc.detail, exc.headers)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError):
        errors = []
        for err in exc.errors():
            key = ".".join(str(part) for part in err.get("loc", ())) or None
            message = str(err.get("msg", "invalid request"))
            errors.append(error_entry(ApiCode.INVALID_REQUEST, key, f"{key}: {message}" if key else message))
        if not errors:
            errors = [error_entry(ApiCode.INVALID_REQUEST, None, "invalid request")]
        return error_response(422, errors)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException):
        message = exc.detail if isinstance(exc.detail, str) else json.dumps(exc.detail, default=str)
        code = _HTTP_CODES.get(exc.status_code, ApiCode.HTTP_ERROR)
        return error_response(exc.status_code, [error_entry(code, None, message)])

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception):
        error = _internal(exc, f"Unhandled error in {request.method} {request.url.path}")
        return error_response(error.status, error.errors, error.detail)


def _path_error(error: PathSecurityError) -> ApiError:
    return ApiError.one(400, ApiCode.INVALID_PATH, f"Invalid path: {error}")


def _valid_file(path_str: str) -> Path:
    try:
        return validate_file_path(path_str, must_exist=True)
    except PathSecurityError as exc:
        raise _path_error(exc) from exc


# Library exceptions caused by a value (never a 500 for user input, plan B4).
# TypeError is deliberately NOT here: a value of the wrong type is rejected by
# ``canonical`` (``wrong_type``) before any hook runs, so a TypeError raised
# later is a bug of a handler or library and must surface as a logged 500.
_VALUE_EXCEPTIONS: tuple[type[BaseException], ...] = (ValueError, struct.error, OverflowError)
try:  # pragma: no branch
    from mutagen import MutagenError
    _VALUE_EXCEPTIONS += (MutagenError,)
except ImportError:  # pragma: no cover
    pass


def _value_error(exc: BaseException, keys: list[str]) -> MetadataValueError:
    """Attribute a library value error to the key when the operation had only one."""
    if isinstance(exc, MetadataValueError):
        return exc
    key = keys[0] if len(keys) == 1 else None
    code = ErrorCode.UNSUPPORTED if isinstance(exc, NotImplementedError) else ErrorCode.INVALID_VALUE
    message = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
    err = MetadataValueError(key, code, f"{key}: {message}" if key else message)
    err.__cause__ = exc
    return err


def _transaction_error(exc: BaseException) -> ApiError:
    if isinstance(exc, ApiError):
        return exc
    if isinstance(exc, MetadataValueErrors):
        return ApiError(422, exc.to_dicts())
    if isinstance(exc, MetadataValueError):
        return ApiError(422, [exc.to_dict()])
    if isinstance(exc, TargetNotWritableError):
        return ApiError(403, [exc.to_dict()])
    if isinstance(exc, ConcurrentModificationError):
        return ApiError(409, [exc.to_dict()])
    if isinstance(exc, FileBusyError):
        return _busy(str(exc))
    if isinstance(exc, path_security._RecoveryError):
        return _internal(exc, "Transaction rollback failed")
    if isinstance(exc, PathSecurityError):
        # The target vanished or stopped being a regular file during the request.
        return ApiError.one(409, ErrorCode.CONCURRENT_MODIFICATION, str(exc))
    if winerror_of(exc) in (32, 33):   # sharing/lock violation outside the retried steps
        return _busy(f"The file is in use by another program ({exc}); try again")
    if isinstance(exc, PermissionError):
        return ApiError.one(403, ErrorCode.NOT_WRITABLE, f"File cannot be written: {exc}")
    if isinstance(exc, FileNotFoundError):
        return ApiError.one(409, ErrorCode.CONCURRENT_MODIFICATION, f"The file disappeared during the write: {exc}")
    if isinstance(exc, (NotImplementedError, *_VALUE_EXCEPTIONS)):
        return ApiError(422, [_value_error(exc, []).to_dict()])
    return _internal(exc, "Metadata transaction failed")


# ──────────────────────────────── /health ────────────────────────────────────

@router.get("/health")
def health():
    return {"status": "ok", "version": settings.version, "app": settings.app_name}


# ──────────────────────────────── /hash ──────────────────────────────────────

_SUPPORTED_ALGORITHMS = {"md5", "sha1", "sha256", "sha512", "blake2b"}


@router.get("/hash")
def hash_file(
    path: str = Query(..., description="Absolute file path"),
    algorithms: str = Query("md5,sha1,sha256,sha512,blake2b", description="Comma-separated list of algorithms"),
):
    fpath = _valid_file(path)
    requested = {a.strip().lower() for a in algorithms.split(",") if a.strip()}
    if not requested:
        raise ApiError.one(400, ApiCode.INVALID_PARAMETER, "algorithms: no algorithm requested", "algorithms")
    unknown = requested - _SUPPORTED_ALGORITHMS
    if unknown:
        raise ApiError.one(400, ApiCode.INVALID_PARAMETER,
                           f"algorithms: unsupported {', '.join(sorted(unknown))}", "algorithms")

    def compute() -> tuple[int, dict[str, Any]]:
        hashers = {alg: hashlib.new(alg) for alg in sorted(requested)}
        with fpath.open("rb") as f:
            size = os.fstat(f.fileno()).st_size
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                for h in hashers.values():
                    h.update(chunk)
        return size, hashers

    with PATH_LOCKS.reading(fpath):
        try:
            size, hashers = stable_read(fpath, compute)
        except OSError as exc:   # not transient (e.g. an I/O error of the device)
            raise _internal(exc, f"Hashing {fpath} failed") from exc

    return {"path": str(fpath), "size": size, "hashes": {alg: h.hexdigest() for alg, h in hashers.items()}}


# ──────────────────────── per-file locks and stable reads ────────────────────

class _FileLock:
    """Readers-writer lock of one file: reads share it, a mutation holds it alone.

    A waiting mutation blocks new readers, so continuous reads cannot starve it.
    Not reentrant: each request takes a file's lock once (see ``FileLocks``).
    """

    def __init__(self) -> None:
        self._cond = Condition(Lock())
        self._readers = 0
        self._writing = False
        self._writers_waiting = 0
        self.users = 0   # registry reference count (guarded by FileLocks._guard)

    def acquire(self, write: bool) -> None:
        with self._cond:
            if write:
                self._writers_waiting += 1
                try:
                    while self._writing or self._readers:
                        self._cond.wait()
                finally:
                    self._writers_waiting -= 1
                self._writing = True
            else:
                while self._writing or self._writers_waiting:
                    self._cond.wait()
                self._readers += 1

    def release(self, write: bool) -> None:
        with self._cond:
            if write:
                self._writing = False
            else:
                self._readers -= 1
            self._cond.notify_all()


class FileLocks:
    """Registry of per-file locks, keyed by the resolved, case-normalized path.

    Locks of several files are always taken in sorted key order and a request
    never waits for ``_MUTATION_LOCK`` while holding one: no deadlock.
    """

    def __init__(self) -> None:
        self._guard = Lock()
        self._locks: dict[str, _FileLock] = {}

    @staticmethod
    def key(path: Path) -> str:
        return os.path.normcase(str(path))

    @contextmanager
    def _hold(self, paths: tuple[Path, ...], write: bool) -> Iterator[None]:
        keys = sorted({self.key(p) for p in paths})
        with self._guard:
            locks = []
            for key in keys:
                lock = self._locks.get(key)
                if lock is None:
                    lock = self._locks[key] = _FileLock()
                lock.users += 1
                locks.append((key, lock))
        acquired: list[_FileLock] = []
        try:
            for _key, lock in locks:
                lock.acquire(write)
                acquired.append(lock)
            yield
        finally:
            for lock in reversed(acquired):
                lock.release(write)
            with self._guard:
                for key, lock in locks:
                    lock.users -= 1
                    if lock.users == 0:
                        del self._locks[key]

    def reading(self, *paths: Path):
        return self._hold(paths, write=False)

    def writing(self, path: Path):
        return self._hold((path,), write=True)

    def __len__(self) -> int:
        with self._guard:
            return len(self._locks)


PATH_LOCKS = FileLocks()

# Transient conditions caused by other programs are retried for this long.
READ_RETRY_SECONDS = 1.5


def _file_state(path: Path) -> tuple:
    st = os.stat(path)
    return st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns


def _probe(path: Path) -> tuple:
    """Current version of the file; opening it (like every handler does) reveals locks
    (on Windows with the real error code: sharing violation vs access denied)."""
    state = _file_state(path)
    raise_if_unreadable(path)
    return state


def _transient_kind(exc: OSError) -> str | None:
    if isinstance(exc, (FileNotFoundError, NotADirectoryError)):
        return "missing"
    if winerror_of(exc) in (32, 33):     # sharing / lock violation
        return "busy"
    if isinstance(exc, PermissionError):  # incl. ACCESS_DENIED of a file being replaced
        return "denied"
    return None


def stable_read(path: Path, read: Callable[[], T]) -> T:
    """Run ``read`` on a stable version of ``path``.

    The result is accepted only if the file (device, inode, size, mtime) did not
    change while it was read. Locks, a file vanishing or changing because of
    another program are retried with backoff for ``READ_RETRY_SECONDS``, then
    reported as 503 ``file_busy`` / 404 ``file_not_found`` / 403 ``access_denied``.
    Other errors propagate unchanged.
    """
    deadline = time.monotonic() + READ_RETRY_SECONDS
    delay = 0.02
    while True:
        try:
            before = _probe(path)
            result = read()
            if _file_state(path) == before:
                return result
            kind: str | None = "changed"
            last: OSError | None = None
        except OSError as exc:
            kind, last = _transient_kind(exc), exc
            if kind is None:
                raise
        if time.monotonic() + delay > deadline:
            raise _read_failure(path, kind, last)
        time.sleep(delay)
        delay = min(delay * 2, 0.25)


def _read_failure(path: Path, kind: str, exc: OSError | None) -> ApiError:
    if kind == "missing":
        return ApiError.one(404, ApiCode.FILE_NOT_FOUND, f"The file no longer exists: {path}")
    if kind == "denied":
        return ApiError.one(403, ApiCode.ACCESS_DENIED, f"Access denied: {path} ({exc})")
    if kind == "busy":
        return _busy(f"The file is in use by another program ({exc}); try again")
    return _busy("The file kept changing while it was read (another program is writing it); try again")


# ──────────────────────────────── /list ──────────────────────────────────────

@router.get("/list")
def list_directory(path: str = Query(..., description="Absolute directory path")):
    try:
        dirpath = validate_directory_path(path, must_exist=True)
    except PathSecurityError as exc:
        raise _path_error(exc) from exc

    items = []
    try:
        entries = sorted(dirpath.iterdir(), key=lambda e: (e.is_file(), e.name.lower()))
    except (FileNotFoundError, NotADirectoryError) as exc:
        raise ApiError.one(404, ApiCode.FILE_NOT_FOUND, f"The folder no longer exists: {dirpath}") from exc
    except PermissionError as exc:
        raise ApiError.one(403, ApiCode.ACCESS_DENIED, str(exc)) from exc
    except OSError as exc:
        raise _internal(exc, f"Listing {dirpath} failed") from exc

    for entry in entries:
        if is_active_transaction_dir(entry):
            continue   # private folder of a save in progress: never shown
        try:
            st = entry.stat()
            handler_name = "—"
            try:
                handler_name = HandlerRegistry.get(entry).NAME
            except ValueError:
                # No handler matches this entry; keep the placeholder name.
                pass
            items.append({
                "name": entry.name,
                "path": str(entry),
                "is_dir": entry.is_dir(),
                "size": st.st_size if entry.is_file() else None,
                "mtime": st.st_mtime,
                "ext": entry.suffix.lower() if entry.is_file() else "",
                "handler": handler_name,
            })
        except OSError:
            continue
    return {"path": str(dirpath), "items": items}


# ──────────────────────── shared full record ─────────────────────────────────

FORMAT, FS = "format", "fs"


@dataclass
class _Context:
    """Everything a request needs about one file, built by ``_read_full_record``."""
    path: Path
    handler: BaseMetadataHandler        # format handler (the filesystem one for unknown files)
    fs_handler: BaseMetadataHandler
    record: MetadataRecord              # merged record, exactly as /read serves it
    layer: dict[str, str] = dc_field(default_factory=dict)          # key -> FORMAT | FS
    fields: dict[str, MetadataField] = dc_field(default_factory=dict)  # served + addable
    not_writable: str | None = None     # why the file itself cannot be written (403)

    @property
    def fs_primary(self) -> bool:
        return self.handler is self.fs_handler

    def handler_of(self, layer: str) -> BaseMetadataHandler:
        return self.fs_handler if layer == FS else self.handler


def _handler_for(path: Path) -> BaseMetadataHandler:
    try:
        return HandlerRegistry.get(path)
    except ValueError as exc:
        raise ApiError.one(422, ApiCode.NO_HANDLER, f"No handler for {path.suffix or path.name}") from exc


def _filesystem_layer() -> BaseMetadataHandler:
    """The registered filesystem layer (``fs:*`` / ``xattr:*``), shared by every file."""
    try:
        return HandlerRegistry.filesystem_layer()
    except KeyError:  # registry cleared (tests): use a private instance
        return FilesystemHandler()


def _merge_layers(handler, fs_handler, path: Path) -> tuple[MetadataRecord, MetadataRecord, dict[str, str]]:
    """Format record + filesystem layer merged, keys made unique.

    Returns (merged record, filesystem record, key -> layer). Used for the
    record served by /read and /diff and for the read-after-write verification.
    """
    record = handler.read(path)
    fs_ids: set[int] = set()
    if handler is fs_handler:
        fs_record = record
        fs_ids = {id(f) for f in record.fields}
    else:
        fs_record = fs_handler.read(path)
        present = {f.key for f in record.fields}
        for f in fs_record.fields:
            if f.key not in present:
                record.fields.append(f)
                fs_ids.add(id(f))
        record.warnings.extend(f"Filesystem: {e}" for e in fs_record.read_errors)
        record.warnings.extend(fs_record.warnings)
    record.dedupe_keys()
    layer = {f.key: FS if id(f) in fs_ids else FORMAT for f in record.fields}
    return record, fs_record, layer


def _can_mutate(handler: BaseMetadataHandler, hook: str) -> bool:
    """The handler implements ``hook`` and the snapshot/restore pair of the exact Undo."""
    return handler.overrides(hook) and handler.overrides("snapshot") and handler.overrides("restore")


def _addable(handler, path: Path, record: MetadataRecord, warnings: list[str]) -> list[MetadataField]:
    if not handler.overrides("writable_fields") or not _can_mutate(handler, "apply_write"):
        return []
    try:
        return list(handler.writable_fields(path, record))
    except Exception as exc:  # the schema is optional: never fail the read for it
        logger.warning("Addable fields of %s unavailable", path, exc_info=True)
        warnings.append(f"Addable fields unavailable: {describe_error(exc)}")
        return []


def _read_full_record(path: Path) -> _Context:
    handler = _handler_for(path)
    fs_handler = _filesystem_layer()
    # The registry never returns a layer as primary handler; kept defensive.
    fs_primary = handler is fs_handler or not HandlerRegistry.is_primary(handler)
    if fs_primary:
        fs_handler = handler
    record, fs_record, layer = _merge_layers(handler, fs_handler, path)

    if fs_primary:
        # No format-specific metadata: only the filesystem layer is modifiable.
        fmt_write = fmt_delete = False
        reason = record.read_only_reason or "No format-specific metadata handler for this file"
    else:
        blocked = bool(record.read_errors)
        claimed = bool(record.format_supports_write or record.format_supports_delete)
        fmt_write = bool(record.format_supports_write) and not blocked and _can_mutate(handler, "apply_write")
        fmt_delete = bool(record.format_supports_delete) and not blocked and _can_mutate(handler, "apply_delete")
        reason = record.read_only_reason
        if blocked and reason is None and claimed:
            reason = "Metadata could not be read completely; format metadata is read-only"
        elif reason is None and claimed and not (fmt_write or fmt_delete):
            reason = f"The {handler.NAME or 'format'} handler cannot modify this file"
        for f in record.fields:
            if layer[f.key] == FORMAT:
                f.editable = f.editable and fmt_write
                f.deletable = f.deletable and fmt_delete
    # Filesystem layer: modifiable only through the same hooks (exact Undo).
    for f in record.fields:
        if layer[f.key] == FS:
            f.editable = f.editable and _can_mutate(fs_handler, "apply_write")
            f.deletable = f.deletable and _can_mutate(fs_handler, "apply_delete")

    # A file that cannot be written at all (READONLY, permissions): nothing is
    # modifiable, whatever the format could do (/write and /delete answer 403).
    not_writable = write_block_reason(path)
    if not_writable:
        fmt_write = fmt_delete = False
        reason = "; ".join([not_writable, *([reason] if reason and reason != not_writable else [])])
        for f in record.fields:
            f.editable = f.deletable = False

    addable: list[tuple[MetadataField, str]] = []
    if not_writable:
        pass
    elif fmt_write:
        format_view = MetadataRecord(record.file_path, record.file_size, record.mtime, record.handler_name,
                                     fields=[f for f in record.fields if layer[f.key] == FORMAT])
        addable += [(f, FORMAT) for f in record.addable_fields]
        addable += [(f, FORMAT) for f in _addable(handler, path, format_view, record.warnings)]
    else:
        addable += [(f, FS) for f in (fs_record.addable_fields if fs_primary else [])]
    if not not_writable:
        addable += [(f, FS) for f in _addable(fs_handler, path, fs_record, record.warnings)]

    ctx = _Context(path, handler, fs_handler, record, dict(layer), {f.key: f for f in record.fields},
                   not_writable=not_writable)
    unique: list[MetadataField] = []
    for f, lay in addable:
        if f.key in ctx.fields or not f.editable or f.field_type == "binary":
            continue
        ctx.fields[f.key] = f
        ctx.layer[f.key] = lay
        unique.append(f)

    fs_fields = [f for f in record.fields if layer[f.key] == FS]
    fs_addable = any(ctx.layer[f.key] == FS for f in unique)
    record.addable_fields = unique
    record.format_supports_write = fmt_write
    record.format_supports_delete = fmt_delete
    record.read_only_reason = None if (fmt_write or fmt_delete) and not fs_primary else reason
    record.supports_write = fmt_write or fs_addable or any(f.editable for f in fs_fields)
    record.supports_delete = fmt_delete or any(f.deletable for f in fs_fields)
    return ctx


# ──────────────────────────────── /read ──────────────────────────────────────

@router.get("/read")
def read_metadata(path: str = Query(..., description="Absolute file path")):
    fpath = _valid_file(path)
    with PATH_LOCKS.reading(fpath):
        return stable_read(fpath, lambda: _read_full_record(fpath)).record.to_dict()


# ──────────────────────────────── /write ─────────────────────────────────────

class WriteField(BaseModel):
    # Only key and value are read: client permission/type flags are ignored.
    key: str = Field(min_length=1)
    value: Any


class WriteRequest(BaseModel):
    path: str
    fields: list[WriteField] = Field(default_factory=list)
    delete: list[str] = Field(default_factory=list)   # D2: one atomic save

    @model_validator(mode="after")
    def _something_to_do(self):
        if not self.fields and not self.delete:
            raise ValueError("nothing to save: give 'fields' and/or 'delete'")
        return self


class DeleteRequest(BaseModel):
    path: str
    keys: list[str] = Field(min_length=1)


def _is_filesystem_key(key: str) -> bool:
    return key.startswith(("fs:", "xattr:"))


def _lookup(ctx: _Context, key: str) -> tuple[MetadataField, str] | None:
    """Server-owned schema of ``key``: served field, addable field or dynamic key.

    Dynamic keys (e.g. ``tag:TXXX:<desc>``, ``xattr:user.<name>``) are valid
    keys that no fixed schema lists; the handler's ``writable_field`` decides.
    """
    if key in ctx.fields:
        return ctx.fields[key], ctx.layer[key]
    if _is_filesystem_key(key) or ctx.fs_primary:
        if not _can_mutate(ctx.fs_handler, "apply_write"):
            return None
        field = ctx.fs_handler.writable_field(ctx.path, key)
        return (field, FS) if field is not None else None
    if ctx.record.format_supports_write:
        field = ctx.handler.writable_field(ctx.path, key)
        if field is not None:
            return field, FORMAT
    return None


def _validate_value(handler, field: MetadataField, value: Any) -> Any:
    """Canonical value to write, or MetadataValueError."""
    if value is None:
        raise MetadataValueError(field.key, ErrorCode.NULL_VALUE, f"{field.key}: null is not a valid value")
    try:
        return handler.canonical(field, value)
    except MetadataValueError as exc:
        raise exc.with_key(field.key) from exc
    except _VALUE_EXCEPTIONS as exc:
        raise _value_error(exc, [field.key]) from exc


@dataclass
class _Plan:
    writes: dict[str, list[MetadataField]] = dc_field(default_factory=lambda: {FORMAT: [], FS: []})
    deletes: dict[str, list[str]] = dc_field(default_factory=lambda: {FORMAT: [], FS: []})
    expected: dict[str, Any] = dc_field(default_factory=dict)   # key -> canonical value
    notes: list[str] = dc_field(default_factory=list)           # informational, returned as warnings
    linked: set[str] = dc_field(default_factory=set)            # keys added by the IPTC<->XMP sync

    def keys(self, lay: str) -> list[str]:
        return [*self.deletes[lay], *(f.key for f in self.writes[lay])]


def _plan(ctx: _Context, writes: list[WriteField], deletes: list[str]) -> _Plan:
    """Validate the whole request; every error is collected before any file access."""
    errors: list[MetadataValueError] = []
    plan = _Plan()
    seen: set[str] = set()

    def duplicate(key: str) -> bool:
        if key in seen:
            errors.append(MetadataValueError(key, ApiCode.DUPLICATE_KEY, f"{key}: duplicate key in the request"))
            return True
        seen.add(key)
        return False

    reason = ctx.record.read_only_reason
    served = {f.key for f in ctx.record.fields}
    for item in writes:
        if duplicate(item.key):
            continue
        try:
            found = _lookup(ctx, item.key)
        except MetadataValueError as exc:   # e.g. an xattr name that cannot be added
            errors.append(exc.with_key(item.key))
            continue
        if found is None:
            errors.append(MetadataValueError(item.key, ErrorCode.UNKNOWN_FIELD, f"{item.key}: unknown field"))
            continue
        field, lay = found
        if (not field.editable or field.field_type == "binary"
                or (lay == FORMAT and not ctx.record.format_supports_write)):
            suffix = f" ({reason})" if lay == FORMAT and reason else ""
            errors.append(MetadataValueError(item.key, ErrorCode.READ_ONLY, f"{item.key}: field is read-only{suffix}"))
            continue
        try:
            value = _validate_value(ctx.handler_of(lay), field, item.value)
        except MetadataValueError as exc:
            errors.append(exc)
            continue
        plan.writes[lay].append(replace(field, value=value))
        plan.expected[item.key] = value

    for key in deletes:
        if duplicate(key):
            continue
        # Only fields present in the file can be deleted (never addable schemas).
        found = (ctx.fields[key], ctx.layer[key]) if key in served else None
        if found is None:
            errors.append(MetadataValueError(key, ErrorCode.UNKNOWN_FIELD, f"{key}: unknown field"))
            continue
        field, lay = found
        if not field.deletable or (lay == FORMAT and not ctx.record.format_supports_delete):
            suffix = f" ({reason})" if lay == FORMAT and reason else ""
            errors.append(MetadataValueError(key, ErrorCode.NOT_DELETABLE, f"{key}: field cannot be deleted{suffix}"))
            continue
        plan.deletes[lay].append(key)

    if errors:
        raise MetadataValueErrors(errors)
    _add_linked_changes(ctx, plan)
    return plan


def _add_linked_changes(ctx: _Context, plan: _Plan) -> None:
    """Related fields of the format layer kept in step (IPTC dataset <-> XMP twin):
    the extra writes/deletes join the same transaction, so they are verified and
    undone with the rest. A twin value the other side cannot store is skipped with a note."""
    if not plan.writes[FORMAT] and not plan.deletes[FORMAT]:
        return
    handler = ctx.handler_of(FORMAT)
    present = {f.key for f in ctx.record.fields}     # ctx.fields also holds the addable schemas
    served = {k: f for k, f in ctx.fields.items() if k in present and ctx.layer.get(k) == FORMAT}
    requested = {f.key: f.value for f in plan.writes[FORMAT]}
    try:
        extra_writes, extra_deletes = handler.linked_changes(served, requested, list(plan.deletes[FORMAT]))
    except Exception:
        logger.exception("linked changes of %s failed", ctx.path)
        return
    for key, value in extra_writes.items():
        field = served[key]
        try:
            canonical = _validate_value(handler, field, value)
        except MetadataValueError as exc:
            plan.notes.append(f"{key} was not updated to match: {exc.code.replace('_', ' ')}")
            continue
        plan.writes[FORMAT].append(replace(field, value=canonical))
        plan.expected[key] = canonical
        plan.linked.add(key)
        plan.notes.append(f"{key} updated to match")
    for key in extra_deletes:
        if served[key].has_alternatives:
            plan.notes.append(f"{key} was kept: it holds other language alternatives")
            continue
        plan.deletes[FORMAT].append(key)
        plan.linked.add(key)
        plan.notes.append(f"{key} removed to match")


def _call_hook(handler, hook: str, tmp: Path, payload, keys: list[str]) -> Any:
    """Run one mutation hook on the temporary copy; library value errors become 422."""
    try:
        return getattr(handler, hook)(tmp, payload)
    except (MetadataValueError, MetadataValueErrors, PathSecurityError, PermissionError, ApiError):
        raise
    except (NotImplementedError, *_VALUE_EXCEPTIONS) as exc:
        raise _value_error(exc, keys) from exc


def _norm(value: Any) -> str:
    return json.dumps(MetadataField._serialize_value(value), sort_keys=True, ensure_ascii=False, default=str)


def _layer_values(ctx: _Context, tmp: Path, lay: str) -> tuple[dict[str, MetadataField], list[str]]:
    if lay == FS and not ctx.fs_primary:
        record = ctx.fs_handler.read(tmp)
        record.dedupe_keys()
        return {f.key: f for f in record.fields}, []
    record, _fs, layer = _merge_layers(ctx.handler, ctx.fs_handler, tmp)
    fields = {f.key: f for f in record.fields if layer[f.key] == lay}
    return fields, list(record.read_errors) if lay == FORMAT else []


def _is_volatile(lay: str, field: MetadataField) -> bool:
    """Fields that legitimately change with any write (computed, filesystem times/size)."""
    if lay == FS:
        return not field.key.startswith("xattr:")
    return field.computed


def _verify(lay: str, before: dict, after: dict, after_errors: list[str], written: set[str],
            deleted: set[str], expected: dict[str, Any]) -> list[MetadataValueError]:
    """Read-after-write check of one layer (guarantee G4).

    ``written`` keys must be present (with ``expected`` canonical value when
    given), ``deleted`` keys absent, every other non-volatile key unchanged.
    """
    problems: list[MetadataValueError] = []

    def lossy(key, message):
        problems.append(MetadataValueError(key, ErrorCode.LOSSY_WRITE, f"{key}: {message}" if key else message))

    for key in sorted(written):
        if key not in after:
            lossy(key, "field missing after the write")
        elif key in expected and _norm(after[key].value) != _norm(expected[key]):
            lossy(key, f"stored value {_norm(after[key].value)} differs from {_norm(expected[key])}")
    if after_errors:
        lossy(None, "file metadata unreadable after the write: " + "; ".join(after_errors))
    for key in sorted(deleted):
        if key in after:
            lossy(key, "field still present after the delete")
    for key, field in before.items():
        if key in written or key in deleted or _is_volatile(lay, field):
            continue
        if key not in after:
            lossy(key, "field lost by the write")
        elif _norm(after[key].value) != _norm(field.value):
            lossy(key, "field changed by the write")
    for key, field in after.items():
        if key not in before and key not in written and not _is_volatile(lay, field):
            lossy(key, "field unexpectedly added by the write")
    return problems


def _identity(path: Path) -> tuple:
    """Which file ``path`` is (not its content): changes when another file replaces it."""
    st = os.lstat(path)
    return st.st_dev, st.st_ino


def _mtime_ns(path: Path) -> int | None:
    try:
        return os.stat(path).st_mtime_ns
    except OSError:
        return None


def _follow_mtime(path: Path, before: int | None, after: int | None) -> None:
    """Journal entries that expected ``fs:mtime`` to be ``before`` expect ``after``:
    our own rewrite moved it, which is no reason to refuse an older Undo."""
    if before is not None and after is not None:
        UNDO_JOURNAL.update_mtime(path, MTIME_KEY, before, after)


def _snapshot(handler, target: Path, keys: list[str]) -> dict[str, Any]:
    return handler.snapshot(target, keys) if keys else {}


@dataclass
class _Outcome:
    """Result of one committed mutation transaction."""
    pre: dict[str, dict[str, Any]] | None = None   # raw pre-images, None if they could not be taken
    snapshot_error: str | None = None
    patch: Any = None          # byte_patch.BytePatch of the format layer, None if not taken
    changed: bool = False                           # a touched key changed value or presence
    warnings: list[str] = dc_field(default_factory=list)


def _layer_changed(before: dict[str, MetadataField], after: dict[str, MetadataField],
                   written: set[str], deleted: set[str]) -> bool:
    """Value-level change of the touched keys (the raw snapshots may be unavailable)."""
    if any(key in before for key in deleted):
        return True
    return any(key not in before or key not in after or _norm(before[key].value) != _norm(after[key].value)
               for key in written)


def _identical_copy(ctx: _Context, tmp: Path, layers: list[str], pre: dict[str, dict[str, Any]]) -> bool:
    """True when the hooks left the private copy exactly as the target: same bytes,
    same times and every touched key (raw, extended attributes included) as before.
    Then the transaction ends without replacing the file."""
    try:
        post = {lay: _snapshot(ctx.handler_of(lay), tmp, list(pre[lay])) for lay in layers if lay in pre}
        if any(post[lay] != pre[lay] for lay in post):
            return False
        before, after = ctx.path.stat(), tmp.stat()
        return (before.st_size == after.st_size and before.st_mtime_ns == after.st_mtime_ns
                and filecmp.cmp(ctx.path, tmp, shallow=False))
    except Exception:   # when unsure, commit as before
        return False


def _mutate(ctx: _Context, plan: _Plan) -> _Outcome:
    """``_mutate_once``, except that a failure caused only by keys the sync added (e.g. an XMP
    twin that no longer fits its segment) is not the user's error: those keys are dropped
    with a note and the requested changes are saved on their own."""
    try:
        return _mutate_once(ctx, plan)
    except ApiError as exc:
        failed = {e.get("key") for e in exc.errors}
        if exc.status != 422 or not failed or not failed <= plan.linked:
            raise
    plan.writes[FORMAT] = [f for f in plan.writes[FORMAT] if f.key not in failed]
    plan.deletes[FORMAT] = [k for k in plan.deletes[FORMAT] if k not in failed]
    for key in failed:
        plan.expected.pop(key, None)
        plan.notes = [n for n in plan.notes if not n.startswith(f"{key} ")]
        plan.notes.append(f"{key} was not updated to match: the value could not be stored")
    return _mutate_once(ctx, plan)


def _mutate_once(ctx: _Context, plan: _Plan) -> _Outcome:
    """ONE transaction: hooks on the temporary copy, verification, replace.

    The outcome carries the raw pre-images ``{layer: {key: raw | ABSENT}}`` of
    the touched keys, taken on the private copy before the hooks run. A failing
    snapshot never blocks the save (the user asked for it): it is logged and the
    save reports ``undo_snapshot_failed``.
    """
    layers = [lay for lay in (FORMAT, FS) if plan.writes[lay] or plan.deletes[lay]]
    if ctx.fs_primary:
        layers = [FS] if layers else []
    outcome = _Outcome()

    def mutate(tmp: Path) -> None:
        before = {lay: _layer_values(ctx, tmp, lay)[0] for lay in layers}
        try:
            outcome.pre = {lay: _snapshot(ctx.handler_of(lay), tmp, plan.keys(lay)) for lay in layers}
        except Exception as exc:
            logger.error("Undo pre-image of %s %s failed", ctx.path, plan.keys(FORMAT) + plan.keys(FS),
                         exc_info=(type(exc), exc, exc.__traceback__))
            outcome.pre, outcome.snapshot_error = None, describe_error(exc)
        # Format layer first: its rewrite changes the mtime written by the fs layer.
        for lay in (FORMAT, FS):
            handler = ctx.handler_of(lay)
            if plan.deletes[lay]:
                _call_hook(handler, "apply_delete", tmp, plan.deletes[lay], plan.deletes[lay])
            if plan.writes[lay]:
                _call_hook(handler, "apply_write", tmp, plan.writes[lay], [f.key for f in plan.writes[lay]])
        problems: list[MetadataValueError] = []
        changed = False
        for lay in layers:
            after, after_errors = _layer_values(ctx, tmp, lay)
            written, deleted = {f.key for f in plan.writes[lay]}, set(plan.deletes[lay])
            problems += _verify(lay, before[lay], after, after_errors, written, deleted, plan.expected)
            changed = changed or _layer_changed(before[lay], after, written, deleted)
        if problems:
            raise MetadataValueErrors(problems)
        outcome.changed = changed
        if not changed and outcome.pre is not None and _identical_copy(ctx, tmp, layers, outcome.pre):
            raise NothingToCommit()
        if changed and not ctx.fs_primary and FORMAT in layers and outcome.pre is not None:
            outcome.patch = make_patch(ctx.path, tmp)

    try:
        outcome.warnings = [*plan.notes, *(secure_atomic_write(ctx.path, mutate) or [])]
    except Exception as exc:
        raise _transaction_error(exc) from exc
    return outcome


# ──────────────────────────────── Undo journal ───────────────────────────────

UNDO_JOURNAL = UndoJournal()
# Serializes the API's own mutations (save + journal update, Undo): the journal
# identity bookkeeping assumes one of our transactions at a time.
_MUTATION_LOCK = RLock()


def _size(n: int) -> str:
    """Human size for messages: 512 bytes, 4.9 KiB, 70 MiB, 64.5 MiB."""
    if n < 1024:
        return f"{n} bytes"
    unit, value = ("KiB", n / 1024) if n < 1024 ** 2 else ("MiB", n / 1024 ** 2)
    return f"{value:.1f}".removesuffix(".0") + f" {unit}"


def _unavailable(code: str, message: str) -> dict[str, str]:
    return {"code": code, "message": message}


def _record_undo(ctx: _Context, outcome: _Outcome, identity: tuple) -> tuple[str | None, dict | None]:
    """Journal the save just committed: ``(undo_token, undo_unavailable)``.

    ``(token, None)`` for an undoable change, ``(None, None)`` for a no-op,
    ``(None, {code, message})`` when something changed but cannot be undone.
    """
    if outcome.pre is None:
        if not outcome.changed:
            return None, None
        return None, _unavailable(UndoCode.SNAPSHOT_FAILED,
                                  "This change was saved but cannot be undone: the original values could "
                                  f"not be captured ({outcome.snapshot_error})")
    try:
        post = {lay: _snapshot(ctx.handler_of(lay), ctx.path, list(snap)) for lay, snap in outcome.pre.items()}
    except Exception as exc:
        logger.error("Undo post-image of %s failed", ctx.path, exc_info=(type(exc), exc, exc.__traceback__))
        if not outcome.changed:
            return None, None
        return None, _unavailable(UndoCode.SNAPSHOT_FAILED,
                                  "This change was saved but cannot be undone: the saved values could "
                                  f"not be captured ({describe_error(exc)})")
    changed_pre: dict[str, dict[str, Any]] = {}
    changed_post: dict[str, dict[str, Any]] = {}
    for lay, snap in outcome.pre.items():
        keys = [k for k in snap if k not in post[lay] or post[lay][k] != snap[k]]
        if keys:
            changed_pre[lay] = {k: snap[k] for k in keys}
            changed_post[lay] = {k: post[lay][k] for k in keys}
    if not changed_pre:
        return None, None   # no-op save: nothing to undo
    try:
        return UNDO_JOURNAL.add_entry(ctx.path, ctx.handler.NAME, identity, changed_pre, changed_post,
                                      outcome.patch if FORMAT in changed_pre else None), None
    except UndoTooLargeError as exc:
        return None, _unavailable(UndoCode.TOO_LARGE,
                                  f"This change was saved but is too large to be undone "
                                  f"({_size(exc.size)} > {_size(exc.limit)} limit)")


def _save(path_str: str, writes: list[WriteField], deletes: list[str]) -> dict:
    path = _valid_file(path_str)
    with _MUTATION_LOCK, PATH_LOCKS.writing(path):
        ctx = stable_read(path, lambda: _read_full_record(path))
        if ctx.not_writable:
            # Nothing can be saved: one clear error instead of N "read-only field" ones.
            raise ApiError.one(403, ErrorCode.NOT_WRITABLE, ctx.not_writable)
        try:
            plan = _plan(ctx, writes, deletes)
        except MetadataValueErrors as exc:
            raise ApiError(422, exc.to_dicts()) from exc
        except ApiError:
            raise
        except Exception as exc:   # a handler bug while validating (e.g. TypeError)
            raise _internal(exc, "Validating the request failed") from exc
        try:
            before = _identity(ctx.path)
        except OSError as exc:
            raise ApiError.one(409, ErrorCode.CONCURRENT_MODIFICATION, f"File changed: {exc}") from exc
        mtime_before = _mtime_ns(ctx.path)
        outcome = _mutate(ctx, plan)
        _follow_mtime(ctx.path, mtime_before, _mtime_ns(ctx.path))
        try:
            after = _identity(ctx.path)
        except OSError as exc:
            logger.warning("%s could not be found again after the save", ctx.path, exc_info=True)
            unavailable = None if not outcome.changed else _unavailable(
                UndoCode.SNAPSHOT_FAILED, f"This change was saved but cannot be undone: the file could not be "
                f"found again after the save ({describe_error(exc)})")
            return _saved(None, unavailable, outcome.warnings)
        UNDO_JOURNAL.update_identity(ctx.path, before, after)
        token, unavailable = _record_undo(ctx, outcome, after)
    return _saved(token, unavailable, outcome.warnings)


def _saved(token: str | None, unavailable: dict | None, warnings: list[str]) -> dict:
    """Success body of /write and /delete (see the module docstring)."""
    return {"ok": True, "errors": [], "undo_token": token, "undo_unavailable": unavailable,
            "warnings": list(warnings)}


@router.post("/write")
def write_metadata(req: WriteRequest):
    return _save(req.path, req.fields, req.delete)


@router.post("/delete")
def delete_metadata(req: DeleteRequest):
    return _save(req.path, [], req.keys)


# ──────────────────────────────── /undo ──────────────────────────────────────

class UndoRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")   # the path comes from the journal only
    token: str = Field(min_length=1, max_length=256)


def _conflict(message: str, keys: list[str] | None = None) -> ApiError:
    return ApiError(409, [error_entry(ErrorCode.CONCURRENT_MODIFICATION, k, f"{k}: {message}" if k else message)
                          for k in (keys or [None])])


def _undo_context(entry) -> _Context:
    """Context of the journaled file, or 409 when it is no longer the same file."""
    try:
        path = validate_file_path(str(entry.path), must_exist=True)
        identity = _identity(path)
    except (PathSecurityError, OSError) as exc:
        raise _conflict(f"the file is no longer available: {exc}") from exc
    if path != entry.path or identity != entry.identity:
        raise _conflict("the file was replaced since the operation")
    try:
        ctx = stable_read(path, lambda: _read_full_record(path))
    except ApiError as exc:
        if exc.status != 422:   # busy / vanished: not a conflict, the entry stays retryable
            raise
        raise _conflict("no handler for the file any more") from exc
    if ctx.handler.NAME != entry.handler_name:
        raise _conflict("the file is now read by another handler")
    return ctx


def _undo_transaction(ctx: _Context, entry) -> list[str]:
    layers = [lay for lay in (FORMAT, FS) if entry.pre.get(lay)]
    if ctx.fs_primary and any(lay != FS for lay in layers):
        raise _conflict("the file format changed since the operation")

    def mutate(tmp: Path) -> None:
        # 1. Every touched key must still hold its post-value (else 409, nothing touched).
        conflicts: list[str] = []
        for lay in layers:
            keys = list(entry.post[lay])
            current = _call_hook(ctx.handler_of(lay), "snapshot", tmp, keys, keys)
            conflicts += [k for k in keys if k not in current or current[k] != entry.post[lay][k]]
        if conflicts:
            raise _conflict("modified since the operation; Undo refused", conflicts)
        before = {lay: _layer_values(ctx, tmp, lay)[0] for lay in layers}

        def attempt(use_patch: bool) -> list[MetadataValueError]:
            # 2. Restore, format layer first (its rewrite changes the mtime restored by fs).
            #    With a byte patch the format layer gets its original bytes back; the
            #    verification below still proves the values.
            for lay in layers:
                if use_patch and lay == FORMAT:
                    continue
                snap = entry.pre[lay]
                _call_hook(ctx.handler_of(lay), "restore", tmp, snap, list(snap))
            # 3. Read-after-write verification (G4): exact pre-images, nothing else changed.
            problems: list[MetadataValueError] = []
            for lay in layers:
                snap = entry.pre[lay]
                restored = _call_hook(ctx.handler_of(lay), "snapshot", tmp, list(snap), list(snap))
                for key in snap:
                    if key not in restored or restored[key] != snap[key]:
                        problems.append(MetadataValueError(key, ErrorCode.LOSSY_WRITE,
                                                           f"{key}: the original value could not be restored exactly"))
                after, after_errors = _layer_values(ctx, tmp, lay)
                present = {k for k, raw in snap.items() if raw is not ABSENT}
                absent = {k for k, raw in snap.items() if raw is ABSENT}
                # Keys of the entry are checked exactly above; here: everything else.
                problems += [p for p in _verify(lay, before[lay], after, after_errors, present, absent, {})
                             if p.key not in snap]
            return problems

        backup = None
        if entry.patch is not None and FORMAT in layers and not ctx.fs_primary:
            backup = tmp.with_name(tmp.name + ".prepatch")
            shutil.copyfile(tmp, backup)
            try:
                patched = apply_patch(tmp, entry.patch)
            except OSError as exc:   # possibly half written: start again from the untouched copy
                logger.warning("Byte patch of %s failed (%s): value-level restore", ctx.path, exc)
                shutil.copyfile(backup, tmp)
                patched = False
            if not patched:
                backup = None        # the file is not the saved one any more: value-level restore
        problems = attempt(backup is not None)
        if problems and backup is not None:
            shutil.copyfile(backup, tmp)         # the bytes did not verify: value-level restore
            problems = attempt(False)
        if problems:
            raise MetadataValueErrors(problems)

    try:
        return list(secure_atomic_write(ctx.path, mutate) or [])
    except Exception as exc:
        raise _transaction_error(exc) from exc


@router.post("/undo")
def undo(req: UndoRequest):
    with _MUTATION_LOCK:
        entry = UNDO_JOURNAL.get(req.token)
        if entry is None:
            raise ApiError.one(410, ErrorCode.UNDO_EXPIRED,
                               "Undo is no longer available (unknown, already used or expired token)")
        with PATH_LOCKS.writing(entry.path):
            ctx = _undo_context(entry)
            mtime_before = _mtime_ns(entry.path)
            warnings = _undo_transaction(ctx, entry)
            UNDO_JOURNAL.pop(entry.token)
            _follow_mtime(entry.path, mtime_before, _mtime_ns(entry.path))
            try:
                UNDO_JOURNAL.update_identity(entry.path, entry.identity, _identity(entry.path))
            except OSError as exc:
                # The undo itself succeeded; the journal just keeps the older identity.
                logger.debug("undo identity of %s not refreshed: %s", entry.path, exc)
    return {"ok": True, "errors": [], "keys": entry.keys, "undo_token": None, "warnings": warnings}


# ──────────────────────────────── /diff ──────────────────────────────────────

# Filesystem keys excluded from /diff: they describe the file instance, not its
# content, and differ for any two reads/copies (fs:atime is changed by reading
# the file; fs:ctime is the creation time on Windows and the inode-change time
# on POSIX). fs:mtime, fs:size, attributes, permissions and xattrs are compared.
DIFF_IGNORED_KEYS = frozenset({"fs:atime", "fs:ctime"})


@router.get("/diff")
def diff_files(
    a: str = Query(..., description="Absolute path to file A"),
    b: str = Query(..., description="Absolute path to file B"),
):
    # Same record as /read, filesystem layer included (D9), minus DIFF_IGNORED_KEYS.
    path_a, path_b = _valid_file(a), _valid_file(b)
    with PATH_LOCKS.reading(path_a, path_b):
        record_a = stable_read(path_a, lambda: _read_full_record(path_a)).record
        record_b = stable_read(path_b, lambda: _read_full_record(path_b)).record
    for record in (record_a, record_b):
        record.fields = [f for f in record.fields if f.key not in DIFF_IGNORED_KEYS]
    return compute_diff(record_a, record_b).to_dict()
