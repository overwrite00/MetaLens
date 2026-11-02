"""Audio samples other than MP4 (ID3, Vorbis, FLAC, APEv2, ASF, AAC).

Owned by phase F2B.1: only that phase appends samples here. Every file is built
in Python (struct/wave/uuid/mutagen), deterministically, with fake but well-formed
stream headers: the payload after the headers is a fixed byte pattern so tests
can check that writing tags never touches the audio data.

Register with `@sample("name", ...)`; see `tests/factories/__init__.py`.
"""
from __future__ import annotations

import base64
import struct
import wave
from pathlib import Path

from . import registrar

SAMPLES: list = []
sample = registrar(SAMPLES, __name__)

# ──────────────────────── raw stream builders (no tags) ──────────────────────

#: MPEG-1 Layer III, 128 kbit/s, 44.1 kHz, no padding: 417-byte frames.
MP3_FRAME = bytes([0xFF, 0xFB, 0x90, 0x00]) + bytes(range(256)) + bytes(157)
PCM = bytes(range(256)) * 32  # 8 KiB of deterministic "samples"


def mp3_stream(frames: int = 12) -> bytes:
    return MP3_FRAME * frames


def write_mp3(path: Path, frames: int = 12) -> Path:
    path.write_bytes(mp3_stream(frames))
    return path


def write_wav(path: Path) -> Path:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(PCM)
    return path


def _ieee_extended(value: int) -> bytes:
    """80-bit IEEE 754 extended float of a positive integer (AIFF sample rate)."""
    exponent = value.bit_length() - 1
    mantissa = value << (63 - exponent)
    return struct.pack(">HQ", 16383 + exponent, mantissa)


def write_aiff(path: Path) -> Path:
    frames = len(PCM) // 2
    comm = struct.pack(">hIh", 1, frames, 16) + _ieee_extended(8000)
    ssnd = struct.pack(">II", 0, 0) + PCM
    body = b"AIFF" + b"COMM" + struct.pack(">I", len(comm)) + comm \
        + b"SSND" + struct.pack(">I", len(ssnd)) + ssnd
    path.write_bytes(b"FORM" + struct.pack(">I", len(body)) + body)
    return path


def write_aac_adts(path: Path, frames: int = 40) -> Path:
    """MPEG-4 AAC LC ADTS, 44.1 kHz stereo, 32-byte frames."""
    length = 32
    header = bytearray(7)
    header[0] = 0xFF
    header[1] = 0xF1                                 # MPEG-4, layer 0, no CRC
    header[2] = (1 << 6) | (4 << 2) | 0              # LC, 44.1 kHz, channel cfg hi bit 0
    header[3] = (2 << 6) | ((length >> 11) & 0x3)    # 2 channels
    header[4] = (length >> 3) & 0xFF
    header[5] = ((length & 0x7) << 5) | 0x1F         # buffer fullness 0x7FF
    header[6] = 0xFC
    frame = bytes(header) + bytes(range(1, length - 6))
    path.write_bytes(frame * frames)
    return path


def write_flac(path: Path) -> Path:
    """fLaC + STREAMINFO (last block) + fake frame payload."""
    min_block = max_block = 4096
    sample_rate, channels, bps, total = 8000, 1, 16, 16000
    packed = (sample_rate << 44) | ((channels - 1) << 41) | ((bps - 1) << 36) | total
    info = struct.pack(">HH", min_block, max_block) + b"\x00\x00\x00" + b"\x00\x00\x00" \
        + packed.to_bytes(8, "big") + bytes(16)
    block = bytes([0x80 | 0]) + len(info).to_bytes(3, "big") + info
    path.write_bytes(b"fLaC" + block + b"\xff\xf8" + PCM[:2048])
    return path


def _ogg(path: Path, packets_by_page: list[list[bytes]], granules: list[int]) -> Path:
    from mutagen.ogg import OggPage
    data = b""
    for seq, (packets, granule) in enumerate(zip(packets_by_page, granules)):
        page = OggPage()
        page.serial = 0x4D4C
        page.sequence = seq
        page.position = granule
        page.packets = packets
        page.first = seq == 0
        page.last = seq == len(packets_by_page) - 1
        data += page.write()
    path.write_bytes(data)
    return path


