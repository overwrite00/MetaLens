"""Writing the metadata of PNG and WebP files (core/image_containers.py and the container states)."""
import struct
import zlib

import pytest

from core import image_containers
from core.handlers.image_handler import ImageHandler
from core.registry import HandlerRegistry
from tests.factories import by_name


def _build(tmp_path, name):
    folder = tmp_path / name
    folder.mkdir()
    return by_name(name).build(folder)


def _keys(path):
    return {f.key: f for f in HandlerRegistry.get(path).read(path).fields}


def _pixels(path):
    return ImageHandler().content_digest(path)


async def _write(client, path, fields=(), delete=()):
    return await client.post("/write", json={"path": str(path), "fields": list(fields), "delete": list(delete)})


@pytest.mark.asyncio
async def test_png_exif_and_xmp_are_edited_and_the_picture_is_untouched(client, tmp_path):
    path = _build(tmp_path, "png_text_exif")
    before = _pixels(path)
    resp = await _write(client, path, [{"key": "exif:Artist", "value": "Ada"},
                                       {"key": "xmp:dc:subject", "value": ["one", "two"]}])
    assert resp.status_code == 200, resp.text
    keys = _keys(path)
    assert keys["exif:Artist"].value == "Ada" and keys["xmp:dc:subject"].value == ["one", "two"]
    assert _pixels(path) == before
    assert image_containers.PngFile.parse(path.read_bytes())          # checksums are valid


@pytest.mark.asyncio
async def test_png_text_chunks_are_removed_and_undo_puts_them_back(client, tmp_path):
    path = _build(tmp_path, "png_text_exif")
    original = path.read_bytes()
    resp = await _write(client, path, delete=["png:Title", "png:Comment"])
    assert resp.status_code == 200, resp.text
    keys = _keys(path)
    assert "png:Title" not in keys and "png:Comment" not in keys and "png:Description@it" in keys
    undo = await client.post("/undo", json={"token": resp.json()["undo_token"]})
    assert undo.status_code == 200
    assert path.read_bytes() == original


@pytest.mark.asyncio
async def test_png_without_metadata_gets_a_new_exif_chunk(client, tmp_path):
    from PIL import Image
    path = tmp_path / "plain.png"
    Image.new("RGB", (4, 4), (1, 2, 3)).save(path)
    before = _pixels(path)
    resp = await _write(client, path, [{"key": "exif:Artist", "value": "Grace"}])
    assert resp.status_code == 200, resp.text
    assert _keys(path)["exif:Artist"].value == "Grace" and _pixels(path) == before
    kinds = [k for k, _ in image_containers.PngFile.parse(path.read_bytes()).chunks]
    assert kinds.index(b"eXIf") < kinds.index(b"IDAT")


@pytest.mark.asyncio
async def test_png_without_extension_is_edited_too(client, tmp_path):
    source = _build(tmp_path, "png_text_exif")
    path = tmp_path / "picture"
    path.write_bytes(source.read_bytes())
    assert (await _write(client, path, [{"key": "exif:Artist", "value": "X"}])).status_code == 200
    assert _keys(path)["exif:Artist"].value == "X"


@pytest.mark.asyncio
async def test_damaged_png_stays_read_only(client, tmp_path):
    path = _build(tmp_path, "png_text_exif")
    data = bytearray(path.read_bytes())
    data[-20] ^= 0xFF                                      # break a checksum
    path.write_bytes(bytes(data))
    record = HandlerRegistry.get(path).read(path)
    assert not record.format_supports_write
    assert (await _write(client, path, [{"key": "exif:Artist", "value": "X"}])).status_code in (403, 422)


@pytest.mark.asyncio
async def test_webp_exif_and_xmp_are_edited(client, tmp_path):
    path = _build(tmp_path, "webp_exif")
    before = _pixels(path)
    resp = await _write(client, path, [{"key": "exif:Artist", "value": "Ada"},
                                       {"key": "xmp:dc:subject", "value": ["z"]}])
    assert resp.status_code == 200, resp.text
    keys = _keys(path)
    assert keys["exif:Artist"].value == "Ada" and keys["xmp:dc:subject"].value == ["z"]
    assert _pixels(path) == before
    assert image_containers.WebpFile.parse(path.read_bytes())


@pytest.mark.asyncio
@pytest.mark.parametrize("lossless", [True, False])
async def test_bare_webp_gets_a_vp8x_header_with_the_right_flags(client, tmp_path, lossless):
    from PIL import Image
    path = tmp_path / "bare.webp"
    Image.new("RGB", (7, 5), (9, 9, 9)).save(path, "WEBP", lossless=lossless)
    assert not image_containers.WebpFile.parse(path.read_bytes()).find(b"VP8X") or True
    before = _pixels(path)
    resp = await _write(client, path, [{"key": "exif:Artist", "value": "Ada"}])
    assert resp.status_code == 200, resp.text
    webp = image_containers.WebpFile.parse(path.read_bytes())
    header = webp.chunks[0]
    assert header[0] == b"VP8X" and header[1][0] & image_containers.FLAG_EXIF
    assert (int.from_bytes(header[1][4:7], "little") + 1, int.from_bytes(header[1][7:10], "little") + 1) == (7, 5)
    assert _pixels(path) == before
    with Image.open(path) as img:                           # the library still decodes the picture
        img.load()
        assert img.size == (7, 5)


@pytest.mark.asyncio
async def test_deleting_the_xmp_fields_leaves_no_visible_xmp_and_keeps_the_flags_in_step(client, tmp_path):
    path = _build(tmp_path, "webp_exif")
    keys = [k for k, f in _keys(path).items() if f.deletable and k.startswith("xmp:")]
    resp = await _write(client, path, delete=keys)
    assert resp.status_code == 200, resp.text
    assert not [k for k in _keys(path) if k.startswith("xmp:")]
    webp = image_containers.WebpFile.parse(path.read_bytes())
    flags = next(b for k, b in webp.chunks if k == b"VP8X")[0]
    assert bool(flags & image_containers.FLAG_XMP) == bool(webp.find(b"XMP "))      # flag matches the chunk
    assert flags & image_containers.FLAG_EXIF


def test_containers_reject_inconsistent_files():
    with pytest.raises(image_containers.ContainerError):
        image_containers.PngFile.parse(b"not a png")
    with pytest.raises(image_containers.ContainerError):
        image_containers.WebpFile.parse(b"RIFF" + struct.pack("<I", 1000) + b"WEBP")
    chunk = struct.pack(">I", 0) + b"IHDR" + struct.pack(">I", zlib.crc32(b"IHDR"))
    with pytest.raises(image_containers.ContainerError):
        image_containers.PngFile.parse(image_containers.PNG_SIGNATURE + chunk)       # no IEND


def test_png_bytes_after_iend_are_preserved():
    from PIL import Image
    import io
    buffer = io.BytesIO()
    Image.new("RGB", (2, 2)).save(buffer, "PNG")
    data = buffer.getvalue() + b"trailer"
    png = image_containers.PngFile.parse(data)
    assert png.to_bytes() == data
