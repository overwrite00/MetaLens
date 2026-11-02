"""Photoshop image resources (APP13 ``Photoshop 3.0``) and IPTC-IIM (plan D4).

* ``parse_resources``/``serialize_resources``: the 8BIM resource list. Every
  resource keeps its original bytes; only edited resources are re-encoded.
* ``parse_iim``/``serialize_iim``: IPTC-IIM datasets (resource 0x0404). Every
  dataset keeps its original bytes, order and record; repeatable datasets are
  multi-valued fields. Text is written as UTF-8 and flagged with
  CodedCharacterSet 1:90 = ``ESC % G`` (1B 25 47).
"""
from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass

IPTC_RESOURCE = 0x0404
IPTC_DIGEST_RESOURCE = 0x0425
UTF8_MARKER = b"\x1b%G"
CHARSET = (1, 90)
MAX_DATASET = 0x7FFF   # standard (non-extended) dataset length


class IptcFormatError(ValueError):
    """Malformed Photoshop resources or IIM data."""


# ──────────────────────── 8BIM resources ─────────────────────────────────────

@dataclass
class Resource:
    signature: bytes
    rid: int
    name: bytes          # Pascal string body
    data: bytes
    raw: bytes           # exact original bytes (with padding)

    @classmethod
    def build(cls, rid: int, data: bytes, *, name: bytes = b"", signature: bytes = b"8BIM") -> "Resource":
        pascal = bytes([len(name)]) + name
        if len(pascal) % 2:
            pascal += b"\x00"
        raw = signature + struct.pack(">H", rid) + pascal + struct.pack(">L", len(data)) + data
        if len(data) % 2:
            raw += b"\x00"
        return cls(signature, rid, name, bytes(data), raw)

    def with_data(self, data: bytes) -> "Resource":
        return Resource.build(self.rid, data, name=self.name, signature=self.signature)


def parse_resources(data: bytes) -> tuple[list[Resource], bytes]:
    """Resources and the trailing bytes that do not form a resource (kept verbatim)."""
    out: list[Resource] = []
    pos, size = 0, len(data)
    while pos + 12 <= size and data[pos:pos + 4] in (b"8BIM", b"PHUT", b"AgHg", b"DCSR", b"MeSa"):
        start = pos
        sig = data[pos:pos + 4]
        rid = struct.unpack(">H", data[pos + 4:pos + 6])[0]
        nlen = data[pos + 6]
        name = data[pos + 7:pos + 7 + nlen]
        pos += 6 + 1 + nlen
        if (1 + nlen) % 2:
            pos += 1
        if pos + 4 > size:
            raise IptcFormatError("truncated Photoshop resource header")
        length = struct.unpack(">L", data[pos:pos + 4])[0]
        pos += 4
        if pos + length > size:
            raise IptcFormatError("truncated Photoshop resource")
        body = data[pos:pos + length]
        pos += length + (length % 2)
        pos = min(pos, size)
        out.append(Resource(sig, rid, name, body, data[start:pos]))
    return out, data[pos:]


def serialize_resources(resources: list[Resource], trailer: bytes = b"") -> bytes:
    return b"".join(r.raw for r in resources) + trailer


def update_digest(resources: list[Resource], iim: bytes | None) -> None:
    """Keep Photoshop's IPTC digest (0x0425) consistent with the new IIM block."""
    for i, r in enumerate(resources):
        if r.signature == b"8BIM" and r.rid == IPTC_DIGEST_RESOURCE:
            if iim is None:
                del resources[i]
            else:
                resources[i] = r.with_data(hashlib.md5(iim).digest())
            return


# ──────────────────────── IIM datasets ───────────────────────────────────────

@dataclass(frozen=True)
class Dataset:
    record: int
    number: int
    data: bytes

    def encode(self) -> bytes:
        n = len(self.data)
        if n <= MAX_DATASET:
            return bytes((0x1C, self.record, self.number)) + struct.pack(">H", n) + self.data
        size = (n.bit_length() + 7) // 8
        return bytes((0x1C, self.record, self.number)) + struct.pack(">H", 0x8000 | size) + \
            n.to_bytes(size, "big") + self.data