def _vcomment(vendor: str, comments: list[tuple[str, str]]) -> bytes:
    out = struct.pack("<I", len(vendor.encode())) + vendor.encode()
    out += struct.pack("<I", len(comments))
    for k, v in comments:
        raw = f"{k}={v}".encode("utf-8")
        out += struct.pack("<I", len(raw)) + raw
    return out


def write_ogg_vorbis(path: Path, comments: list[tuple[str, str]] = ()) -> Path:
    ident = b"\x01vorbis" + struct.pack("<IBIiii", 0, 1, 8000, 0, 64000, 0) + b"\xb8\x01"
    comment = b"\x03vorbis" + _vcomment("MetaLens test", list(comments)) + b"\x01"
    setup = b"\x05vorbis" + bytes(range(64))
    audio = PCM[:1024]
    return _ogg(path, [[ident], [comment, setup], [audio]], [0, 0, 16000])


def write_ogg_opus(path: Path, comments: list[tuple[str, str]] = ()) -> Path:
    head = b"OpusHead" + struct.pack("<BBHIhB", 1, 1, 312, 48000, 0, 0)
    tags = b"OpusTags" + _vcomment("MetaLens test", list(comments))
    audio = PCM[:1024]
    return _ogg(path, [[head], [tags], [audio]], [0, 0, 48312])


def write_wavpack(path: Path) -> Path:
    """One WavPack block header (mono, 16-bit, 44.1 kHz) + payload."""
    payload = PCM[:1024]
    flags = 0x4 | 0x1 | (9 << 23)   # mono, 16-bit, rate index 9 = 44100
    header = b"wvpk" + struct.pack("<IHBBIIIII", 24 + len(payload), 0x410, 0, 0,
                                   44100, 0, 44100, flags, 0)
    path.write_bytes(header + payload)
    return path


def asf_guid(text: str) -> bytes:
    """ASF GUIDs are stored with the first three fields little-endian."""
    import uuid
    return uuid.UUID(text).bytes_le


ASF_HEADER_GUID = "75B22630-668E-11CF-A6D9-00AA0062CE6C"
ASF_DATA_GUID = "75B22636-668E-11CF-A6D9-00AA0062CE6C"
ASF_PAYLOAD = PCM[:1024]


def write_asf(path: Path) -> Path:
    """Minimal ASF: a Header Object without children + a Data Object with a fixed payload.

    mutagen parses it (no stream properties: duration 0) and, when saving tags,
    adds the content description / extended content description / header
    extension (metadata, metadata library) and padding objects to the header.
    """
    header = asf_guid(ASF_HEADER_GUID) + struct.pack("<QL", 30, 0) + b"\x01\x02"
    data = asf_guid(ASF_DATA_GUID) + struct.pack("<Q", 50 + len(ASF_PAYLOAD)) + bytes(16) \
        + struct.pack("<QH", 0, 0x0101) + ASF_PAYLOAD
    path.write_bytes(header + data)
    return path


def wm_picture(data: bytes | None = None) -> bytes:
    """WM/Picture blob: type, size, MIME and description (UTF-16LE, NUL-terminated), data."""
    data = png_bytes() if data is None else data
    return b"\x03" + len(data).to_bytes(4, "little") + "image/png".encode("utf-16-le") \
        + b"\x00\x00" + b"\x00\x00" + data


# ──────────────────────── tag builders ───────────────────────────────────────

def png_bytes() -> bytes:
    return b"\x89PNG\r\n\x1a\n" + bytes(range(48))


