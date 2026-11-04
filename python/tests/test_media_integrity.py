"""G5: a metadata write never changes the media data of the file."""
import itertools

import pytest

from core.handlers.image_handler import ImageHandler
from core.registry import HandlerRegistry
from tests.factories import by_name


def _build(tmp_path, name):
    folder = tmp_path / name
    folder.mkdir()
    return by_name(name).build(folder)


def test_jpeg_digest_ignores_metadata_but_not_the_picture(tmp_path):
    handler = HandlerRegistry.get(_build(tmp_path, "jpeg_camera"))
    first = _build(tmp_path, "jpeg_iptc")
    second = _build(tmp_path, "jpeg_xmp_first")
    assert handler.content_digest(first) == handler.content_digest(second)     # same picture, other metadata
    damaged = tmp_path / "damaged.jpg"
    data = bytearray(first.read_bytes())
    data[-10] ^= 0xFF
    damaged.write_bytes(bytes(data))
    assert handler.content_digest(damaged) != handler.content_digest(first)


def test_png_and_webp_digests_ignore_text_and_metadata(tmp_path):
    handler = ImageHandler()
    png = _build(tmp_path, "png_text_exif")
    data = png.read_bytes()
    other = tmp_path / "other.png"
    cut = data.index(b"tEXt") - 4
    other.write_bytes(data[:cut] + data[cut + 12 + int.from_bytes(data[cut:cut + 4], "big"):])
    assert handler.content_digest(png) == handler.content_digest(other)
    assert handler.content_digest(_build(tmp_path, "webp_exif"))


def test_unknown_formats_are_not_checked(tmp_path):
    assert ImageHandler().content_digest(_build(tmp_path, "bmp")) is None
    assert ImageHandler().content_digest(_build(tmp_path, "gif_comment")) is None


def test_tiff_digest_follows_the_strips_not_the_tables(tmp_path):
    assert ImageHandler().content_digest(_build(tmp_path, "tiff_dpi"))


@pytest.mark.asyncio
async def test_a_write_that_changes_the_media_is_refused_and_leaves_the_file(client, tmp_path, monkeypatch):
    path = _build(tmp_path, "jpeg_camera")
    original = path.read_bytes()
    counter = itertools.count()
    monkeypatch.setattr(ImageHandler, "content_digest", lambda self, p: f"digest-{next(counter) % 2}")
    resp = await client.post("/write", json={"path": str(path), "fields": [{"key": "exif:Artist", "value": "Someone"}]})
    assert resp.status_code == 422, resp.text
    assert resp.json()["errors"][0]["code"] == "lossy_write"
    assert "media data" in resp.json()["detail"]
    assert path.read_bytes() == original
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".metalens-")] == []


@pytest.mark.asyncio
async def test_a_digest_that_cannot_be_computed_does_not_block_the_save(client, tmp_path, monkeypatch):
    path = _build(tmp_path, "jpeg_camera")

    def broken(self, p):
        raise OSError("cannot read")
    monkeypatch.setattr(ImageHandler, "content_digest", broken)
    resp = await client.post("/write", json={"path": str(path), "fields": [{"key": "exif:Artist", "value": "Someone"}]})
    assert resp.status_code == 200, resp.text
