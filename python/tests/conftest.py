import sys
from pathlib import Path

# Make sure `python/` is in sys.path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from main import app


@pytest_asyncio.fixture
async def client(monkeypatch):
    monkeypatch.setattr(app.state, "sidecar_token", "test-session-secret")
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
        headers={"Authorization": "Bearer test-session-secret"},
    ) as c:
        yield c


SAMPLES = Path(__file__).parent / "samples"
