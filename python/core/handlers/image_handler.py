from __future__ import annotations
import io
from pathlib import Path
from typing import Any

from core.base_handler import BaseMetadataHandler
from core.models import MetadataField, MetadataRecord

_READ_ONLY_EXTS = frozenset({".cr2", ".nef", ".arw", ".dng", ".orf", ".rw2", ".pef", ".srw"})

EXIF_TAGS = {
    # IFD0
    0x010E: ("exif:ImageDescription", "Description"),
    0x010F: ("exif:Make", "Camera Make"),
    0x0110: ("exif:Model", "Camera Model"),
    0x0112: ("exif:Orientation", "Orientation"),
    0x011A: ("exif:XResolution", "X Resolution"),
    0x011B: ("exif:YResolution", "Y Resolution"),
    0x0128: ("exif:ResolutionUnit", "Resolution Unit"),
    0x013B: ("exif:Artist", "Artist"),
    0x013E: ("exif:WhitePoint", "White Point"),
    0x013F: ("exif:PrimaryChromaticities", "Primary Chromaticities"),
    0x0213: ("exif:YCbCrPositioning", "YCbCr Positioning"),
    0x8298: ("exif:Copyright", "Copyright"),
    0x8769: ("exif:ExifIFDPointer", "Exif IFD Pointer"),
    0x8825: ("exif:GPSInfoIFDPointer", "GPS IFD Pointer"),
    # ExifIFD
    0x9000: ("exif:ExifVersion", "Exif Version"),
    0x9003: ("exif:DateTimeOriginal", "Date Taken"),
    0x9004: ("exif:DateTimeDigitized", "Date Digitized"),
    0x9201: ("exif:ShutterSpeedValue", "Shutter Speed"),
    0x9202: ("exif:ApertureValue", "Aperture"),
    0x9203: ("exif:BrightnessValue", "Brightness"),
    0x9204: ("exif:ExposureBiasValue", "Exposure Bias"),
    0x9205: ("exif:MaxApertureValue", "Max Aperture"),
    0x9207: ("exif:MeteringMode", "Metering Mode"),
    0x9208: ("exif:LightSource", "Light Source"),
    0x9209: ("exif:Flash", "Flash"),
    0x920A: ("exif:FocalLength", "Focal Length"),
    0x9214: ("exif:SubjectArea", "Subject Area"),
    0x927C: ("exif:MakerNote", "Maker Note"),
    0xA001: ("exif:ColorSpace", "Color Space"),
    0xA002: ("exif:PixelXDimension", "Pixel Width"),
    0xA003: ("exif:PixelYDimension", "Pixel Height"),
    0xA20E: ("exif:FocalPlaneXResolution", "Focal Plane X Resolution"),
    0xA20F: ("exif:FocalPlaneYResolution", "Focal Plane Y Resolution"),
    0xA210: ("exif:FocalPlaneResolutionUnit", "Focal Plane Resolution Unit"),
    0xA401: ("exif:CustomRendered", "Custom Rendered"),
    0xA402: ("exif:ExposureMode", "Exposure Mode"),
    0xA403: ("exif:WhiteBalance", "White Balance"),
    0xA404: ("exif:DigitalZoomRatio", "Digital Zoom Ratio"),
    0xA405: ("exif:FocalLengthIn35mmFilm", "Focal Length (35mm equiv)"),
    0xA406: ("exif:SceneCaptureType", "Scene Capture Type"),
    0xA408: ("exif:Contrast", "Contrast"),
    0xA409: ("exif:Saturation", "Saturation"),
    0xA40A: ("exif:Sharpness", "Sharpness"),
    # GPS IFD
    0x0001: ("gps:LatitudeRef", "GPS Latitude Ref"),
    0x0002: ("gps:Latitude", "GPS Latitude"),
    0x0003: ("gps:LongitudeRef", "GPS Longitude Ref"),
    0x0004: ("gps:Longitude", "GPS Longitude"),
    0x0005: ("gps:AltitudeRef", "GPS Altitude Ref"),
    0x0006: ("gps:Altitude", "GPS Altitude"),
    0x0007: ("gps:TimeStamp", "GPS Timestamp"),
    0x0010: ("gps:ImgDirectionRef", "GPS Direction Ref"),
    0x0011: ("gps:ImgDirection", "GPS Direction"),
    0x001D: ("gps:DateStamp", "GPS Date"),
}


