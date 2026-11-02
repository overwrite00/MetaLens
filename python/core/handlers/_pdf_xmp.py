"""XMP packet of a PDF: edit only the properties mirrored from /Info (decision D6).

The packet is parsed with ``xml.dom.minidom`` (keeps prefixes, attributes,
processing instructions such as ``<?xpacket?>`` and every unknown namespace)
and only the mapped properties are touched. A property may be written as a
child element or as an attribute of ``rdf:Description`` and may occur in more
than one description; ``snapshot`` captures every occurrence as a comparable,
deep-copyable tuple tree, ``restore`` rebuilds them exactly.
"""
from __future__ import annotations

from xml.dom import Node, minidom
from xml.parsers.expat import ExpatError

RDF = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
XML_NS = "http://www.w3.org/XML/1998/namespace"
XMLNS = "http://www.w3.org/2000/xmlns/"
DC = "http://purl.org/dc/elements/1.1/"
XMP = "http://ns.adobe.com/xap/1.0/"
PDF = "http://ns.adobe.com/pdf/1.3/"

MAX_PACKET = 4 * 1024 * 1024

# /Info key -> (namespace, local name, preferred prefix, kind)
PROPERTIES: dict[str, tuple[str, str, str, str]] = {
    "/Title": (DC, "title", "dc", "alt"),
    "/Author": (DC, "creator", "dc", "seq"),
    "/Subject": (DC, "description", "dc", "alt"),
    "/Keywords": (PDF, "Keywords", "pdf", "text"),
    "/Creator": (XMP, "CreatorTool", "xmp", "text"),
    "/Producer": (PDF, "Producer", "pdf", "text"),
    "/CreationDate": (XMP, "CreateDate", "xmp", "text"),
    "/ModDate": (XMP, "ModifyDate", "xmp", "text"),
}

Occurrences = tuple  # of ("attr", desc_index, value) | ("elem", desc_index, tree)


class XmpError(ValueError):
    """The packet cannot be parsed or edited safely."""


def _children(node, ns=None, local=None):
    return [c for c in node.childNodes if c.nodeType == Node.ELEMENT_NODE
            and (ns is None or c.namespaceURI == ns) and (local is None or c.localName == local)]


def _tree(node) -> tuple:
    """Comparable image of a node, independent of where namespaces are declared."""
    if node.nodeType == Node.ELEMENT_NODE:
        attrs = tuple(sorted(
            (a.namespaceURI or "", a.localName, a.prefix or "", a.value)
            for a in node.attributes.values() if a.namespaceURI != XMLNS))
        return ("e", node.namespaceURI or "", node.localName, node.prefix or "", attrs,
                tuple(_tree(c) for c in node.childNodes))
    if node.nodeType in (Node.TEXT_NODE, Node.CDATA_SECTION_NODE):
        return ("t", node.data)
    if node.nodeType == Node.COMMENT_NODE:
        return ("c", node.data)
    if node.nodeType == Node.PROCESSING_INSTRUCTION_NODE:
        return ("p", node.target, node.data)
    return ("t", "")


def _prefixes(tree: tuple, out: dict) -> dict:
    if tree[0] == "e":
        _, ns, _local, prefix, attrs, children = tree
        out.setdefault(prefix, ns)
        for a_ns, _l, a_prefix, _v in attrs:
            if a_prefix and a_prefix != "xml":
                out.setdefault(a_prefix, a_ns)
        for child in children:
            _prefixes(child, out)
    return out


def _qualified(prefix: str, local: str) -> str:
    return f"{prefix}:{local}" if prefix else local


