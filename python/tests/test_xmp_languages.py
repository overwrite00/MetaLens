"""The other languages of an XMP text are shown next to the main one."""
from core.handlers.image_handler import _xmp_fields
from core.xmp import XmpPacket

PACKET = b"""<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
<rdf:Description rdf:about="" xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title><rdf:Alt>
<rdf:li xml:lang="x-default">Sunset</rdf:li><rdf:li xml:lang="de">Sonnenuntergang</rdf:li><rdf:li xml:lang="it">Tramonto</rdf:li>
</rdf:Alt></dc:title></rdf:Description></rdf:RDF></x:xmpmeta>"""


def test_other_languages_are_listed_read_only():
    fields = {f.key: f for f in _xmp_fields(XmpPacket(PACKET), True)}
    assert fields["xmp:dc:title"].value == "Sunset" and fields["xmp:dc:title"].has_alternatives
    assert fields["xmp:dc:title@de"].value == "Sonnenuntergang" and fields["xmp:dc:title@it"].value == "Tramonto"
    for key in ("xmp:dc:title@de", "xmp:dc:title@it"):
        assert fields[key].computed and not fields[key].editable and not fields[key].deletable
