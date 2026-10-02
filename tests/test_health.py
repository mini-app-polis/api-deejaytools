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


async def test_head_health_and_routing_edges_match_hono() -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        head = await c.head("/health")
        slash = await c.get("/v1/events/")
        options = await c.options("/v1/events")
    assert head.status_code == 200
    assert slash.status_code == 404  # no redirect to /v1/events
    assert slash.json() == {"error": {"code": "NOT_FOUND", "message": "Not found"}}
    assert options.status_code == 204


async def test_unhandled_500_keeps_cors_headers(monkeypatch) -> None:
    from api_deejaytools.routers import events

    def broken_select(*_a: object, **_k: object) -> None:
        raise RuntimeError("database fell over")

    monkeypatch.setattr(events, "select", broken_select)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        res = await c.get("/v1/events", headers={"Origin": "http://localhost:5173"})
    assert res.status_code == 500
    assert res.json()["error"]["code"] == "INTERNAL"
    assert res.headers["access-control-allow-origin"] == "http://localhost:5173"
