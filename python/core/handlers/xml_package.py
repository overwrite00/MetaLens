"""Changing the metadata of OpenDocument and EPUB packages: an XML part of a ZIP is edited and written back.

The part is parsed, the elements of the field are removed / rewritten / put back, and the package is written again
with every other member copied unchanged (the same way as for Office documents). A ``Mutations`` subclass says which
part holds the metadata and how a key is found in it.
"""
from __future__ import annotations

import copy
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from zipfile import ZipFile

from core.base_handler import ABSENT
from core.errors import ErrorCode, MetadataValueError, MetadataValueErrors
from core.handlers.office_handler import _rewrite_parts
from core.zipxml import read_xml

for _prefix, _uri in {
        "office": "urn:oasis:names:tc:opendocument:xmlns:office:1.0", "meta": "urn:oasis:names:tc:opendocument:xmlns:meta:1.0",
        "dc": "http://purl.org/dc/elements/1.1/", "xlink": "http://www.w3.org/1999/xlink",
        "grddl": "http://www.w3.org/2003/g/data-view#", "ooo": "http://openoffice.org/2004/office",
        "opf": "http://www.idpf.org/2007/opf", "dcterms": "http://purl.org/dc/terms/"}.items():
    ET.register_namespace(_prefix, _uri)


@dataclass(frozen=True)
class Placed:
    """One saved element: equality is its content; the position is only where to put it back."""
    content: bytes
    index: int = field(default=0, compare=False)


@dataclass(frozen=True)
class AttrSaved:
    owner: str                       # local name of the element that holds the attribute
    attribute: str
    value: str | None


def _bytes(element) -> bytes:
    clone = copy.deepcopy(element)
    clone.tail = None
    return ET.tostring(clone, encoding="utf-8")


class Mutations:
    """Hooks of ``BaseMetadataHandler`` on top of the XML part of a package."""

    def part_name(self, archive: ZipFile) -> str:             # pragma: no cover - implemented by the handlers
        raise NotImplementedError

    def locate(self, root, key: str) -> list[tuple]:          # pragma: no cover
        """``(parent, element, attribute or None)`` of every place that holds ``key``."""
        raise NotImplementedError

    def make(self, root, key: str, value) -> list:            # pragma: no cover
        """New elements that hold ``value`` for ``key`` (an empty list: the key cannot be created)."""
        raise NotImplementedError

    def removable(self, key: str) -> bool:
        return True

    def text_of(self, key: str, value) -> str:
        return str(value)

    # ── helpers ──
    def _load(self, tmp: Path):
        with ZipFile(tmp) as archive:
            if any(n.startswith("META-INF/") and "signatures" in n for n in archive.namelist()):
                raise MetadataValueError(None, ErrorCode.READ_ONLY, "Editing digitally signed packages is not supported")
            name = self.part_name(archive)
            return name, read_xml(archive, name)

    @staticmethod
    def _store(tmp: Path, name: str, root) -> None:
        _rewrite_parts(tmp, {name: ET.tostring(root, encoding="utf-8", xml_declaration=True)})

    # ── mutation hooks ──
    def canonical(self, field, value):
        from core import values
        return values.canonicalize(field, value)

    def apply_delete(self, tmp: Path, keys: list[str]) -> None:
        name, root = self._load(tmp)
        errors = []
        for key in keys:
            hits = self.locate(root, key)
            if not hits or not self.removable(key):
                errors.append(MetadataValueError(key, ErrorCode.NOT_DELETABLE if hits else ErrorCode.UNKNOWN_FIELD))
                continue
            self._remove(hits)
        if errors:
            raise MetadataValueErrors(errors)
        self._store(tmp, name, root)

    @staticmethod
    def _remove(hits) -> None:
        for parent, element, attribute in hits:
            if attribute is not None:
                element.attrib.pop(attribute, None)
            elif element in list(parent):
                parent.remove(element)

    def apply_write(self, tmp: Path, fields) -> None:
        name, root = self._load(tmp)
        errors = []
        current = {f.key: f.value for f in self.read(tmp).fields}
        fields = [f for f in fields if current.get(f.key) != f.value]       # a value already there is not a change
        if not fields:
            return
        for f in fields:
            try:
                hits = self.locate(root, f.key)
                texts = [self.text_of(f.key, v) for v in (f.value if isinstance(f.value, list) else [f.value])]
                if hits and all(h[2] is not None for h in hits):          # an attribute
                    hits[0][1].attrib[hits[0][2]] = texts[0]
                    continue
                parent = hits[0][0] if hits else None
                made = self.make(root, f.key, f.value)
                if not made:
                    raise MetadataValueError(f.key, ErrorCode.UNSUPPORTED, f"{f.key}: this field cannot be created here")
                if not hits and made:
                    parent = made[0][0]
                position = list(parent).index(hits[0][1]) if hits else len(parent)
                self._remove(hits)
                for offset, (_parent, element) in enumerate(made):
                    parent.insert(min(position + offset, len(parent)), element)
            except MetadataValueError as exc:
                errors.append(exc)
        if errors:
            raise MetadataValueErrors(errors)
        self._store(tmp, name, root)

    def snapshot(self, tmp: Path, keys: list[str]):
        with ZipFile(tmp) as archive:
            root = read_xml(archive, self.part_name(archive))
        out = {}
        for key in keys:
            hits = self.locate(root, key)
            out[key] = ABSENT if not hits else tuple(
                (AttrSaved(element.tag.rsplit("}", 1)[-1], attribute, element.attrib.get(attribute))
                 if attribute is not None else Placed(_bytes(element), list(parent).index(element)))
                for parent, element, attribute in hits)
        return out

    def restore(self, tmp: Path, snap) -> None:
        name, root = self._load(tmp)
        placed: list[tuple[int, object, bytes]] = []
        for key, raw in snap.items():
            hits = self.locate(root, key)
            parent = hits[0][0] if hits else self.make_parent(root, key)
            self._remove(hits)
            if raw is ABSENT:
                continue
            for item in raw:
                if isinstance(item, AttrSaved):
                    owner = next((e for e in root.iter() if e.tag.rsplit("}", 1)[-1] == item.owner), None)
                    if owner is not None and item.value is not None:
                        owner.attrib[item.attribute] = item.value
                else:
                    placed.append((item.index, parent, item.content))
        for index, parent, content in sorted(placed, key=lambda entry: entry[0]):     # original order
            parent.insert(min(index, len(parent)), ET.fromstring(content))
        self._store(tmp, name, root)

    def writable_field(self, path: Path, key: str):
        record = self.read(path)
        return next((f for f in self.writable_fields(path, record) if f.key == key), None)

    def make_parent(self, root, key: str):                    # pragma: no cover - rarely needed
        return next(iter(root))
