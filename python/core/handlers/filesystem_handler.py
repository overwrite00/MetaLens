from __future__ import annotations
import os
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path

from core.base_handler import BaseMetadataHandler
from core.models import MetadataField, MetadataRecord


class FilesystemHandler(BaseMetadataHandler):
    NAME = "filesystem"
    EXTENSIONS = frozenset()  # handles ALL files — used as secondary layer
    PRIORITY = 90             # low priority (run after format-specific handlers)

    def can_handle(self, path: Path) -> bool:
        return True  # universal fallback

    def read(self, path: Path) -> MetadataRecord:
        fields: list[MetadataField] = []
        errors: list[str] = []
        try:
            st = path.stat()
            fields += [
                MetadataField("fs:size", "File Size (bytes)", st.st_size,
                              editable=False, deletable=False, field_type="int", source="filesystem"),
                MetadataField("fs:mtime", "Modified", _ts(st.st_mtime),
                              editable=True, deletable=False, field_type="datetime", source="filesystem"),
                MetadataField("fs:ctime", "Created / Changed", _ts(st.st_ctime),
                              editable=False, deletable=False, field_type="datetime", source="filesystem"),
                MetadataField("fs:atime", "Last Accessed", _ts(st.st_atime),
                              editable=False, deletable=False, field_type="datetime", source="filesystem"),
            ]
            if sys.platform != "win32":
                mode = stat.filemode(st.st_mode)
                fields.append(MetadataField("fs:permissions", "Permissions", mode,
                                            editable=False, deletable=False, source="filesystem"))
                try:
                    import pwd, grp
                    fields.append(MetadataField("fs:owner", "Owner",
                                                pwd.getpwuid(st.st_uid).pw_name,
                                                editable=False, deletable=False, source="filesystem"))
                    fields.append(MetadataField("fs:group", "Group",
                                                grp.getgrgid(st.st_gid).gr_name,
                                                editable=False, deletable=False, source="filesystem"))
                except Exception:
                    pass
                # Extended attributes (Linux/macOS)
                try:
                    import xattr as xattr_lib
                    for attr_name in xattr_lib.listxattr(str(path)):
                        try:
                            val = xattr_lib.getxattr(str(path), attr_name)
                            fields.append(MetadataField(
                                f"xattr:{attr_name}", f"xattr: {attr_name}",
                                val.decode("utf-8", errors="replace"),
                                field_type="str", source="xattr"
                            ))
                        except Exception:
                            pass
                except ImportError:
                    pass
            else:
                # Windows — file attributes flags
                try:
                    attrs = _win_file_attributes(path)
                    if attrs:
                        fields.append(MetadataField("fs:win_attrs", "Windows Attributes", attrs,
                                                    editable=False, deletable=False, source="filesystem"))
                except Exception:
                    pass
        except Exception as e:
            errors.append(f"Filesystem read error: {e}")

        record = self._make_record(path, read_errors=errors, supports_write=True, supports_delete=False)
        record.fields = fields
        return record

    def write(self, path: Path, fields: list[MetadataField]) -> None:
        for f in fields:
            if f.key == "fs:mtime":
                try:
                    dt = _parse_dt(f.value)
                    ts = dt.timestamp()
                    st = path.stat()
                    os.utime(path, (st.st_atime, ts))
                except Exception as e:
                    raise RuntimeError(f"Cannot set mtime: {e}") from e
            elif f.key.startswith("xattr:") and sys.platform != "win32":
                try:
                    import xattr as xattr_lib
                    attr_name = f.key[len("xattr:"):]
                    xattr_lib.setxattr(str(path), attr_name, str(f.value).encode())
                except Exception as e:
                    raise RuntimeError(f"Cannot set xattr {f.key}: {e}") from e

    def delete(self, path: Path, keys: list[str]) -> None:
        for key in keys:
            if key.startswith("xattr:") and sys.platform != "win32":
                try:
                    import xattr as xattr_lib
                    attr_name = key[len("xattr:"):]
                    xattr_lib.removexattr(str(path), attr_name)
                except Exception as e:
                    raise RuntimeError(f"Cannot delete xattr {key}: {e}") from e


def _ts(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()


def _parse_dt(value) -> datetime:
    from datetime import datetime
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))


def _win_file_attributes(path: Path) -> str:
    try:
        import ctypes
        attrs = ctypes.windll.kernel32.GetFileAttributesW(str(path))
        if attrs == -1:
            return ""
        flags = []
        FILE_ATTRIBUTE = {
            0x1: "READONLY", 0x2: "HIDDEN", 0x4: "SYSTEM",
            0x20: "ARCHIVE", 0x80: "NORMAL", 0x100: "TEMPORARY",
            0x800: "COMPRESSED", 0x4000: "ENCRYPTED",
        }
        for bit, name in FILE_ATTRIBUTE.items():
            if attrs & bit:
                flags.append(name)
        return ", ".join(flags)
    except Exception:
        return ""
