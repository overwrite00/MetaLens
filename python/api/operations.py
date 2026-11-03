"""Operations on several files or on dates: batch preparation for sharing, date shift, tag copy, export, rename.

Every change goes through the same single-file save as ``/write`` (one verified transaction and one Undo token
per file); a batch is therefore NOT atomic across files, and says which files were saved and which were not.
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
from datetime import timedelta
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field

from api import routes
from api.routes import ApiCode, ApiError, PATH_LOCKS, SanitizeRequest, stable_read
from core import values
from core.errors import ErrorCode, MetadataValueErrors
from core.models import MetadataField
from core.path_security import PathSecurityError, validate_directory_path

router = APIRouter()
MAX_FILES = 500
MAX_EXPORT_FILES = 2000


def _item(path: str, work) -> dict[str, Any]:
    """Result of ``work()`` for one file: ``{"path", "ok", ...}``; an error never stops the batch."""
    try:
        return {"path": path, "ok": True, **work()}
    except ApiError as exc:
        return {"path": path, "ok": False, "status": exc.status, "errors": exc.errors, "detail": exc.detail}
    except MetadataValueErrors as exc:
        return {"path": path, "ok": False, "status": 422, "errors": exc.to_dicts(), "detail": str(exc)}


def _paths(paths: list[str]) -> list[str]:
    if len(paths) > MAX_FILES:
        raise ApiError.one(422, ApiCode.INVALID_REQUEST, f"at most {MAX_FILES} files per request")
    return paths


# ─────────────────────────── prepare many files for sharing ───────────────────────────

class SanitizeManyRequest(BaseModel):
    paths: list[str] = Field(min_length=1)
    categories: list[str] = Field(min_length=1)
    preview: bool = True
    keep: list[str] = Field(default_factory=list)


@router.post("/sanitize-many")
def sanitize_many(req: SanitizeManyRequest):
    results = [_item(p, lambda p=p: routes.sanitize_metadata(
        SanitizeRequest(path=p, categories=req.categories, preview=req.preview, keep=req.keep)))
        for p in _paths(req.paths)]
    return {"ok": all(r["ok"] for r in results), "preview": req.preview, "results": results,
            "undo_tokens": [r["undo_token"] for r in results if r.get("undo_token")]}


# ─────────────────────────── date shift ───────────────────────────

class ShiftRequest(BaseModel):
    paths: list[str] = Field(min_length=1)
    seconds: int = Field(ge=-10 * 365 * 86400, le=10 * 365 * 86400)
    keys: list[str] | None = None            # default: every editable date-time field of the file
    preview: bool = True


def _shifted(field: MetadataField, seconds: int) -> str | None:
    """The shifted canonical value of a date-time field, ``None`` when it cannot be shifted."""
    try:
        if field.field_type == "localdatetime":
            return values.format_datetime(values.parse_localdatetime(field.value) + timedelta(seconds=seconds))
        if field.field_type == "datetime":
            return values.format_datetime(values.parse_datetime(field.value) + timedelta(seconds=seconds))
    except (ValueError, OverflowError, values.MetadataValueError):
        return None
    return None


def _shift_one(path: str, req: ShiftRequest) -> dict[str, Any]:
    fpath = routes._valid_file(path)
    with PATH_LOCKS.reading(fpath):
        ctx = stable_read(fpath, lambda: routes._read_full_record(fpath))
    wanted = set(req.keys) if req.keys else None
    changes, skipped = [], []
    for f in ctx.record.fields:
        if f.field_type not in ("datetime", "localdatetime") or f.multi or routes._is_filesystem_key(f.key):
            continue
        if wanted is not None and f.key not in wanted:
            continue
        new = _shifted(f, req.seconds)
        if not f.editable or new is None:
            skipped.append({"key": f.key, "label": f.label, "reason": "this date cannot be changed"})
        else:
            changes.append({"key": f.key, "label": f.label, "from": f.value, "to": new})
    out: dict[str, Any] = {"changes": changes, "skipped": skipped, "undo_token": None}
    if not req.preview and changes:
        saved = routes._save(path, [routes.WriteField(key=c["key"], value=c["to"]) for c in changes], [])
        out.update(undo_token=saved["undo_token"], undo_unavailable=saved["undo_unavailable"],
                   warnings=saved["warnings"])
    return out


@router.post("/shift-dates")
def shift_dates(req: ShiftRequest):
    results = [_item(p, lambda p=p: _shift_one(p, req)) for p in _paths(req.paths)]
    return {"ok": all(r["ok"] for r in results), "preview": req.preview, "results": results,
            "undo_tokens": [r["undo_token"] for r in results if r.get("undo_token")]}


# ─────────────────────────── copy tags between files ───────────────────────────

class CopyRequest(BaseModel):
    source: str
    targets: list[str] = Field(min_length=1)
    keys: list[str] | None = None            # default: every copyable field of the source
    preview: bool = True


def _copy_one(source_fields: dict[str, MetadataField], target: str, req: CopyRequest) -> dict[str, Any]:
    fpath = routes._valid_file(target)
    with PATH_LOCKS.reading(fpath):
        ctx = stable_read(fpath, lambda: routes._read_full_record(fpath))
    schema = {f.key: f for f in ctx.record.fields if f.editable}
    schema.update({f.key: f for f in ctx.record.addable_fields})
    handler = ctx.handler_of(routes.FORMAT)
    writes, skipped = [], []
    for key, field in source_fields.items():
        if key not in schema:
            skipped.append({"key": key, "reason": "the target has no such field"})
            continue
        target_field = schema[key]
        if target_field.field_type != field.field_type or bool(target_field.multi) != bool(field.multi):
            skipped.append({"key": key, "reason": "the type of the field is different"})
            continue
        try:
            handler.canonical(target_field, field.value)
        except Exception as exc:                           # a value the target cannot hold
            skipped.append({"key": key, "reason": f"the value is not accepted ({type(exc).__name__})"})
            continue
        writes.append({"key": key, "value": field.value})
    out: dict[str, Any] = {"will_copy": [w["key"] for w in writes], "skipped": skipped, "undo_token": None}
    if not req.preview and writes:
        saved = routes._save(target, [routes.WriteField(key=w["key"], value=w["value"]) for w in writes], [])
        out.update(undo_token=saved["undo_token"], undo_unavailable=saved["undo_unavailable"],
                   warnings=saved["warnings"])
    return out


@router.post("/copy-tags")
def copy_tags(req: CopyRequest):
    source = routes._valid_file(req.source)
    with PATH_LOCKS.reading(source):
        record = stable_read(source, lambda: routes._read_full_record(source)).record
    wanted = set(req.keys) if req.keys else None
    fields = {f.key: f for f in record.fields
              if not routes._is_filesystem_key(f.key) and f.field_type != "binary" and not f.computed
              and (wanted is None or f.key in wanted)}
    results = [_item(t, lambda t=t: _copy_one(fields, t, req)) for t in _paths(req.targets)]
    return {"ok": all(r["ok"] for r in results), "preview": req.preview, "results": results,
            "undo_tokens": [r["undo_token"] for r in results if r.get("undo_token")]}


# ─────────────────────────── export ───────────────────────────

_FORMULA = re.compile(r"^[=+\-@\t\r]")


def _cell(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False) if isinstance(value, (list, dict)) else \
        ("" if value is None else str(value))
    return "'" + text if _FORMULA.match(text) else text


@router.get("/export")
def export_folder(path: str = Query(..., description="Absolute directory path"),
                  format: str = Query("json", pattern="^(json|csv)$"), recursive: bool = False):
    try:
        folder = validate_directory_path(path, must_exist=True)
    except PathSecurityError as exc:
        raise routes._path_error(exc) from exc
    files = sorted(p for p in (folder.rglob("*") if recursive else folder.iterdir())
                   if p.is_file() and not routes.is_active_transaction_dir(p.parent))
    if len(files) > MAX_EXPORT_FILES:
        raise ApiError.one(422, ApiCode.INVALID_REQUEST, f"at most {MAX_EXPORT_FILES} files can be exported at once")
    records = []
    for file in files:
        try:
            with PATH_LOCKS.reading(file):
                record = stable_read(file, lambda f=file: routes._read_full_record(f)).record
            records.append({"file": str(file.relative_to(folder)), "handler": record.handler_name,
                            "fields": {f.key: f.to_dict()["value"] for f in record.fields},
                            "warnings": record.warnings, "read_errors": record.read_errors})
        except Exception as exc:                            # one unreadable file does not stop the export
            records.append({"file": str(file.relative_to(folder)), "handler": None, "fields": {},
                            "warnings": [], "read_errors": [routes.describe_error(exc)]})
    if format == "json":
        return Response(json.dumps(records, ensure_ascii=False, indent=1), media_type="application/json",
                        headers={"Content-Disposition": 'attachment; filename="metadata.json"'})
    columns = sorted({key for r in records for key in r["fields"]})
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["file", "handler", *columns])
    for r in records:
        writer.writerow([_cell(r["file"]), _cell(r["handler"]), *(_cell(r["fields"].get(c)) for c in columns)])
    return Response("﻿" + buffer.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="metadata.csv"'})


# ─────────────────────────── rename ───────────────────────────

class RenameRequest(BaseModel):
    path: str
    template: str = Field(min_length=1, max_length=200)     # e.g. "{exif:DateTimeOriginal|%Y%m%d_%H%M%S}_{name}"
    preview: bool = True


_TOKEN = re.compile(r"\{([^{}|]+)(?:\|([^{}]*))?\}")
_BAD_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}


def _new_name(path: Path, record, template: str) -> str:
    by_key = {f.key: f for f in record.fields}

    def fill(match: re.Match) -> str:
        key, fmt = match.group(1), match.group(2)
        if key == "name":
            return path.stem
        if key == "ext":
            return path.suffix.lstrip(".")
        field = by_key.get(key)
        if field is None or field.value in (None, "", []):
            raise ApiError.one(422, ApiCode.INVALID_REQUEST, f"the file has no value for {{{key}}}")
        value = field.value[0] if isinstance(field.value, list) else field.value
        if field.field_type in ("datetime", "localdatetime"):
            stamp = values.parse_datetime(value) if field.field_type == "datetime" else values.parse_localdatetime(value)
            return stamp.strftime(fmt or "%Y%m%d_%H%M%S")
        return str(value)
    stem = _BAD_NAME.sub("_", _TOKEN.sub(fill, template)).strip(" .")
    if not stem or stem.lower() in _RESERVED:
        raise ApiError.one(422, ApiCode.INVALID_REQUEST, "the template gives an empty or reserved name")
    if "{" in stem or "}" in stem:
        raise ApiError.one(422, ApiCode.INVALID_REQUEST, "unknown or unbalanced placeholder in the template")
    return stem + path.suffix


@router.post("/rename")
def rename_file(req: RenameRequest):
    path = routes._valid_file(req.path)
    with PATH_LOCKS.reading(path):
        record = stable_read(path, lambda: routes._read_full_record(path)).record
    new = path.with_name(_new_name(path, record, req.template))
    body = {"ok": True, "preview": req.preview, "old": str(path), "new": str(new), "renamed": False}
    if new == path or req.preview:
        return body
    with routes._MUTATION_LOCK, PATH_LOCKS.writing(path):
        if new.exists():
            raise ApiError.one(409, ErrorCode.CONCURRENT_MODIFICATION, f"{new.name} already exists")
        try:
            os.link(path, new) if os.name != "nt" else os.rename(path, new)
            if os.name != "nt":
                os.unlink(path)
        except FileExistsError as exc:
            raise ApiError.one(409, ErrorCode.CONCURRENT_MODIFICATION, f"{new.name} already exists") from exc
        except OSError as exc:
            raise ApiError.one(403, ApiCode.ACCESS_DENIED, routes.describe_error(exc)) from exc
    body["renamed"] = True
    return body