def add_full_id3(path: Path, *, version: int = 4) -> None:
    """Tag of mp3_v24_full: multi-valued text, lyrics, URLs, binaries, counters."""
    import mutagen.id3 as id3
    tags = id3.ID3()
    tags.add(id3.TIT2(encoding=3, text=["Song"]))
    tags.add(id3.TPE1(encoding=3, text=["Artist A", "Artist B"]))
    tags.add(id3.TCON(encoding=3, text=["Rock", "Jazz"]))
    tags.add(id3.TALB(encoding=1, text=["Album ü"]))
    tags.add(id3.TRCK(encoding=0, text=["3/10"]))
    tags.add(id3.TDRC(encoding=3, text=["2026"]))
    tags.add(id3.USLT(encoding=3, lang="eng", desc="", text="Line one\nLine two\nLine three"))
    tags.add(id3.COMM(encoding=3, lang="eng", desc="note", text=["First", "Second"]))
    tags.add(id3.TXXX(encoding=3, desc="MOOD", text=["calm"]))
    tags.add(id3.WOAR(url="https://example.org/a"))
    tags.add(id3.WOAR(url="https://example.org/b"))
    tags.add(id3.WXXX(encoding=0, desc="shop", url="https://example.org/shop"))
    tags.add(id3.APIC(encoding=3, mime="image/png", type=3, desc="cover", data=png_bytes()))
    tags.add(id3.POPM(email="user@example.org", rating=196, count=3))
    tags.add(id3.PCNT(count=7))
    tags.add(id3.PRIV(owner="com.example", data=b"\x00\x01private"))
    tags.add(id3.UFID(owner="http://musicbrainz.org", data=b"abc-123"))
    tags.add(id3.GEOB(encoding=3, mime="application/octet-stream", filename="x.bin",
                      desc="blob", data=b"\x02\x03"))
    tags.add(id3.SYLT(encoding=3, lang="eng", format=2, type=1, desc="synced",
                      text=[("Hello", 1000), ("World", 2000)]))
    tags.add(id3.TIPL(encoding=3, people=[["producer", "P. Name"]]))
    if version == 3:
        tags.update_to_v23()
    tags.save(path, v2_version=version, v23_sep=None)


# ──────────────────────── registered samples ─────────────────────────────────

_ID3_MUTATIONS = {
    "tag:TDRC": ["2027-05"],
    "tag:WOAR": ["https://example.org/a", "https://example.org/b", "https://example.org/c"],
    "tag:WXXX:shop": "https://example.org/other",
}


@sample("mp3_v24_full", mutations=_ID3_MUTATIONS)
def mp3_v24_full(tmp_path: Path) -> Path:
    path = write_mp3(tmp_path / "full.mp3")
    add_full_id3(path, version=4)
    return path


@sample("mp3_v23_latin1", mutations={"tag:TYER": ["2027"], "tag:TDRC": ["2027"]})
def mp3_v23_latin1(tmp_path: Path) -> Path:
    import mutagen.id3 as id3
    path = write_mp3(tmp_path / "v23.mp3")
    tags = id3.ID3()
    tags.add(id3.TIT2(encoding=0, text=["Caf\xe9"]))
    tags.add(id3.TPE1(encoding=0, text=["Jos\xe9"]))
    tags.add(id3.TDRC(encoding=0, text=["2026"]))
    tags.add(id3.COMM(encoding=0, lang="eng", desc="", text=["Latin-1 comment"]))
    tags.update_to_v23()
    tags.save(path, v2_version=3, v23_sep=None)
    return path


@sample("mp3_untagged", mutations=_ID3_MUTATIONS)
def mp3_untagged(tmp_path: Path) -> Path:
    return write_mp3(tmp_path / "untagged.mp3")


@sample("wav_untagged", mutations=_ID3_MUTATIONS)
def wav_untagged(tmp_path: Path) -> Path:
    return write_wav(tmp_path / "untagged.wav")


@sample("wav_id3", mutations=_ID3_MUTATIONS)
def wav_id3(tmp_path: Path) -> Path:
    import mutagen.id3 as id3
    from mutagen.wave import WAVE
    path = write_wav(tmp_path / "tagged.wav")
    audio = WAVE(path)
    audio.add_tags()
    audio.tags.add(id3.TIT2(encoding=3, text=["Wave title"]))
    audio.tags.add(id3.TPE1(encoding=3, text=["One", "Two"]))
    audio.save()
    return path


@sample("aiff_untagged", mutations=_ID3_MUTATIONS)
def aiff_untagged(tmp_path: Path) -> Path:
    return write_aiff(tmp_path / "untagged.aiff")


@sample("aac_adts", tags={"read_only"})
def aac_adts(tmp_path: Path) -> Path:
    return write_aac_adts(tmp_path / "stream.aac")


