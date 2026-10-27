"""MetaLens Python sidecar — FastAPI app.

Started by Electron main process. Listens on 127.0.0.1 only.
Port is passed as first CLI argument (or defaults to 57321).
"""
from __future__ import annotations
import sys
import os
import secrets

import uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from config import settings
from api.routes import router

app = FastAPI(
    title=settings.app_name,
    version=settings.version,
    docs_url="/docs" if settings.debug else None,
    redoc_url=None,
)
app.state.sidecar_token = os.environ.pop("METALENS_SIDECAR_TOKEN", None)
_ALLOWED_ORIGINS = {"null", "http://localhost:5173", "http://127.0.0.1:5173"}

app.add_middleware(
    CORSMiddleware,
    allow_origins=sorted(_ALLOWED_ORIGINS),
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "Content-Type"],
)


@app.middleware("http")
async def authenticate(request: Request, call_next):
    origin = request.headers.get("origin")
    if origin is not None and origin not in _ALLOWED_ORIGINS:
        return JSONResponse({"detail": "Origin not allowed"}, status_code=403)
    token = app.state.sidecar_token
    if not token:
        return JSONResponse({"detail": "Sidecar authentication is not configured"}, status_code=503)
    # CORS preflights carry no credentials and never execute an API operation.
    if request.method == "OPTIONS" and "access-control-request-method" in request.headers:
        return await call_next(request)
    supplied = request.headers.get("authorization", "")
    expected = f"Bearer {token}"
    if not secrets.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8")):
        return JSONResponse({"detail": "Invalid sidecar credentials"}, status_code=401)
    return await call_next(request)

app.include_router(router)


if __name__ == "__main__":
    if not app.state.sidecar_token:
        raise SystemExit("METALENS_SIDECAR_TOKEN must be configured")
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 57321
    print(f"[MetaLens sidecar] starting on 127.0.0.1:{port}", flush=True)
    uvicorn.run(
        app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        access_log=False,
    )
