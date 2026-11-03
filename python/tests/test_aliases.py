"""The table of alternative field names served by the sidecar matches the one the interface searches with."""
import re
from pathlib import Path

import pytest

from core.aliases import ALIASES

JS = Path(__file__).resolve().parents[2] / "frontend" / "src" / "lib" / "aliases.js"


@pytest.mark.asyncio
async def test_aliases_are_served(client):
    body = (await client.get("/aliases")).json()
    assert body["aliases"] == ALIASES and body["aliases"]["exif:DateTimeOriginal"]


def test_server_and_interface_tables_agree():
    text = JS.read_text(encoding="utf-8")
    found = {k: re.findall(r"'([^']*)'", v) for k, v in re.findall(r"^\s*'([^']+)':\s*\[(.*?)\],?\s*$", text, re.M)}
    assert found == ALIASES
