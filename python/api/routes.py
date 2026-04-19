from __future__ import annotations
import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from config import settings
from core import handlers as _handlers_pkg  # noqa: F401 — triggers handler registration
from core.registry import HandlerRegistry
from core.diff import compute_diff

router = APIRouter()


# ──────────────────────────────── /health ────────────────────────────────────

@router.get("/health")
def health():
    return {"status": "ok", "version": settings.version, "app": settings.app_name}


# ──────────────────────────────── /list ──────────────────────────────────────

@router.get("/list")
def list_directory(path: str = Query(..., description="Absolute directory path")):
    dirpath = Path(path)
    if not dirpath.is_dir():
        raise HTTPException(status_code=400, detail=f"Not a directory: {path}")

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
            pass
    return {"path": str(dirpath), "items": items}


# ──────────────────────────────── /read ──────────────────────────────────────

@router.get("/read")
def read_metadata(path: str = Query(..., description="Absolute file path")):
    fpath = Path(path)
    if not fpath.is_file():
        raise HTTPException(status_code=404, detail=f"File not found: {path}")
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

    return record.to_dict()


# ──────────────────────────────── /write ─────────────────────────────────────

class WriteRequest(BaseModel):
    path: str
    fields: list[dict[str, Any]]


@router.post("/write")
def write_metadata(req: WriteRequest):
    fpath = Path(req.path)
    if not fpath.is_file():
        raise HTTPException(status_code=404, detail=f"File not found: {req.path}")

    try:
        handler = HandlerRegistry.get(fpath)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"No handler for {fpath.suffix}")

    if not handler.read(fpath).supports_write:
        raise HTTPException(status_code=422, detail="Handler does not support write for this format")

    from core.models import MetadataField
    fields = [
        MetadataField(
            key=f["key"], label=f.get("label", f["key"]),
            value=f["value"], field_type=f.get("field_type", "str"),
            source=f.get("source", "unknown"),
        )
        for f in req.fields
    ]
    try:
        handler.write(fpath, fields)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    return {"ok": True, "errors": []}


# ──────────────────────────────── /delete ────────────────────────────────────

class DeleteRequest(BaseModel):
    path: str
    keys: list[str]


@router.post("/delete")
def delete_metadata(req: DeleteRequest):
    fpath = Path(req.path)
    if not fpath.is_file():
        raise HTTPException(status_code=404, detail=f"File not found: {req.path}")

    try:
        handler = HandlerRegistry.get(fpath)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"No handler for {fpath.suffix}")

    if not handler.read(fpath).supports_delete:
        raise HTTPException(status_code=422, detail="Handler does not support delete for this format")

    try:
        handler.delete(fpath, req.keys)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    return {"ok": True, "errors": []}


# ──────────────────────────────── /diff ──────────────────────────────────────

@router.get("/diff")
def diff_files(
    a: str = Query(..., description="Absolute path to file A"),
    b: str = Query(..., description="Absolute path to file B"),
):
    path_a, path_b = Path(a), Path(b)
    for p in (path_a, path_b):
        if not p.is_file():
            raise HTTPException(status_code=404, detail=f"File not found: {p}")

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
