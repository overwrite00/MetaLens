"""Typed errors of the metadata contract (plan B4).

Every error carries a machine-readable ``code``, an optional metadata ``key``,
a human ``message`` and the HTTP status the API maps it to. ``to_dict()`` gives
one entry of the uniform error body ``{"ok": false, "errors": [...], "detail": str}``.

* ``MetadataValueError`` is a ``ValueError``: any value-related failure (parsing,
  encoding, library errors such as ``struct.error`` or a mutagen error) must be
  re-raised as one, so user input never produces a 500.
* ``TargetNotWritableError`` / ``ConcurrentModificationError`` are
  ``PathSecurityError`` subclasses, so code that already catches the transaction
  errors of ``secure_atomic_write`` keeps working while the API can tell 403 from 409.
"""
from __future__ import annotations

__all__ = [
    "PathSecurityError",
    "ErrorCode",
    "MetadataValueError",
    "MetadataValueErrors",
    "TargetNotWritableError",
    "ConcurrentModificationError",
    "UndoExpiredError",
    "error_entry",
]


class PathSecurityError(Exception):
    """Raised when path validation or a safe write fails (base of the transaction errors)."""


class ErrorCode:
    """Stable error codes (string constants, part of the API)."""
    # 422 — validation of one field
    INVALID_VALUE = "invalid_value"          # unparsable / malformed value
    WRONG_TYPE = "wrong_type"                # e.g. a string where a list is required
    NULL_VALUE = "null_value"                # null is never a valid value
    EMPTY_VALUE = "empty_value"              # '' / [] where the format cannot store it
    OUT_OF_RANGE = "out_of_range"
    INVALID_CHOICE = "invalid_choice"        # enum value not among choices
    WRONG_LENGTH = "wrong_length"            # fixed-length list of the wrong size
    MISSING_OFFSET = "missing_offset"        # datetime without UTC offset
    UNEXPECTED_OFFSET = "unexpected_offset"  # localdatetime with an offset
    TOO_LARGE = "too_large"                  # exceeds a format limit (e.g. 64 KiB segment)
    UNSUPPORTED = "unsupported"              # field type / encoding the handler cannot write
    READ_ONLY = "read_only"                  # field not editable
    NOT_DELETABLE = "not_deletable"
    UNKNOWN_FIELD = "unknown_field"
    LOSSY_WRITE = "lossy_write"              # read-after-write verification failed (G4)
    # transaction / API
    NOT_WRITABLE = "not_writable"                        # 403
    CONCURRENT_MODIFICATION = "concurrent_modification"  # 409
    UNDO_EXPIRED = "undo_expired"                        # 410
    FILE_BUSY = "file_busy"                              # 503 locked/changing because of another program (retry)
    FILE_NOT_FOUND = "file_not_found"                    # 404 the file/folder vanished during the request


def error_entry(code: str, key: str | None, message: str) -> dict:
    """One element of the ``errors`` array of the uniform error body."""
    return {"code": code, "key": key, "message": message}


class MetadataValueError(ValueError):
    """An invalid value for one metadata key (HTTP 422)."""
    http_status = 422

    def __init__(self, key: str | None, code: str = ErrorCode.INVALID_VALUE,
                 message: str | None = None) -> None:
        self.key = key
        self.code = code
        self.message = message or (f"{key}: {code.replace('_', ' ')}" if key else code.replace("_", " "))
        super().__init__(self.message)

    def with_key(self, key: str) -> "MetadataValueError":
        """Same error attributed to ``key`` (low-level parsers raise with ``key=None``)."""
        if self.key == key:
            return self
        base = self.message
        if self.key and base.startswith(f"{self.key}: "):
            base = base[len(self.key) + 2:]
        err = MetadataValueError(key, self.code, f"{key}: {base}")
        err.__cause__ = self.__cause__ or self
        return err

    def to_dict(self) -> dict:
        return error_entry(self.code, self.key, self.message)


class MetadataValueErrors(ValueError):
    """All validation errors of one request, collected before touching the file (422)."""
    http_status = 422

    def __init__(self, errors: list[MetadataValueError]) -> None:
        if not errors:
            raise ValueError("MetadataValueErrors needs at least one error")
        self.errors = list(errors)
        super().__init__("; ".join(e.message for e in self.errors))

    def to_dicts(self) -> list[dict]:
        return [e.to_dict() for e in self.errors]


class TargetNotWritableError(PathSecurityError):
    """The target file cannot be written (read-only attribute, permissions): HTTP 403."""
    http_status = 403
    code = ErrorCode.NOT_WRITABLE
    key = None

    def to_dict(self) -> dict:
        return error_entry(self.code, None, str(self))


class ConcurrentModificationError(PathSecurityError):
    """The file changed during the transaction or since the Undo entry: HTTP 409."""
    http_status = 409
    code = ErrorCode.CONCURRENT_MODIFICATION
    key = None

    def to_dict(self) -> dict:
        return error_entry(self.code, None, str(self))


class UndoExpiredError(LookupError):
    """Unknown or evicted undo token: HTTP 410."""
    http_status = 410
    code = ErrorCode.UNDO_EXPIRED
    key = None

    def to_dict(self) -> dict:
        return error_entry(self.code, None, str(self))
