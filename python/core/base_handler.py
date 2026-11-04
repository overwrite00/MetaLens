from __future__ import annotations
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any
from core.path_security import secure_atomic_write

from core.models import MetadataField, MetadataRecord
from core import values


class _Absent:
    """Snapshot marker: the key did not exist (restore removes it). Singleton."""
    _instance: "_Absent | None" = None

    def __new__(cls) -> "_Absent":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "ABSENT"

    def __bool__(self) -> bool:
        return False

    def __copy__(self) -> "_Absent":
        return self

    def __deepcopy__(self, memo) -> "_Absent":
        return self

    def __reduce__(self):
        return (_Absent, ())


ABSENT = _Absent()

# key -> raw pre-image (handler-specific, comparable with ==) or ABSENT.
Snapshot = dict[str, Any]


class BaseMetadataHandler(ABC):
    """Base class of every metadata handler.

    Mutation contract (plan B1/B2/B5). The API runs ONE transaction
    (``secure_atomic_write``) per request and calls the hooks on the private
    temporary copy, never nesting another transaction:

        snap = handler.snapshot(tmp, keys)       # raw pre-image, for exact Undo
        handler.apply_write(tmp, fields)         # and/or apply_delete(tmp, keys)
        ... read-after-write verification ...
        handler.restore(tmp, snap)               # Undo, in a later transaction

    ``write``/``delete`` stay as public wrappers that open their own transaction.
    The default hooks raise ``NotImplementedError``: a handler that exposes
    editable/deletable fields must override them (enforced by ``test_contract``).
    """
    NAME: str = ""
    EXTENSIONS: frozenset[str] = frozenset()
    PRIORITY: int = 50  # lower = higher priority; hachoir fallback = 100

    MUTATION_HOOKS: tuple[str, ...] = (
        "writable_fields", "apply_write", "apply_delete", "snapshot", "restore", "canonical",
    )

    @abstractmethod
    def read(self, path: Path) -> MetadataRecord:
        """Read all metadata. Never raises — errors go to record.read_errors."""

    @abstractmethod
    def write(self, path: Path, fields: list[MetadataField]) -> None:
        """Atomically write changed fields back to file."""

    @abstractmethod
    def delete(self, path: Path, keys: list[str]) -> None:
        """Delete metadata keys from file."""

    def can_handle(self, path: Path) -> bool:
        return path.suffix.lower() in self.EXTENSIONS

    def content_digest(self, path: Path) -> str | None:
        """Digest of the media data of ``path`` (the picture, the audio, the video), ``None`` when
        the handler cannot tell. A metadata write must never change it: the save is refused otherwise (G5)."""
        return None

    def writable_field(self, path: Path, key: str) -> MetadataField | None:
        """Server-owned schema for known writable fields absent from a file."""
        return None

    # ──────────────────────── mutation hooks ────────────────────────────────

    def writable_fields(self, path: Path, record: MetadataRecord) -> list[MetadataField]:
        """Schema of the fields that can be ADDED to this file (absent from ``record``).

        Each returned field is editable, non-binary, with the ``field_type``/``multi``/
        ``length``/``choices`` its value must follow. Default: none.
        """
        return []

    def linked_changes(self, fields: dict[str, MetadataField], writes: dict[str, Any],
                       deletes: list[str]) -> tuple[dict[str, Any], list[str]]:
        """Changes that keep related fields of the same file in step (e.g. an IPTC
        dataset and its XMP twin): given the served ``fields`` of this layer and the
        requested ``writes`` ({key: value}) and ``deletes``, return the extra
        ``(writes, deletes)`` to save in the same transaction. Only keys present in
        ``fields`` and absent from the request may be returned. Default: none.
        """
        return {}, []

    def apply_write(self, tmp: Path, fields: list[MetadataField]) -> None:
        """Write ``fields`` (schema of the field + new value) into ``tmp`` in place.

        Called inside the caller's transaction: must not open another one. Every
        value must be validated with ``canonical`` first; value errors (including
        library exceptions) are raised as ``core.errors.MetadataValueError(key)``.
        """
        raise NotImplementedError(f"{self.NAME or type(self).__name__} does not implement apply_write")

    def apply_delete(self, tmp: Path, keys: list[str]) -> None:
        """Remove ``keys`` from ``tmp`` in place (same rules as ``apply_write``)."""
        raise NotImplementedError(f"{self.NAME or type(self).__name__} does not implement apply_delete")

    def snapshot(self, tmp: Path, keys: list[str]) -> Snapshot:
        """Raw pre-image of each key: ``{key: raw}`` or ``{key: ABSENT}``.

        Read-only, works on any path. ``raw`` is whatever ``restore`` needs to
        rebuild the key exactly (bytes, frames, attribute objects...), must compare
        with ``==`` and survive ``copy.deepcopy``. ``restore`` followed by
        ``snapshot`` must return an equal snapshot (guarantee G3).
        """
        raise NotImplementedError(f"{self.NAME or type(self).__name__} does not implement snapshot")

    def restore(self, tmp: Path, snap: Snapshot) -> None:
        """Put every key of ``snap`` back exactly (``ABSENT`` = remove it), in place."""
        raise NotImplementedError(f"{self.NAME or type(self).__name__} does not implement restore")

    def canonical(self, field: MetadataField, value: Any) -> Any:
        """Validate ``value`` for ``field`` and return its canonical JSON form.

        Default: the generic rules of ``core.values.canonicalize`` (type, multi,
        length, choices). Override to add format limits (e.g. reject '' where the
        format cannot store it, ranges, maximum sizes), raising
        ``MetadataValueError(field.key, code)``.
        """
        return values.canonicalize(field, value)

    def overrides(self, hook: str) -> bool:
        """True when this handler's class overrides the default of ``hook``."""
        if hook not in self.MUTATION_HOOKS:
            raise ValueError(f"Unknown mutation hook: {hook}")
        return getattr(type(self), hook) is not getattr(BaseMetadataHandler, hook)

    # ──────────────────────── helpers ───────────────────────────────────────

    def _atomic_write(self, path: Path, write_fn) -> None:
        """Execute write_fn on a temp copy, then atomically replace original."""
        secure_atomic_write(path, write_fn)

    def _make_record(self, path: Path, **kwargs) -> MetadataRecord:
        stat = path.stat()
        return MetadataRecord(
            file_path=path,
            file_size=stat.st_size,
            mtime=stat.st_mtime,
            handler_name=self.NAME,
            **kwargs,
        )
