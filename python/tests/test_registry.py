import pytest
from pathlib import Path
from core.registry import HandlerRegistry
from core.base_handler import BaseMetadataHandler
from core.models import MetadataField, MetadataRecord
import core.handlers  # noqa: F401 — registers all handlers


class MockHandler(BaseMetadataHandler):
    NAME = "mock"
    EXTENSIONS = frozenset({".fakeext"})
    PRIORITY = 5

    def read(self, path: Path) -> MetadataRecord:
        return self._make_record(path)

    def write(self, path: Path, fields: list[MetadataField]) -> None:
        pass

    def delete(self, path: Path, keys: list[str]) -> None:
        pass


def test_all_handlers_registered():
    names = [h.NAME for h in HandlerRegistry.all_handlers()]
    for expected in ["filesystem", "image", "audio", "video", "pdf", "office", "ole", "hachoir"]:
        assert expected in names, f"Handler '{expected}' not registered"


def test_get_handler_by_extension():
    handler = HandlerRegistry.get(Path("photo.jpg"))
    assert handler.NAME == "image"

    handler = HandlerRegistry.get(Path("song.mp3"))
    assert handler.NAME == "audio"

    handler = HandlerRegistry.get(Path("document.pdf"))
    assert handler.NAME == "pdf"

    handler = HandlerRegistry.get(Path("spreadsheet.xlsx"))
    assert handler.NAME == "office"


def test_hachoir_fallback_for_unknown():
    handler = HandlerRegistry.get(Path("unknown.xyz"))
    assert handler.NAME in ("hachoir", "filesystem")


def test_priority_ordering():
    handlers = HandlerRegistry.all_handlers()
    priorities = [h.PRIORITY for h in handlers]
    assert priorities == sorted(priorities)


def test_register_custom_handler():
    mock = MockHandler()
    HandlerRegistry.register(mock)
    result = HandlerRegistry.get(Path("test.fakeext"))
    assert result.NAME == "mock"
    # cleanup
    HandlerRegistry._handlers.remove(mock)
