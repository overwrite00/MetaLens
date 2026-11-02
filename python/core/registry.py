"""Handler registry: one primary (format) handler per file plus metadata layers.

* Primary handlers own the format metadata of a file. ``get(path)`` returns the
  first one, by ``PRIORITY``, whose ``can_handle`` accepts the path. ``hachoir``
  (``PRIORITY = 100``, accepts everything) is the catch-all for unknown
  extensions and extension-less files: it recognizes the format from the
  content (D3).
* Layers (handlers with ``PRIMARY = False``, i.e. the filesystem layer) are never
  returned by ``get``. They apply to every file and the API adds their fields
  to each record: ``layers(path)`` / ``filesystem_layer()``.
"""
from __future__ import annotations
from pathlib import Path

from core.base_handler import BaseMetadataHandler


class HandlerRegistry:
    _handlers: list[BaseMetadataHandler] = []

    @classmethod
    def register(cls, handler: BaseMetadataHandler) -> None:
        cls._handlers.append(handler)
        cls._handlers.sort(key=lambda h: h.PRIORITY)

    @staticmethod
    def is_primary(handler: BaseMetadataHandler) -> bool:
        """False for layer handlers (``PRIMARY = False``), True otherwise."""
        return getattr(handler, "PRIMARY", True)

    @classmethod
    def get(cls, path: Path) -> BaseMetadataHandler:
        """Primary (format) handler of ``path``. Directories have none (ValueError)."""
        path = Path(path)
        if path.is_dir():
            raise ValueError(f"No format handler for a directory: {path}")
        for h in cls._handlers:
            if cls.is_primary(h) and h.can_handle(path):
                return h
        raise ValueError(f"No handler registered for extension '{path.suffix}'")

    @classmethod
    def layers(cls, path: Path) -> list[BaseMetadataHandler]:
        """Layer handlers that apply to ``path`` (priority order)."""
        return [h for h in cls._handlers if not cls.is_primary(h) and h.can_handle(Path(path))]

    @classmethod
    def layer(cls, name: str) -> BaseMetadataHandler:
        """The registered layer handler called ``name`` (KeyError if none)."""
        for h in cls._handlers:
            if not cls.is_primary(h) and h.NAME == name:
                return h
        raise KeyError(f"No layer handler named {name!r}")

    @classmethod
    def filesystem_layer(cls) -> BaseMetadataHandler:
        """The filesystem layer (``fs:*`` / ``xattr:*`` keys) shared by every file."""
        return cls.layer("filesystem")

    @classmethod
    def get_all(cls, path: Path) -> list[BaseMetadataHandler]:
        """Every handler accepting ``path``: primary candidates first, then layers."""
        path = Path(path)
        matching = [h for h in cls._handlers if h.can_handle(path)]
        return [h for h in matching if cls.is_primary(h)] + [h for h in matching if not cls.is_primary(h)]

    @classmethod
    def all_handlers(cls) -> list[BaseMetadataHandler]:
        return list(cls._handlers)

    @classmethod
    def clear(cls) -> None:
        """Used in tests to reset state."""
        cls._handlers.clear()
