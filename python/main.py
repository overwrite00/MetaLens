"""MetaLens Python sidecar — FastAPI app.

Started by Electron main process. Listens on 127.0.0.1 only.
Port is passed as first CLI argument (or defaults to 57321).
"""
from __future__ import annotations
import sys

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from config import settings
from api.routes import router

app = FastAPI(
    title=settings.app_name,
    version=settings.version,
    docs_url="/docs" if settings.debug else None,
    redoc_url=None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # sidecar è localhost-only, nessun rischio CORS
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 57321
    print(f"[MetaLens sidecar] starting on 127.0.0.1:{port}", flush=True)
    uvicorn.run(
        app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        access_log=False,
    )