class ImageHandler(BaseMetadataHandler):
    NAME = "image"
    EXTENSIONS = frozenset({
        ".jpg", ".jpeg", ".tif", ".tiff",
        ".png", ".bmp", ".gif", ".webp", ".ico",
        ".cr2", ".nef", ".arw", ".dng", ".orf", ".rw2", ".pef", ".srw",
    })
    PRIORITY = 10

    def read(self, path: Path) -> MetadataRecord:
        fields: list[MetadataField] = []
        errors: list[str] = []
        writable = path.suffix.lower() not in _READ_ONLY_EXTS

        try:
            from PIL import Image
            with Image.open(path) as img:
                fields += [
                    MetadataField("img:format", "Format", img.format or path.suffix[1:].upper(),
                                  editable=False, deletable=False, source="image"),
                    MetadataField("img:mode", "Color Mode", img.mode,
                                  editable=False, deletable=False, source="image"),
                    MetadataField("img:width", "Width (px)", img.width,
                                  editable=False, deletable=False, field_type="int", source="image"),
                    MetadataField("img:height", "Height (px)", img.height,
                                  editable=False, deletable=False, field_type="int", source="image"),
                ]
                dpi = img.info.get("dpi")
                if dpi:
                    fields.append(MetadataField("img:dpi", "DPI", f"{dpi[0]:.0f} x {dpi[1]:.0f}",
                                                editable=False, deletable=False, source="image"))
        except Exception as e:
            errors.append(f"Pillow read error: {e}")

        # EXIF via piexif (JPEG / TIFF)
        if path.suffix.lower() in {".jpg", ".jpeg", ".tif", ".tiff"} or path.suffix.lower() in _READ_ONLY_EXTS:
            fields += _read_exif(path, errors)

        # PNG text chunks
        if path.suffix.lower() == ".png":
            fields += _read_png_text(path, errors)

        record = self._make_record(path, read_errors=errors,
                                   supports_write=writable, supports_delete=writable)
        record.fields = fields
        return record

    def write(self, path: Path, fields: list[MetadataField]) -> None:
        if path.suffix.lower() not in {".jpg", ".jpeg", ".tif", ".tiff"}:
            raise NotImplementedError(f"EXIF write not supported for {path.suffix}")

        def _do_write(tmp: Path) -> None:
            import piexif
            try:
                exif_dict = piexif.load(str(tmp))
            except Exception:
                exif_dict = {"0th": {}, "Exif": {}, "GPS": {}, "1st": {}}

            tag_map = {v[0]: k for k, v in EXIF_TAGS.items()}
            for f in fields:
                tag_id = tag_map.get(f.key)
                if tag_id is None:
                    continue
                ifd = "GPS" if f.key.startswith("gps:") else ("Exif" if f.key.startswith("exif:") and tag_id >= 0x9000 else "0th")
                val = str(f.value).encode("utf-8") if isinstance(f.value, str) else f.value
                exif_dict[ifd][tag_id] = val

            exif_bytes = piexif.dump(exif_dict)
            piexif.insert(exif_bytes, str(tmp))

        self._atomic_write(path, _do_write)

    def delete(self, path: Path, keys: list[str]) -> None:
        if path.suffix.lower() not in {".jpg", ".jpeg", ".tif", ".tiff"}:
            raise NotImplementedError(f"EXIF delete not supported for {path.suffix}")

        def _do_delete(tmp: Path) -> None:
            import piexif
            try:
                exif_dict = piexif.load(str(tmp))
            except Exception:
                return
            tag_map = {v[0]: k for k, v in EXIF_TAGS.items()}
            for key in keys:
                tag_id = tag_map.get(key)
                if tag_id is None:
                    continue
                for ifd in ("0th", "Exif", "GPS", "1st"):
                    exif_dict[ifd].pop(tag_id, None)
            exif_bytes = piexif.dump(exif_dict)
            piexif.insert(exif_bytes, str(tmp))

        self._atomic_write(path, _do_delete)


def _read_exif(path: Path, errors: list[str]) -> list[MetadataField]:
    fields = []
    try:
        import piexif
        exif_dict = piexif.load(str(path))
        for ifd_name, ifd_data in exif_dict.items():
            if not isinstance(ifd_data, dict):
                continue
            source = "gps" if ifd_name == "GPS" else "exif"
            for tag_id, raw_val in ifd_data.items():
                info = EXIF_TAGS.get(tag_id)
                if info is None:
                    key = f"{source}:tag_{tag_id}"
                    label = f"Tag {tag_id}"
                else:
                    key, label = info
                value = _decode_exif_value(raw_val)
                fields.append(MetadataField(key, label, value, raw_value=raw_val,
                                            source=source))
    except Exception as e:
        errors.append(f"EXIF read error: {e}")
    return fields


def _read_png_text(path: Path, errors: list[str]) -> list[MetadataField]:
    fields = []
    try:
        from PIL import Image
        with Image.open(path) as img:
            for k, v in img.info.items():
                if isinstance(v, str):
                    fields.append(MetadataField(f"png:{k}", k, v, source="png"))
    except Exception as e:
        errors.append(f"PNG text read error: {e}")
    return fields


def _decode_exif_value(raw: Any) -> Any:
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace").rstrip("\x00")
    if isinstance(raw, tuple) and len(raw) == 2 and isinstance(raw[0], int):
        return f"{raw[0]}/{raw[1]}" if raw[1] != 0 else str(raw[0])
    if isinstance(raw, list):
        return [_decode_exif_value(v) for v in raw]
    return raw
