import importlib
import time
from pathlib import Path

import pytest
from core.registry import HandlerRegistry
from core.base_handler import BaseMetadataHandler
from core.models import MetadataField, MetadataRecord

# Trigger handler registration as an explicit side effect.
importlib.import_module("core.handlers")


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
    assert handler.NAME == "hachoir"


@pytest.mark.parametrize("name", ["unknown.xyz", "noextension", "data.bin", "notes.txt", "photo.jpg",
                                  "song.mp3", "a.pdf"])
def test_filesystem_layer_is_never_the_primary_handler(name):
    assert HandlerRegistry.get(Path(name)).NAME != "filesystem"
    assert [h.NAME for h in HandlerRegistry.layers(Path(name))] == ["filesystem"]


def test_extensionless_files_fall_back_to_hachoir():
    assert HandlerRegistry.get(Path("README")).NAME == "hachoir"


def test_filesystem_layer_accessor():
    from core.handlers.filesystem_handler import FilesystemHandler
    layer = HandlerRegistry.filesystem_layer()
    assert isinstance(layer, FilesystemHandler)
    assert layer in HandlerRegistry.all_handlers()
    assert HandlerRegistry.is_primary(layer) is False
    assert HandlerRegistry.is_primary(HandlerRegistry.get(Path("x.jpg"))) is True


def test_get_all_lists_the_primary_handler_before_layers():
    names = [h.NAME for h in HandlerRegistry.get_all(Path("unknown.xyz"))]
    assert names[0] == "hachoir" and names[-1] == "filesystem"


def test_directories_have_no_format_handler(tmp_path):
    with pytest.raises(ValueError):
        HandlerRegistry.get(tmp_path)


def _wav(path: Path) -> Path:
    import wave
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(bytes(16000))
    return path


def test_wav_renamed_bin_is_read_by_hachoir_from_content(tmp_path):
    path = _wav(tmp_path / "tone.bin")
    handler = HandlerRegistry.get(path)
    assert handler.NAME == "hachoir"
    record = handler.read(path)
    assert record.read_errors == []
    assert any(f.key.startswith("hachoir:") for f in record.fields)


def test_jpeg_without_extension_is_read_by_hachoir_from_content(tmp_path):
    from PIL import Image
    path = tmp_path / "photo"
    Image.new("RGB", (8, 6)).save(path, "JPEG")
    handler = HandlerRegistry.get(path)
    assert handler.NAME == "hachoir"
    record = handler.read(path)
    assert record.read_errors == []
    assert any(f.key.startswith("hachoir:") for f in record.fields)


@pytest.mark.asyncio
async def test_api_list_reports_hachoir_for_unknown_files(client, tmp_path):
    (tmp_path / "noise.xyz").write_bytes(b"\x13\x37" * 32)
    (tmp_path / "noextension").write_bytes(b"\x13\x37" * 32)
    (tmp_path / "sub").mkdir()
    resp = await client.get("/list", params={"path": str(tmp_path)})
    assert resp.status_code == 200, resp.text
    handlers = {item["name"]: item["handler"] for item in resp.json()["items"]}
    assert handlers["noise.xyz"] == "hachoir"
    assert handlers["noextension"] == "hachoir"
    assert handlers["sub"] != "filesystem"


@pytest.mark.asyncio
async def test_api_read_unknown_file_has_hachoir_and_filesystem_fields(client, tmp_path):
    path = _wav(tmp_path / "tone.bin")
    resp = await client.get("/read", params={"path": str(path)})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["handler_name"] == "hachoir"
    keys = {f["key"] for f in body["fields"]}
    assert any(k.startswith("hachoir:") for k in keys)
    assert {"fs:size", "fs:mtime"} <= keys


@pytest.mark.asyncio
async def test_api_read_large_sparse_unknown_file_is_fast(client, tmp_path):
    path = tmp_path / "sparse.bin"
    with path.open("wb") as stream:
        stream.truncate(50 * 1024 * 1024)
    start = time.perf_counter()
    resp = await client.get("/read", params={"path": str(path)})
    elapsed = time.perf_counter() - start
    assert resp.status_code == 200, resp.text
    assert resp.json()["handler_name"] == "hachoir"
    assert elapsed < 2.0, f"/read took {elapsed:.2f} s"


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