@sample("flac_vorbis_picture")
def flac_vorbis_picture(tmp_path: Path) -> Path:
    from mutagen.flac import FLAC, Picture
    path = write_flac(tmp_path / "pic.flac")
    audio = FLAC(path)
    audio.add_tags()
    audio.tags.append(("TITLE", "Flac title"))
    audio.tags.append(("ARTIST", "First"))
    audio.tags.append(("ARTIST", "Second"))
    audio.tags.append(("Genre", "Ambient"))
    pic = Picture()
    pic.type, pic.mime, pic.desc, pic.data = 3, "image/png", "front", png_bytes()
    audio.add_picture(pic)
    audio.save()
    return path


@sample("flac_untagged")
def flac_untagged(tmp_path: Path) -> Path:
    return write_flac(tmp_path / "untagged.flac")


@sample("ogg_vorbis")
def ogg_vorbis(tmp_path: Path) -> Path:
    from mutagen.flac import Picture
    pic = Picture()
    pic.type, pic.mime, pic.desc, pic.data = 3, "image/png", "", png_bytes()
    encoded = base64.b64encode(pic.write()).decode("ascii")
    return write_ogg_vorbis(tmp_path / "song.ogg", [
        ("TITLE", "Ogg title"), ("ARTIST", "X"), ("ARTIST", "Y"),
        ("METADATA_BLOCK_PICTURE", encoded)])


@sample("ogg_opus")
def ogg_opus(tmp_path: Path) -> Path:
    return write_ogg_opus(tmp_path / "song.opus", [("TITLE", "Opus title"), ("ALBUM", "LP")])


@sample("wv_apev2")
def wv_apev2(tmp_path: Path) -> Path:
    from mutagen.apev2 import APEv2, APEValue, BINARY, EXTERNAL
    path = write_wavpack(tmp_path / "song.wv")
    tags = APEv2()
    tags["Title"] = "Ape title"
    tags["Artist"] = ["One", "Two"]
    tags["Cover Art (Front)"] = APEValue(b"cover.png\x00" + png_bytes(), BINARY)
    tags["Related"] = APEValue("https://example.org/rel", EXTERNAL)
    tags.save(path)
    return path


@sample("wv_untagged")
def wv_untagged(tmp_path: Path) -> Path:
    return write_wavpack(tmp_path / "untagged.wv")


#: Order of the attributes of wma_asf as mutagen reads them back: content
#: description, extended content description, metadata, metadata library.
WMA_ORDER = ["Title", "Author", "WM/AlbumTitle", "WM/Genre", "WM/TrackNumber", "WM/EncodingTime",
             "WM/IsVBR", "WM/Picture", "WM/Mood", "WM/Genre", "WM/MediaClassPrimaryID", "WM/Lyrics"]


@sample("wma_asf")
def wma_asf(tmp_path: Path) -> Path:
    """Real ASF file (built in Python, tagged by mutagen) with every attribute class.

    Two ``WM/Genre`` values land in different header objects (extended content
    description + metadata library), so they are not adjacent when read back;
    ``WM/EncodingTime`` is a QWORD above 2**53.
    """
    import uuid
    import mutagen.asf as asf
    path = write_asf(tmp_path / "song.wma")
    audio = asf.ASF(path)
    for name, attr in (
            ("Title", asf.ASFUnicodeAttribute("Wma title")),
            ("Author", asf.ASFUnicodeAttribute("Wma author")),
            ("WM/AlbumTitle", asf.ASFUnicodeAttribute("Album")),
            ("WM/Genre", asf.ASFUnicodeAttribute("Rock")),
            ("WM/Genre", asf.ASFUnicodeAttribute("Pop")),
            ("WM/TrackNumber", asf.ASFDWordAttribute(3)),
            ("WM/EncodingTime", asf.ASFQWordAttribute(2**60 + 3)),
            ("WM/IsVBR", asf.ASFBoolAttribute(False)),
            ("WM/Picture", asf.ASFByteArrayAttribute(wm_picture())),
            ("WM/MediaClassPrimaryID",
             asf.ASFGUIDAttribute(uuid.UUID("D1607DBC-E323-4BE2-86A1-48A42A28441E").bytes_le)),
            ("WM/Mood", asf.ASFUnicodeAttribute("calm", stream=1)),
            ("WM/Lyrics", asf.ASFUnicodeAttribute("testo", language=1))):
        audio.tags.append((name, attr))
    audio.save()
    return path
