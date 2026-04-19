from __future__ import annotations
import hashlib
from pathlib import Path

from core.models import DiffResult, MetadataField, MetadataRecord


def compute_diff(record_a: MetadataRecord, record_b: MetadataRecord) -> DiffResult:
    map_a = {f.key: f for f in record_a.fields}
    map_b = {f.key: f for f in record_b.fields}
    keys_a = set(map_a)
    keys_b = set(map_b)

    only_in_a = [map_a[k] for k in keys_a - keys_b]
    only_in_b = [map_b[k] for k in keys_b - keys_a]
    changed = []
    identical = []

    for key in keys_a & keys_b:
        fa, fb = map_a[key], map_b[key]
        if _values_equal(fa.value, fb.value):
            identical.append(fa)
        else:
            changed.append((fa, fb))

    return DiffResult(
        file_a=record_a.file_path,
        file_b=record_b.file_path,
        only_in_a=sorted(only_in_a, key=lambda f: f.key),
        only_in_b=sorted(only_in_b, key=lambda f: f.key),
        changed=sorted(changed, key=lambda p: p[0].key),
        identical=sorted(identical, key=lambda f: f.key),
    )


def _values_equal(a, b) -> bool:
    if type(a) != type(b):
        a, b = str(a), str(b)
    if isinstance(a, bytes):
        return hashlib.md5(a).digest() == hashlib.md5(b).digest()
    return str(a).strip().lower() == str(b).strip().lower()
