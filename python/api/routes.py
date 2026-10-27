from __future__ import annotations
import hashlib
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

import importlib
from collections import OrderedDict
from threading import RLock

from config import settings
from core.registry import HandlerRegistry

# Trigger handler registration as an explicit side effect.
importlib.import_module("core.handlers")
from core.diff import compute_diff
from core.path_security import validate_file_path, validate_directory_path, secure_atomic_write, PathSecurityError

router = APIRouter()

# Dynamic metadata keys (e.g. a Vorbis custom tag) may be restored only after
# this server authorized their deletion. Never trust client permission flags.
_DELETED_FIELDS = OrderedDict()
_DELETED_FIELDS_LOCK = RLock()
_MAX_DELETED_FIELDS = 512


def _raise_path_error(path_str: str, error: PathSecurityError):
    """Convert PathSecurityError to HTTPException."""
    raise HTTPException(status_code=400, detail=f"Invalid path: {str(error)}") from error


# ──────────────────────────────── /health ────────────────────────────────────

@router.get("/health")
def health():
    return {"status": "ok", "version": settings.version, "app": settings.app_name}


# ──────────────────────────────── /hash ──────────────────────────────────────

_SUPPORTED_ALGORITHMS = {"md5", "sha1", "sha256", "sha512", "blake2b"}

@router.get("/hash")
def hash_file(
    path: str = Query(..., description="Absolute file path"),
    algorithms: str = Query("md5,sha1,sha256,sha512,blake2b", description="Comma-separated list of algorithms"),
):
    try:
        fpath = validate_file_path(path, must_exist=True)
    except PathSecurityError as e:
        _raise_path_error(path, e)

    requested = {a.strip().lower() for a in algorithms.split(",") if a.strip()}
    unknown = requested - _SUPPORTED_ALGORITHMS
    if unknown:
        raise HTTPException(status_code=400, detail=f"Unsupported algorithms: {', '.join(unknown)}")

    hashers = {alg: hashlib.new(alg) for alg in requested}

    try:
        with fpath.open("rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                for h in hashers.values():
                    h.update(chunk)
    except OSError as e:
        raise HTTPException(status_code=500, detail=str(e))

    return {
        "path": str(fpath),
        "size": fpath.stat().st_size,
        "hashes": {alg: h.hexdigest() for alg, h in hashers.items()},
    }


# ──────────────────────────────── /list ──────────────────────────────────────

@router.get("/list")
def list_directory(path: str = Query(..., description="Absolute directory path")):
    try:
        dirpath = validate_directory_path(path, must_exist=True)
    except PathSecurityError as e:
        _raise_path_error(path, e)

    items = []
    try:
        entries = sorted(dirpath.iterdir(), key=lambda e: (e.is_file(), e.name.lower()))
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except OSError as e:
        raise HTTPException(status_code=500, detail=str(e))

    for entry in entries:
        try:
            st = entry.stat()
            handler_name = "—"
            try:
                handler_name = HandlerRegistry.get(entry).NAME
            except ValueError:
                # No handler matches this entry; keep the placeholder name.
                pass
            items.append({
                "name": entry.name,
                "path": str(entry),
                "is_dir": entry.is_dir(),
                "size": st.st_size if entry.is_file() else None,
                "mtime": st.st_mtime,
                "ext": entry.suffix.lower() if entry.is_file() else "",
                "handler": handler_name,
            })
        except OSError:
            continue
    return {"path": str(dirpath), "items": items}


# ──────────────────────────────── /read ──────────────────────────────────────

@router.get("/read")
def read_metadata(path: str = Query(..., description="Absolute file path")):
    try:
        fpath = validate_file_path(path, must_exist=True)
    except PathSecurityError as e:
        _raise_path_error(path, e)
    try:
        handler = HandlerRegistry.get(fpath)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"No handler for {fpath.suffix}")

    record = handler.read(fpath)

    # Always append filesystem metadata as a secondary layer
    from core.handlers.filesystem_handler import FilesystemHandler
    fs_record = FilesystemHandler().read(fpath)
    existing_keys = {f.key for f in record.fields}
    for f in fs_record.fields:
        if f.key not in existing_keys:
            record.fields.append(f)

    record.supports_write = record.supports_write or any(f.editable for f in fs_record.fields)
    record.supports_delete = record.supports_delete or any(f.deletable for f in fs_record.fields)

    return record.to_dict()


# ──────────────────────────────── /write ─────────────────────────────────────

class WriteField(BaseModel):
    key: str = Field(min_length=1)
    value: Any


class WriteRequest(BaseModel):
    path: str
    fields: list[WriteField] = Field(min_length=1)