class XmpPacket:
    """Mutable view of one XMP packet."""

    def __init__(self, data: bytes) -> None:
        if len(data) > MAX_PACKET:
            raise XmpError("XMP packet too large")
        upper = data.upper()
        if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
            raise XmpError("XMP packet with DTD/entity declarations")
        try:
            self.doc = minidom.parseString(data)
        except (ExpatError, ValueError) as exc:
            raise XmpError(f"XMP packet is not well-formed XML: {exc}") from None
        rdfs = self.doc.getElementsByTagNameNS(RDF, "RDF")
        if not rdfs:
            raise XmpError("XMP packet without rdf:RDF")
        self.rdf = rdfs[0]

    # ── lookup ───────────────────────────────────────────────────────────────

    def _descriptions(self, create: bool = False) -> list:
        descs = _children(self.rdf, RDF, "Description")
        if not descs and create:
            desc = self.doc.createElementNS(RDF, _qualified(self.rdf.prefix or "", "Description"))
            desc.setAttributeNS(RDF, _qualified(self.rdf.prefix or "", "about"), "")
            self.rdf.appendChild(desc)
            descs = [desc]
        return descs

    def _find(self, key: str) -> list[tuple[str, int, object]]:
        ns, local, _prefix, _kind = PROPERTIES[key]
        found = []
        for i, desc in enumerate(self._descriptions()):
            attr = desc.getAttributeNodeNS(ns, local)
            if attr is not None:
                found.append(("attr", i, attr))
            for el in _children(desc, ns, local):
                found.append(("elem", i, el))
        return found

    # ── snapshot / restore ───────────────────────────────────────────────────

    def snapshot(self, key: str) -> Occurrences:
        return tuple((kind, i, node.value if kind == "attr" else _tree(node))
                     for kind, i, node in self._find(key))

    def remove(self, key: str) -> None:
        for kind, _i, node in self._find(key):
            if kind == "attr":
                node.ownerElement.removeAttributeNode(node)
            else:
                node.parentNode.removeChild(node)

    def restore(self, key: str, occurrences: Occurrences) -> None:
        ns, local, prefix, _kind = PROPERTIES[key]
        self.remove(key)
        if not occurrences:
            return
        descs = self._descriptions(create=True)
        for kind, i, payload in occurrences:
            desc = descs[min(i, len(descs) - 1)]
            if kind == "attr":
                p = self._prefix_for(desc, ns, prefix)
                if not self._bound(desc, p, ns):
                    desc.setAttributeNS(XMLNS, f"xmlns:{p}", ns)
                desc.setAttributeNS(ns, _qualified(p, local), payload)
            else:
                desc.appendChild(self._build(payload, root=True))

    def _build(self, tree: tuple, root: bool = False):
        kind = tree[0]
        if kind == "t":
            return self.doc.createTextNode(tree[1])
        if kind == "c":
            return self.doc.createComment(tree[1])
        if kind == "p":
            return self.doc.createProcessingInstruction(tree[1], tree[2])
        _, ns, local, prefix, attrs, children = tree
        el = self.doc.createElementNS(ns or None, _qualified(prefix, local))
        if root:
            for p, uri in sorted(_prefixes(tree, {}).items()):
                if p == "xml":
                    continue
                el.setAttributeNS(XMLNS, f"xmlns:{p}" if p else "xmlns", uri)
        for a_ns, a_local, a_prefix, value in attrs:
            el.setAttributeNS(a_ns or None, _qualified(a_prefix, a_local), value)
        for child in children:
            el.appendChild(self._build(child))
        return el

    # ── write ────────────────────────────────────────────────────────────────

    def _prefix_for(self, element, ns: str, preferred: str) -> str:
        """A prefix bound to ``ns`` in scope of ``element``, else ``preferred``."""
        node = element
        while node is not None and node.nodeType == Node.ELEMENT_NODE:
            for attr in node.attributes.values():
                if attr.namespaceURI == XMLNS and attr.value == ns and attr.prefix == "xmlns":
                    return attr.localName
            node = node.parentNode
        return preferred

    def _bound(self, element, prefix: str, ns: str) -> bool:
        node = element
        while node is not None and node.nodeType == Node.ELEMENT_NODE:
            attr = node.getAttributeNodeNS(XMLNS, prefix)
            if attr is not None:
                return attr.value == ns
            node = node.parentNode
        return False

    def _rdf(self, local: str) -> str:
        return _qualified(self.rdf.prefix or "", local)

    def _li(self, text: str, default_lang: bool):
        li = self.doc.createElementNS(RDF, self._rdf("li"))
        if default_lang:
            li.setAttributeNS(XML_NS, "xml:lang", "x-default")
        li.appendChild(self.doc.createTextNode(text))
        return li

    @staticmethod
    def _set_text(element, text: str) -> None:
        while element.firstChild is not None:
            element.removeChild(element.firstChild)
        element.appendChild(element.ownerDocument.createTextNode(text))

    def _fill(self, element, kind: str, text: str) -> None:
        if kind == "text":
            self._set_text(element, text)
            return
        containers = [c for c in _children(element, RDF) if c.localName in ("Alt", "Seq", "Bag")]
        if not containers:
            while element.firstChild is not None:
                element.removeChild(element.firstChild)
            container = self.doc.createElementNS(RDF, self._rdf("Alt" if kind == "alt" else "Seq"))
            element.appendChild(container)
        else:
            container = containers[0]
        items = _children(container, RDF, "li")
        if kind == "alt":
            target = next((li for li in items if li.getAttributeNS(XML_NS, "lang") == "x-default"),
                          items[0] if items else None)
            if target is None:
                container.appendChild(self._li(text, default_lang=True))
            else:
                self._set_text(target, text)
        else:
            for li in items:
                container.removeChild(li)
            container.appendChild(self._li(text, default_lang=False))

    def set(self, key: str, text: str) -> None:
        ns, local, preferred, kind = PROPERTIES[key]
        found = self._find(key)
        if found:
            first_kind, _i, node = found[0]
            for kind_i, _j, other in found[1:]:  # one value after a sync
                if kind_i == "attr":
                    other.ownerElement.removeAttributeNode(other)
                else:
                    other.parentNode.removeChild(other)
            if first_kind == "attr":
                node.value = text
            else:
                self._fill(node, kind, text)
            return
        descs = self._descriptions(create=True)
        desc = next((d for d in descs if any(c.namespaceURI == ns for c in _children(d))
                     or any(a.namespaceURI == ns for a in d.attributes.values())), descs[0])
        prefix = self._prefix_for(desc, ns, preferred)
        el = self.doc.createElementNS(ns, _qualified(prefix, local))
        if not self._bound(desc, prefix, ns):
            el.setAttributeNS(XMLNS, f"xmlns:{prefix}", ns)
        if kind == "text":
            el.appendChild(self.doc.createTextNode(text))
        else:
            container = self.doc.createElementNS(RDF, self._rdf("Alt" if kind == "alt" else "Seq"))
            container.appendChild(self._li(text, default_lang=kind == "alt"))
            el.appendChild(container)
        desc.appendChild(el)

    def to_bytes(self) -> bytes:
        # No XML declaration is added: the packet keeps its <?xpacket?> wrapper as the first node.
        return "".join(node.toxml() for node in self.doc.childNodes).encode("utf-8")
