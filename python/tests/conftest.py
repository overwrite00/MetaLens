import sys
from pathlib import Path

# Make sure `python/` is in sys.path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from main import app


@pytest.fixture(autouse=True)
def _keep_sidecar_token():
    # monkeypatch.setattr su starlette State (attributi in `_state`, non in
    # `__dict__`) con pytest >= 9.2 cancella l'attributo al termine del test
    # invece di rimettere il valore precedente: lo ripristina questa fixture.
    previous = app.state.sidecar_token
    yield
    app.state.sidecar_token = previous


@pytest_asyncio.fixture
async def client(monkeypatch):
    monkeypatch.setattr(app.state, "sidecar_token", "test-session-secret", raising=False)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
        headers={"Authorization": "Bearer test-session-secret"},
    ) as c:
        yield c


SAMPLES = Path(__file__).parent / "samples"
