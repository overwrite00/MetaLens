from __future__ import annotations
from abc import ABC, abstractmethod
from pathlib import Path
from core.path_security import secure_atomic_write

from core.models import MetadataField, MetadataRecord


class BaseMetadataHandler(ABC):
    NAME: str = ""
    EXTENSIONS: frozenset[str] = frozenset()
    PRIORITY: int = 50  # lower = higher priority; hachoir fallback = 100

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

    def writable_field(self, path: Path, key: str) -> MetadataField | None:
        """Server-owned schema for known writable fields absent from a file."""
        return None

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
