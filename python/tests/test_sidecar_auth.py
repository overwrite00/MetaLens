import pytest
from httpx import ASGITransport, AsyncClient
from main import app


@pytest.mark.asyncio
async def test_sidecar_requires_token_even_for_health(monkeypatch):
    monkeypatch.setattr(app.state, "sidecar_token", "test-session-secret", raising=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/health")).status_code == 401
        assert (await client.get("/health", headers={"Authorization": "Bearer wrong"})).status_code == 401
        assert (await client.get("/health", headers={"Authorization": "Bearer test-session-secret"})).status_code == 200


@pytest.mark.asyncio
async def test_sidecar_rejects_foreign_origin_with_valid_token(monkeypatch):
    monkeypatch.setattr(app.state, "sidecar_token", "test-session-secret", raising=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/health", headers={"Origin": "https://attacker.example", "Authorization": "Bearer test-session-secret"})
        assert response.status_code == 403
        preflight = await client.options("/write", headers={"Origin": "https://attacker.example", "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "authorization,content-type"})
        assert preflight.status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", ["null", "http://localhost:5173", "http://127.0.0.1:5173"])
async def test_sidecar_allows_authorized_app_origin(monkeypatch, origin):
    monkeypatch.setattr(app.state, "sidecar_token", "test-session-secret", raising=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/health", headers={"Origin": origin, "Authorization": "Bearer test-session-secret"})
        assert response.status_code == 200
        assert response.headers["access-control-allow-origin"] == origin
        preflight = await client.options("/write", headers={"Origin": origin, "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "authorization,content-type"})
        assert preflight.status_code == 200


@pytest.mark.asyncio
async def test_sidecar_missing_configuration_fails_closed(monkeypatch):
    monkeypatch.setattr(app.state, "sidecar_token", None, raising=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/health")).status_code == 503