def parse_iim(data: bytes) -> tuple[list[Dataset], bytes]:
    """Datasets and trailing bytes (usually NUL padding) after the last dataset."""
    out: list[Dataset] = []
    pos, size = 0, len(data)
    while pos < size and data[pos] == 0x1C:
        if pos + 5 > size:
            raise IptcFormatError("truncated IPTC dataset header")
        record, number = data[pos + 1], data[pos + 2]
        length = struct.unpack(">H", data[pos + 3:pos + 5])[0]
        pos += 5
        if length & 0x8000:
            nbytes = length & 0x7FFF
            if nbytes == 0 or nbytes > 4 or pos + nbytes > size:
                raise IptcFormatError("invalid extended IPTC dataset length")
            length = int.from_bytes(data[pos:pos + nbytes], "big")
            pos += nbytes
        if pos + length > size:
            raise IptcFormatError("truncated IPTC dataset")
        out.append(Dataset(record, number, bytes(data[pos:pos + length])))
        pos += length
    trailer = data[pos:]
    if trailer.strip(b"\x00"):
        raise IptcFormatError("unexpected bytes after the IPTC datasets")
    return out, trailer


def serialize_iim(datasets: list[Dataset]) -> bytes:
    return b"".join(d.encode() for d in datasets)


def charset_marker(datasets: list[Dataset]) -> bytes | None:
    for d in datasets:
        if (d.record, d.number) == CHARSET:
            return d.data
    return None


def occurrences(datasets: list[Dataset], record: int, number: int) -> list[tuple[int, bytes]]:
    """(index within the record, data) of every occurrence of ``record:number``."""
    out, index = [], 0
    for d in datasets:
        if d.record != record:
            continue
        if d.number == number:
            out.append((index, d.data))
        index += 1
    return out


def remove(datasets: list[Dataset], record: int, number: int) -> int | None:
    """Remove every occurrence; return the index within the record of the first one."""
    first, index, kept = None, 0, []
    for d in datasets:
        if d.record == record:
            if d.number == number:
                if first is None:
                    first = index
                index += 1
                continue
            index += 1
        kept.append(d)
    datasets[:] = kept
    return first


def insert_at(datasets: list[Dataset], dataset: Dataset, index_in_record: int | None) -> None:
    """Insert at ``index_in_record`` within its record, or at its numeric position
    (after the last dataset of the record with a number <= its number)."""
    positions = [i for i, d in enumerate(datasets) if d.record == dataset.record]
    if index_in_record is not None:
        if index_in_record < len(positions):
            datasets.insert(positions[index_in_record], dataset)
            return
        at = positions[-1] + 1 if positions else _record_slot(datasets, dataset.record)
        datasets.insert(at, dataset)
        return
    at = None
    for i in positions:
        if datasets[i].number <= dataset.number:
            at = i + 1
    if at is None:
        at = positions[0] if positions else _record_slot(datasets, dataset.record)
    datasets.insert(at, dataset)


def _record_slot(datasets: list[Dataset], record: int) -> int:
    for i, d in enumerate(datasets):
        if d.record > record:
            return i
    return len(datasets)


def set_values(datasets: list[Dataset], record: int, number: int, payloads: list[bytes]) -> None:
    """Replace every occurrence by ``payloads`` (contiguous, at the first old position)."""
    first = remove(datasets, record, number)
    for offset, payload in enumerate(payloads):
        insert_at(datasets, Dataset(record, number, payload),
                  None if first is None else first + offset)
        if first is None:
            # Following items go right after the one just inserted.
            idx = [i for i, d in enumerate(datasets) if d.record == record and d.number == number]
            first = sum(1 for d in datasets[:idx[0]] if d.record == record) - offset
