"""XMP packet reader/editor that preserves everything it does not touch (plan D4).

The packet is parsed with expat (no DTD, no external entities) into a small node
tree that keeps the ORIGINAL namespace prefixes and declaration sites, comments,
processing instructions and whitespace. The bytes before and after the root
element (``<?xpacket ...?>``, padding) are kept verbatim. Re-serialization only
differs from the input where a property was edited (plus XML-equivalent
normalisation inside tags: quoting and character references).

Properties are addressed by ``(namespace URI, local name)`` and live as
attributes or child elements of the ``rdf:Description`` nodes. Supported value
forms: simple text, ``rdf:Alt`` (language alternatives, ``x-default`` first),
``rdf:Bag``/``rdf:Seq`` of simple items (an empty array reads as ``[]``).
Anything else (structs, qualifiers, nested arrays) is never shown and never
modified.

Padding: the whitespace between the root element and ``<?xpacket end?>``
exists so that a packet can be edited in place. ``to_bytes`` keeps the packet
at its original size whenever the padding allows: growth consumes padding,
shrinking gives it back (first and last padding characters are kept).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any
from xml.parsers import expat

RDF = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
XML_NS = "http://www.w3.org/XML/1998/namespace"
X_NS = "adobe:ns:meta/"

# Canonical prefixes used in MetaLens keys (``xmp:<prefix>:<local>``).
KNOWN_PREFIXES = {
    "http://purl.org/dc/elements/1.1/": "dc",
    "http://ns.adobe.com/xap/1.0/": "xmp",
    "http://ns.adobe.com/xap/1.0/rights/": "xmpRights",
    "http://ns.adobe.com/xap/1.0/mm/": "xmpMM",
    "http://ns.adobe.com/photoshop/1.0/": "photoshop",
    "http://ns.adobe.com/exif/1.0/": "exif",
    "http://ns.adobe.com/exif/1.0/aux/": "aux",
    "http://ns.adobe.com/tiff/1.0/": "tiff",
    "http://cipa.jp/exif/1.0/": "exifEX",
    "http://ns.adobe.com/lightroom/1.0/": "lr",
    "http://ns.adobe.com/camera-raw-settings/1.0/": "crs",
    "http://iptc.org/std/Iptc4xmpCore/1.0/xmlns/": "Iptc4xmpCore",
    "http://iptc.org/std/Iptc4xmpExt/2008-02-29/": "Iptc4xmpExt",
    "http://ns.adobe.com/pdf/1.3/": "pdf",
    RDF: "rdf",
    X_NS: "x",
}

NEW_PACKET = (
    '<?xpacket begin="﻿" id="W5M0MpCehiHzreSzNTczkc9d"?>\n'
    '<x:xmpmeta xmlns:x="adobe:ns:meta/">\n'
    ' <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">\n'
    '  <rdf:Description rdf:about=""/>\n'
    ' </rdf:RDF>\n'
    '</x:xmpmeta>\n'
    '<?xpacket end="w"?>'
).encode("utf-8")

_XML_CHAR = re.compile("[^\t\n\r\x20-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]")


_SUFFIX = re.compile(rb"([ \t\r\n]*)(.*)\Z", re.S)     # padding run, then <?xpacket end ...?> etc.
_PADDING = re.compile(rb"[ \t\r\n]+")


class XmpFormatError(ValueError):
    """The packet cannot be parsed or edited safely."""


def invalid_xml_char(text: str) -> str | None:
    m = _XML_CHAR.search(text)
    return None if m is None else m.group(0)


# ──────────────────────── node tree ──────────────────────────────────────────

Name = tuple  # (uri | None, local, prefix | None)


@dataclass(eq=False)
class Node:
    kind: str                         # "elem" | "comment" | "pi"
    name: Name = (None, "", None)
    attrs: list[list] = field(default_factory=list)       # [[Name, value], ...]
    nsdecls: list[tuple] = field(default_factory=list)    # [(prefix | None, uri), ...]
    children: list["Node"] = field(default_factory=list)
    text: str = ""
    tail: str = ""
    data: str = ""                    # comment text / PI "target data"

    @property
    def uri(self):
        return self.name[0]

    @property
    def local(self):
        return self.name[1]

    def is_(self, uri: str, local: str) -> bool:
        return self.kind == "elem" and self.name[0] == uri and self.name[1] == local

    def elements(self) -> list["Node"]:
        return [c for c in self.children if c.kind == "elem"]

    def attr(self, uri, local):
        for name, value in self.attrs:
            if name[0] == uri and name[1] == local:
                return value
        return None


def _split(name: str) -> Name:
    parts = name.split("\x01")
    if len(parts) == 3:
        return parts[0], parts[1], parts[2]
    if len(parts) == 2:
        return parts[0], parts[1], None
    return None, parts[0], None


def _parse_xml(data: bytes) -> tuple[Node, int, int]:
    """Parse ``data``; return (root, byte start of root, byte end of root)."""
    parser = expat.ParserCreate("utf-8", namespace_separator="\x01")
    parser.namespace_prefixes = True
    parser.buffer_text = True
    parser.ordered_attributes = True
    try:
        parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
    except AttributeError:  # pragma: no cover
        pass
    stack: list[Node] = []
    pending: list[tuple] = []
    root: list[Node] = []
    span = [None, None]

    def add_text(text: str) -> None:
        if not stack:
            return  # top-level whitespace: kept through the verbatim prefix/suffix
        parent = stack[-1]
        if parent.children:
            parent.children[-1].tail += text
        else:
            parent.text += text

    def start_ns(prefix, uri):
        pending.append((prefix, uri))

    def start(name, attrs):
        node = Node("elem", _split(name))
        node.attrs = [[_split(attrs[i]), attrs[i + 1]] for i in range(0, len(attrs), 2)]
        node.nsdecls = list(pending)
        pending.clear()
        if stack:
            stack[-1].children.append(node)
        else:
            if root:
                raise XmpFormatError("several root elements")
            root.append(node)
            span[0] = parser.CurrentByteIndex
        stack.append(node)

    def end(name):
        stack.pop()
        if not stack:
            span[1] = parser.CurrentByteIndex

    def comment(text):
        if stack:
            stack[-1].children.append(Node("comment", data=text))

    def pi(target, text):
        if stack:
            stack[-1].children.append(Node("pi", data=f"{target} {text}" if text else target))

    def doctype(*args):
        raise XmpFormatError("DOCTYPE is not allowed in XMP")

    def entity(*args):
        raise XmpFormatError("entity declarations are not allowed in XMP")

    parser.StartNamespaceDeclHandler = start_ns
    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.CharacterDataHandler = add_text
    parser.CommentHandler = comment
    parser.ProcessingInstructionHandler = pi
    parser.StartDoctypeDeclHandler = doctype
    parser.EntityDeclHandler = entity
    try:
        parser.Parse(data, True)
    except expat.ExpatError as exc:
        raise XmpFormatError(f"malformed XMP: {exc}") from None
    if not root:
        raise XmpFormatError("XMP packet without root element")
    start_idx, end_idx = span
    # CurrentByteIndex at the end event points at "</...>" (or at "<.../>").
    close = data.find(b">", end_idx)
    if close < 0:
        raise XmpFormatError("cannot locate the end of the XMP root element")
    return root[0], start_idx, close + 1


def _escape_text(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\r", "&#13;")


def _escape_attr(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace('"', "&quot;")
            .replace("\t", "&#9;").replace("\n", "&#10;").replace("\r", "&#13;"))


class _Serializer:
    def __init__(self) -> None:
        self.parts: list[str] = []

    def run(self, node: Node, scope: dict) -> str:
        self._node(node, scope)
        return "".join(self.parts)

    def _qname(self, name: Name, scope: dict, decls: list, is_attr: bool) -> str:
        uri, local, prefix = name
        if uri is None:
            return local
        if uri == XML_NS:
            return f"xml:{local}"
        if prefix is not None and scope.get(prefix) == uri:
            return f"{prefix}:{local}"
        if not is_attr and prefix is None and scope.get(None) == uri:
            return local
        for p, u in scope.items():
            if u == uri and p is not None:
                return f"{p}:{local}"
        wanted = prefix or KNOWN_PREFIXES.get(uri) or "ns"
        candidate, n = wanted, 1
        while candidate in scope and scope[candidate] != uri:
            n += 1
            candidate = f"{wanted}{n}"
        scope[candidate] = uri
        decls.append((candidate, uri))
        return f"{candidate}:{local}"

    def _node(self, node: Node, scope: dict) -> None:
        if node.kind == "comment":
            self.parts.append(f"<!--{node.data}-->")
            return
        if node.kind == "pi":
            self.parts.append(f"<?{node.data}?>")
            return
        scope = dict(scope)
        decls: list[tuple] = []
        for prefix, uri in node.nsdecls:
            scope[prefix] = uri
            decls.append((prefix, uri))
        tag = self._qname(node.name, scope, decls, False)
        attrs = [(self._qname(n, scope, decls, True), v) for n, v in node.attrs]
        out = ["<", tag]
        for prefix, uri in decls:
            out.append(f' xmlns="{_escape_attr(uri)}"' if prefix is None
                       else f' xmlns:{prefix}="{_escape_attr(uri)}"')
        for q, v in attrs:
            out.append(f' {q}="{_escape_attr(v)}"')
        if not node.children and not node.text:
            out.append("/>")
            self.parts.append("".join(out))
            return
        out.append(">")
        self.parts.append("".join(out))
        self.parts.append(_escape_text(node.text))
        for child in node.children:
            self._node(child, scope)
            self.parts.append(_escape_text(child.tail))
        self.parts.append(f"</{tag}>")


def serialize_node(node: Node) -> str:
    """Standalone, deterministic XML of one element (used as snapshot raw)."""
    return _Serializer().run(node, {})


def parse_fragment(text: str) -> Node:
    root, _, _ = _parse_xml(text.encode("utf-8"))
    return root


# ──────────────────────── packet ─────────────────────────────────────────────

@dataclass
class Prop:
    """One top-level property occurrence."""
    uri: str
    local: str
    prefix: str | None
    desc_index: int
    attr: bool                          # attribute of rdf:Description
    node: Node | None = None            # element form
    value: str | None = None            # attribute form


class XmpPacket:
    def __init__(self, data: bytes) -> None:
        data = bytes(data)
        if data.startswith((b"\xfe\xff", b"\xff\xfe", b"\x00\x00\xfe\xff", b"\xff\xfe\x00\x00")):
            raise XmpFormatError("UTF-16/32 XMP packets are read-only")
        self.root, start, end = _parse_xml(data)
        self.prefix = data[:start]
        self.suffix = data[end:]
        self.size = len(data)                 # the packet keeps this size while padding allows
        self.rdf = self._find_rdf()

    @classmethod
    def new(cls) -> "XmpPacket":
        return cls(NEW_PACKET)

    def same_content(self, other: "XmpPacket") -> bool:
        """Same wrapper and same XML tree (as serialized), whatever the padding."""
        return (self.prefix == other.prefix
                and _PADDING.sub(b"", self.suffix) == _PADDING.sub(b"", other.suffix)
                and _Serializer().run(self.root, {}) == _Serializer().run(other.root, {}))

    def is_blank(self) -> bool:
        """No property left and nothing else than a packet ``new()`` creates."""
        return not self.properties() and self.same_content(XmpPacket.new())

    def _find_rdf(self) -> Node:
        if self.root.is_(RDF, "RDF"):
            return self.root
        for child in self.root.elements():
            if child.is_(RDF, "RDF"):
                return child
        raise XmpFormatError("XMP packet without rdf:RDF")

    def descriptions(self) -> list[Node]:
        return [c for c in self.rdf.elements() if c.is_(RDF, "Description")]

    def to_bytes(self) -> bytes:
        body = _Serializer().run(self.root, {}).encode("utf-8")
        return self.prefix + body + self._padded_suffix(len(self.prefix) + len(body))

    def _padded_suffix(self, used: int) -> bytes:
        """The suffix with its padding resized so that the packet keeps ``self.size``
        bytes (or as close as the padding allows)."""
        match = _SUFFIX.match(self.suffix)
        pad, rest = match.group(1), match.group(2)
        if len(pad) < 2:
            return self.suffix              # no padding to resize (at most a line break)
        head, middle, tail = pad[:1], pad[1:-1], pad[-1:]
        target = max(self.size - used - len(rest) - len(head) - len(tail), 0)
        middle = middle[:target] if target <= len(middle) else middle + b" " * (target - len(middle))
        return head + middle + tail + rest

    # ── property access ──
    def properties(self) -> list[Prop]:
        out = []
        for i, desc in enumerate(self.descriptions()):
            for name, value in desc.attrs:
                uri, local, prefix = name
                if uri in (None, RDF, XML_NS):
                    continue
                out.append(Prop(uri, local, prefix, i, True, value=value))
            for child in desc.elements():
                if child.uri in (None, RDF):
                    continue
                out.append(Prop(child.uri, child.local, child.name[2], i, False, node=child))
        return out

    def find(self, uri: str, local: str) -> list[Prop]:
        return [p for p in self.properties() if p.uri == uri and p.local == local]

    def doc_prefix(self, uri: str) -> str | None:
        for p in self.properties():
            if p.uri == uri and p.prefix:
                return p.prefix
        return None

    # ── raw snapshot / restore ──
    def raw(self, uri: str, local: str) -> tuple | None:
        """Exact pre-image of a property: tuple of occurrences, or None when absent.

        Occurrence = ``("attr", description index, attribute position, prefix, value)``
        or ``("elem", description index, element position, standalone XML)``.
        """
        occ = []
        for p in self.find(uri, local):
            desc = self.descriptions()[p.desc_index]
            if p.attr:
                position = next(i for i, a in enumerate(desc.attrs)
                                if a[0][0] == uri and a[0][1] == local)
                occ.append(("attr", p.desc_index, position, p.prefix, p.value))
            else:
                position = desc.elements().index(p.node)
                occ.append(("elem", p.desc_index, position, serialize_node(p.node)))
        return tuple(occ) or None

    def remove(self, uri: str, local: str) -> bool:
        removed = False
        for desc in self.descriptions():
            before = len(desc.attrs)
            desc.attrs = [a for a in desc.attrs if not (a[0][0] == uri and a[0][1] == local)]
            removed |= len(desc.attrs) != before
            for child in list(desc.elements()):
                if child.is_(uri, local):
                    _remove_child(desc, child)
                    removed = True
        return removed

    def put_raw(self, uri: str, local: str, occurrences) -> None:
        """Replace the property by its raw occurrences (see ``raw``)."""
        self.put_raw_many([(uri, local, occurrences)])

    def put_raw_many(self, items) -> None:
        """Restore several properties at once: ``[(uri, local, occurrences | None)]``.

        All of them are removed first, then every occurrence is reinserted at its
        recorded position in ascending order, so the original order comes back
        even when neighbouring properties were removed too.
        """
        for uri, local, _ in items:
            self.remove(uri, local)
        flat = [(uri, local, occ) for uri, local, occs in items for occ in (occs or ())]
        for uri, local, occ in sorted(flat, key=lambda t: (t[2][0], t[2][1], t[2][2])):
            if occ[0] == "attr":
                _, index, position, prefix, value = occ
                self._description(index).attrs.insert(position, [(uri, local, prefix), value])
            else:
                _, index, position, text = occ
                desc = self._description(index)
                node = parse_fragment(text)
                _strip_redundant(node, self._scope(desc))
                _insert_child(desc, node, position)

    def _scope(self, desc: Node) -> dict:
        scope: dict = {}
        for node in (self.root, self.rdf, desc):
            if node is self.rdf and self.rdf is self.root:
                continue
            scope.update({p: u for p, u in node.nsdecls})
        return scope

    def _description(self, index: int) -> Node:
        descs = self.descriptions()
        while len(descs) <= index:
            node = Node("elem", (RDF, "Description", _prefix_in(self.rdf, RDF)))
            node.attrs = [[(RDF, "about", _prefix_in(self.rdf, RDF)), ""]]
            _insert_child(self.rdf, node, len(self.rdf.elements()))
            descs = self.descriptions()
        return descs[index]

    # ── typed values ──
    def read_value(self, prop: Prop, form: str) -> Any:
        """Value of ``prop`` in ``form`` ("text" | "alt" | "bag" | "seq"), or None if the
        occurrence does not have a simple shape for that form."""
        if prop.attr:
            return prop.value if form in ("text", "alt") else None
        node = prop.node
        elems = node.elements()
        if not elems:
            if node.attrs or form not in ("text", "alt"):
                return None
            return node.text
        if len(elems) != 1 or node.attrs or node.text.strip() or any(e.tail.strip() for e in elems):
            return None
        array = elems[0]
        kinds = {"alt": "Alt", "bag": "Bag", "seq": "Seq"}
        if form == "text" or not array.is_(RDF, kinds[form]):
            return None
        items = array.elements()
        if not items:
            # An empty array (``<rdf:Bag/>``) holds no value; only Bag/Seq may be empty.
            empty = form in ("bag", "seq") and not array.attrs and not array.children and not array.text.strip()
            return [] if empty else None
        if any(not li.is_(RDF, "li") or li.elements() for li in items):
            return None
        if form == "alt":
            for li in items:
                if li.attr(XML_NS, "lang") == "x-default":
                    return li.text
            return items[0].text
        if any(li.attrs for li in items):
            return None
        return [li.text for li in items]

    def has_alternatives(self, prop: Prop) -> bool:
        """``prop`` is an ``rdf:Alt`` with more than one alternative (other languages)."""
        if prop.attr:
            return False
        elems = prop.node.elements()
        if len(elems) != 1 or not elems[0].is_(RDF, "Alt"):
            return False
        return len(elems[0].elements()) > 1

    def write_value(self, uri: str, local: str, prefix: str, form: str, value: Any) -> None:
        """Set the property (first occurrence, in place); create it when absent."""
        occ = self.find(uri, local)
        for extra in occ[1:]:
            self._remove_occurrence(extra)
        current = occ[0] if occ else None
        if form == "text":
            if current is not None and current.attr:
                desc = self.descriptions()[current.desc_index]
                for a in desc.attrs:
                    if a[0][0] == uri and a[0][1] == local:
                        a[1] = value
                return
            node = current.node if current is not None else self._new_property(uri, local, prefix)
            node.children, node.text, node.attrs = [], value, []
            return
        if current is not None and current.attr:
            self._remove_occurrence(current)
            current = None
        node = current.node if current is not None else self._new_property(uri, local, prefix)
        kinds = {"alt": "Alt", "bag": "Bag", "seq": "Seq"}
        elems = node.elements()
        array = elems[0] if len(elems) == 1 and elems[0].is_(RDF, kinds[form]) else None
        rdf_prefix = _prefix_in(self.rdf, RDF)
        if array is None:
            array = Node("elem", (RDF, kinds[form], rdf_prefix))
            node.children, node.attrs, node.text = [], [], ""
            _insert_child(node, array, 0)
        if form == "alt":
            items = array.elements()
            default = next((li for li in items if li.attr(XML_NS, "lang") == "x-default"), None)
            if default is None or default.elements():
                if default is not None:
                    _remove_child(array, default)
                default = Node("elem", (RDF, "li", rdf_prefix))
                default.attrs = [[(XML_NS, "lang", "xml"), "x-default"]]
                _insert_child(array, default, 0)
            default.text, default.children = value, []
            return
        for li in list(array.elements()):
            _remove_child(array, li)
        for i, item in enumerate(value):
            li = Node("elem", (RDF, "li", rdf_prefix), text=item)
            _insert_child(array, li, i)

    def _remove_occurrence(self, prop: Prop) -> None:
        desc = self.descriptions()[prop.desc_index]
        if prop.attr:
            desc.attrs = [a for a in desc.attrs if not (a[0][0] == prop.uri and a[0][1] == prop.local)]
        else:
            _remove_child(desc, prop.node)

    def _new_property(self, uri: str, local: str, prefix: str) -> Node:
        desc = self._description(0)
        node = Node("elem", (uri, local, self.doc_prefix(uri) or prefix))
        _insert_child(desc, node, len(desc.elements()))
        return node


def _strip_redundant(node: Node, scope: dict) -> None:
    """Drop namespace declarations that repeat the binding already in effect."""
    if node.kind != "elem":
        return
    node.nsdecls = [(p, u) for p, u in node.nsdecls if scope.get(p, object()) != u]
    inner = {**scope, **{p: u for p, u in node.nsdecls}}
    for child in node.children:
        _strip_redundant(child, inner)


def _prefix_in(node: Node, uri: str) -> str | None:
    if node.uri == uri:
        return node.name[2]
    return KNOWN_PREFIXES.get(uri)


def _indent_before(parent: Node, index: int) -> str:
    """Whitespace preceding the ``index``-th child node."""
    if index == 0:
        return parent.text
    return parent.children[index - 1].tail


def _remove_child(parent: Node, child: Node) -> None:
    i = parent.children.index(child)
    if i == len(parent.children) - 1:
        # Last child: its tail is the indentation of the closing tag; keep it.
        if i == 0:
            parent.text = child.tail if not parent.text.strip() else parent.text
        else:
            parent.children[i - 1].tail = child.tail
    del parent.children[i]


def _insert_child(parent: Node, child: Node, element_position: int) -> None:
    """Insert ``child`` before the ``element_position``-th element child (or at the end)."""
    elems = parent.elements()
    if element_position < len(elems):
        index = parent.children.index(elems[element_position])
        child.tail = _indent_before(parent, index) if _indent_before(parent, index).strip() == "" else ""
        parent.children.insert(index, child)
        return
    if parent.children:
        last = parent.children[-1]
        closing = last.tail
        inner = _indent_before(parent, len(parent.children) - 1)
        last.tail = inner if inner.strip() == "" else ""
        child.tail = closing
    else:
        closing = parent.text if parent.text.strip() == "" else ""
        parent.text = (closing + " ") if "\n" in closing else ""
        child.tail = closing
    parent.children.append(child)