class DeleteRequest(BaseModel):
    path: str
    keys: list[str] = Field(min_length=1)


def _mutation_context(path_str: str):
    from core.handlers.filesystem_handler import FilesystemHandler
    try:
        path = validate_file_path(path_str, must_exist=True)
    except PathSecurityError as exc:
        _raise_path_error(path_str, exc)
    handler = HandlerRegistry.get(path)
    record = handler.read(path)
    fs_handler = handler if handler.NAME == "filesystem" else FilesystemHandler()
    fs_record = record if handler.NAME == "filesystem" else fs_handler.read(path)
    available = {field.key: field for field in record.fields}
    for field in fs_record.fields:
        available.setdefault(field.key, field)
    return path, handler, fs_handler, record, available


def _is_filesystem_key(key: str) -> bool:
    return key.startswith(("fs:", "xattr:"))


def _apply_mutation(path, callback):
    try:
        secure_atomic_write(path, callback)
    except PathSecurityError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (ValueError, TypeError, NotImplementedError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/write")
def write_metadata(req: WriteRequest):
    from dataclasses import replace
    path, handler, fs_handler, record, available = _mutation_context(req.path)
    keys = [field.key for field in req.fields]
    if len(set(keys)) != len(keys):
        raise HTTPException(status_code=422, detail="Duplicate metadata keys")
    for key in keys:
        if key not in available and not _is_filesystem_key(key) and record.supports_write and not record.read_errors:
            field = handler.writable_field(path, key)
            if field is not None:
                available[key] = field
        if key not in available:
            with _DELETED_FIELDS_LOCK:
                field = _DELETED_FIELDS.get((str(path), handler.NAME, key))
            if field is not None:
                available[key] = field
        if key not in available or not available[key].editable:
            raise HTTPException(status_code=422, detail=f"Field is unknown or read-only: {key}")
    fields = [replace(available[field.key], value=field.value) for field in req.fields]
    fs_fields = [field for field in fields if _is_filesystem_key(field.key)]
    format_fields = [field for field in fields if not _is_filesystem_key(field.key)]
    if format_fields and (not record.supports_write or record.read_errors):
        raise HTTPException(status_code=422, detail="File metadata cannot be safely modified")

    def mutate(tmp):
        if format_fields:
            handler.write(tmp, format_fields)
        if fs_fields:
            fs_handler.write(tmp, fs_fields)
    _apply_mutation(path, mutate)
    return {"ok": True, "errors": []}


@router.post("/delete")
def delete_metadata(req: DeleteRequest):
    path, handler, fs_handler, record, available = _mutation_context(req.path)
    if len(set(req.keys)) != len(req.keys):
        raise HTTPException(status_code=422, detail="Duplicate metadata keys")
    for key in req.keys:
        if key not in available or not available[key].deletable:
            raise HTTPException(status_code=422, detail=f"Field is unknown or not deletable: {key}")
    fs_keys = [key for key in req.keys if _is_filesystem_key(key)]
    format_keys = [key for key in req.keys if not _is_filesystem_key(key)]
    if format_keys and (not record.supports_delete or record.read_errors):
        raise HTTPException(status_code=422, detail="File metadata cannot be safely modified")

    def mutate(tmp):
        if format_keys:
            handler.delete(tmp, format_keys)
        if fs_keys:
            fs_handler.delete(tmp, fs_keys)
    _apply_mutation(path, mutate)
    with _DELETED_FIELDS_LOCK:
        for key in req.keys:
            cache_key = (str(path), handler.NAME, key)
            _DELETED_FIELDS[cache_key] = available[key]
            _DELETED_FIELDS.move_to_end(cache_key)
        while len(_DELETED_FIELDS) > _MAX_DELETED_FIELDS:
            _DELETED_FIELDS.popitem(last=False)
    return {"ok": True, "errors": []}


# ──────────────────────────────── /diff ──────────────────────────────────────

@router.get("/diff")
def diff_files(
    a: str = Query(..., description="Absolute path to file A"),
    b: str = Query(..., description="Absolute path to file B"),
):
    try:
        path_a = validate_file_path(a, must_exist=True)
        path_b = validate_file_path(b, must_exist=True)
    except PathSecurityError as e:
        _raise_path_error(a if "a" in str(e) else b, e)

    def _read(p: Path):
        try:
            handler = HandlerRegistry.get(p)
        except ValueError:
            raise HTTPException(status_code=422, detail=f"No handler for {p.suffix}")
        return handler.read(p)

    record_a = _read(path_a)
    record_b = _read(path_b)
    result = compute_diff(record_a, record_b)
    return result.to_dict()
