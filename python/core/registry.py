from __future__ import annotations
from pathlib import Path

from core.base_handler import BaseMetadataHandler


class HandlerRegistry:
    _handlers: list[BaseMetadataHandler] = []

    @classmethod
    def register(cls, handler: BaseMetadataHandler) -> None:
        cls._handlers.append(handler)
        cls._handlers.sort(key=lambda h: h.PRIORITY)

    @classmethod
    def get(cls, path: Path) -> BaseMetadataHandler:
        for h in cls._handlers:
            if h.can_handle(path):
                return h
        raise ValueError(f"No handler registered for extension '{path.suffix}'")

    @classmethod
    def get_all(cls, path: Path) -> list[BaseMetadataHandler]:
        return [h for h in cls._handlers if h.can_handle(path)]

    @classmethod
    def all_handlers(cls) -> list[BaseMetadataHandler]:
        return list(cls._handlers)

    @classmethod
    def clear(cls) -> None:
        """Used in tests to reset state."""
        cls._handlers.clear()
