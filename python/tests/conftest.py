import sys
from pathlib import Path

# Make sure `python/` is in sys.path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from httpx import AsyncClient, ASGITransport
from main import app


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


SAMPLES = Path(__file__).parent / "samples"
