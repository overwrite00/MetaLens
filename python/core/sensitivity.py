"""What a metadata field can reveal when the file is shared.

``classify(key)`` returns the category of a field key, or ``None`` when the field
is technical (size, codec, colour space...). The category drives the privacy
indicators of the interface; it never changes whether a field can be edited.

Categories: ``location`` (where), ``identity`` (who), ``device`` (what took or
made it, serial numbers, unique identifiers), ``datetime`` (when), ``software``
(what edited it), ``thumbnail`` (an embedded copy of the picture), ``history`` (edit history and
other data stored but not shown), ``text`` (free text written by a person).
"""
from __future__ import annotations

from fnmatch import fnmatchcase

CATEGORIES = ("location", "identity", "device", "datetime", "software", "thumbnail", "history", "text")

# Higher first: what the interface warns about most.
SEVERITY = {"location": 3, "identity": 3, "device": 2, "thumbnail": 2, "history": 2, "text": 1, "datetime": 1, "software": 1}

# Patterns are matched case-insensitively against the whole key; the first match wins.
_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("location", ("gps:*", "mkv:tag:*location*", "mkv:tag:*gps*", "xmp:exif:GPS*", "xmp:photoshop:City", "xmp:photoshop:State", "xmp:photoshop:Country",
                  "xmp:Iptc4xmpCore:Location", "xmp:Iptc4xmpCore:CountryCode", "xmp:Iptc4xmpExt:LocationShown*",
                  "iptc:City", "iptc:Province-State", "iptc:Country-PrimaryLocationName", "iptc:Sub-location",
                  "iptc:Country-PrimaryLocationCode", "qt:mdta:*location*", "hachoir:*:latitude", "hachoir:*:longitude", "hachoir:*:altitude", "qt:udta:©xyz", "exif:GPSInfo*")),
    ("history", ("xmp:@hidden", "pdf:@xmp")),
    ("thumbnail", ("mkv:attachment:*", "*Thumbnail*", "exif:Interop:*Thumb*", "ole:thumbnail", "xmp:xmp:Thumbnails")),
    ("identity", ("iptc:@block", "mkv:tag:artist", "mkv:tag:author", "mkv:tag:composer", "mkv:tag:copyright", "mkv:tag:director",
                  "mkv:tag:actor", "exif:Artist", "exif:Copyright", "exif:CameraOwnerName", "maker:*:OwnerName", "exif:OwnerName", "exif:XPAuthor",
                  "xmp:dc:creator", "xmp:dc:rights", "xmp:dc:publisher", "xmp:photoshop:Credit", "xmp:photoshop:Source",
                  "xmp:xmpRights:*", "xmp:plus:*", "iptc:By-line", "iptc:By-lineTitle", "iptc:Credit", "iptc:Source",
                  "iptc:CopyrightNotice", "iptc:Contact", "pdf:Author", "office:author", "office:last_modified_by",
                  "office:app.Company", "office:app.Manager", "ole:author", "ole:last_author", "ole:company",
                  "ole:manager", "odf:initial-creator", "odf:creator", "epub:creator", "epub:contributor",
                  "epub:publisher", "fs:owner", "fs:group", "xattr:*", "qt:mdta:*author*", "qt:mdta:*artist*", "hachoir:*:author", "hachoir:*:artist",
                  "hachoir:*:copyright")),
    ("device", ("exif:Make", "exif:Model", "exif:*SerialNumber", "exif:LensModel", "exif:LensMake", "exif:LensSpecification",
                "exif:ImageUniqueID", "maker:*", "exif:MakerNote", "exif:BodySerialNumber", "xmp:aux:*", "xmp:xmpMM:DocumentID",
                "xmp:xmpMM:InstanceID", "xmp:xmpMM:OriginalDocumentID", "xmp:tiff:Make", "xmp:tiff:Model",
                "qt:mdta:*make", "qt:mdta:*model", "qt:mdta:*content.identifier", "qt:udta:©mak", "qt:udta:©mod",
                "heif:*", "audio:encoder", "ole:app_name", "hachoir:*:camera_*")),
    ("datetime", ("mkv:Info:DateUTC", "mkv:tag:date*", "exif:DateTime*", "exif:SubSec*", "exif:OffsetTime*", "xmp:xmp:CreateDate", "xmp:xmp:ModifyDate",
                  "xmp:xmp:MetadataDate", "xmp:photoshop:DateCreated", "iptc:DateCreated", "iptc:TimeCreated",
                  "iptc:DigitalCreationDate", "iptc:DigitalCreationTime", "pdf:CreationDate", "pdf:ModDate",
                  "office:created", "office:modified", "ole:create_time", "ole:modified_time", "ole:last_print",
                  "odf:creation-date", "odf:date", "odf:print-date", "epub:modified", "epub:date", "qt:*Date",
                  "qt:mdta:*creationdate", "pngchunk:tIME", "icc:CreationDate", "fs:mtime", "fs:ctime", "fs:atime",
                  "tag:TDRC", "tag:TDRL", "tag:TDEN", "tag:TDTG", "hachoir:*:creation_date",
                  "hachoir:*:date_time_original")),
    ("software", ("mkv:Info:MuxingApp", "mkv:Info:WritingApp", "mkv:tag:encoder*", "exif:Software", "exif:ProcessingSoftware", "xmp:xmp:CreatorTool", "xmp:xmpMM:History",
                  "xmp:pdf:Producer", "pdf:Producer", "pdf:Creator", "office:app.Application", "office:app.AppVersion",
                  "odf:generator", "audio:vendor", "qt:mdta:*software", "qt:udta:©swr", "tag:TSSE", "tag:TENC",
                  "xmp:x:xmptk", "hachoir:*:producer")),
    ("text", ("mkv:Info:Title", "mkv:tag:*", "exif:UserComment", "exif:ImageDescription", "exif:XPComment", "exif:XPSubject", "exif:XPTitle",
              "exif:XPKeywords", "jpeg:Comment", "gif:comment", "png:*", "xmp:dc:description", "xmp:dc:title",
              "xmp:dc:subject", "iptc:Caption-Abstract", "iptc:Keywords", "iptc:Headline", "iptc:SpecialInstructions",
              "pdf:Subject", "pdf:Keywords", "pdf:Title", "office:description", "office:keywords", "office:subject",
              "office:custom.*", "ole:comments", "ole:keywords", "odf:description", "odf:keyword", "odf:custom.*",
              "epub:description", "tag:COMM*", "tag:USLT*", "tag:TXXX*")),
)

_COMPILED = tuple((category, tuple(p.lower() for p in patterns)) for category, patterns in _RULES)


def classify(key: str) -> str | None:
    """Category of the field with this key (``None`` for technical fields)."""
    lowered = key.lower()
    for category, patterns in _COMPILED:
        if any(fnmatchcase(lowered, pattern) for pattern in patterns):
            return category
    return None


def summarize(keys) -> dict[str, int]:
    """Number of fields per category among ``keys`` (categories without fields are omitted)."""
    counts: dict[str, int] = {}
    for key in keys:
        category = classify(key)
        if category:
            counts[category] = counts.get(category, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: -SEVERITY[item[0]]))
