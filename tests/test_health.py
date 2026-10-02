"""GET /health: liveness only (API-010, ADR-009)."""

from __future__ import annotations

import httpx

from api_deejaytools.main import app


async def test_health_is_ok_without_auth_or_database() -> None:
    # No db fixture: /health must answer without touching the database.
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        res = await c.get("/health")
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}  # not wrapped in the envelope


async def test_cors_preflight_matches_deejaytools_api() -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        res = await c.options(
            "/v1/auth/me",
            headers={
                "Origin": "http://localhost:5173",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "Authorization",
            },
        )
    assert res.status_code == 200
    assert res.headers["access-control-allow-origin"] == "http://localhost:5173"
