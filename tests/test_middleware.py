"""Body limit, rate limit and deadline, with deejaytools-api's answers."""

from __future__ import annotations

import asyncio

import httpx
from fastapi import FastAPI, Request

from api_deejaytools.cache import TtlCache
from api_deejaytools.middleware import (
    BodyLimitMiddleware,
    DeadlineMiddleware,
    RateLimitMiddleware,
)


class Clock:
    """A clock tests move by hand."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    )


def _echo_app() -> FastAPI:
    app = FastAPI()

    @app.post("/v1/echo")
    async def echo(request: Request) -> dict:
        return {"bytes": len(await request.body())}

    @app.get("/v1/ping")
    async def ping() -> dict:
        return {"ok": True}

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok"}

    return app


async def test_body_limit_by_declared_length() -> None:
    app = _echo_app()
    app.add_middleware(BodyLimitMiddleware, limit=10)
    async with _client(app) as c:
        assert (await c.post("/v1/echo", content=b"x" * 10)).json() == {"bytes": 10}
        res = await c.post("/v1/echo", content=b"x" * 11)
    assert res.status_code == 413
    assert res.json() == {
        "error": {
            "code": "payload_too_large",
            "message": "Request body exceeds the 11 MB limit.",
        }
    }


async def test_body_limit_on_a_stream_without_length() -> None:
    app = _echo_app()
    app.add_middleware(BodyLimitMiddleware, limit=10)

    async def chunks(n: int):
        for _ in range(n):
            yield b"xxxx"

    async with _client(app) as c:
        ok = await c.post("/v1/echo", content=chunks(2))
        too_big = await c.post("/v1/echo", content=chunks(3))
    assert ok.json() == {"bytes": 8}
    assert too_big.status_code == 413


async def test_rate_limit_per_forwarded_address() -> None:
    clock = Clock()
    app = _echo_app()
    app.add_middleware(RateLimitMiddleware, limit=2, window_seconds=60, clock=clock)
    a = {"x-forwarded-for": "10.0.0.1, 172.16.0.1"}
    b = {"x-forwarded-for": "10.0.0.2"}
    async with _client(app) as c:
        assert (await c.get("/v1/ping", headers=a)).status_code == 200
        assert (await c.get("/v1/ping", headers=a)).status_code == 200
        limited = await c.get("/v1/ping", headers=a)
        other = await c.get("/v1/ping", headers=b)
        health = [await c.get("/health", headers=a) for _ in range(3)]
        clock.now += 60
        after = await c.get("/v1/ping", headers=a)

    assert limited.status_code == 429
    assert limited.headers["retry-after"] == "60"
    assert limited.json()["error"] == {
        "code": "too_many_requests",
        "message": "Rate limit exceeded. Please slow down.",
    }
    assert other.status_code == 200  # keyed on the first hop
    assert all(r.status_code == 200 for r in health)  # outside /v1
    assert after.status_code == 200  # new window


async def test_deadline_answers_503_and_lets_the_handler_finish() -> None:
    finished = asyncio.Event()
    app = FastAPI()

    @app.get("/v1/slow")
    async def slow() -> dict:
        await asyncio.sleep(0.2)
        finished.set()
        return {"late": True}

    @app.get("/v1/songs/upload/slow")
    async def slow_upload() -> dict:
        await asyncio.sleep(0.2)
        return {"ok": True}

    app.add_middleware(DeadlineMiddleware, default_seconds=0.05, upload_seconds=1)
    async with _client(app) as c:
        res = await c.get("/v1/slow")
        upload = await c.get("/v1/songs/upload/slow")
    assert res.status_code == 503
    assert res.json() == {
        "error": {"code": "request_timeout", "message": "Request timed out after 50ms"}
    }
    await asyncio.wait_for(finished.wait(), 1)  # not cancelled, as in Node
    assert upload.json() == {"ok": True}  # the longer upload budget


def test_ttl_cache_expiry_and_prefix_invalidation() -> None:
    clock = Clock()
    cache = TtlCache(clock=clock)
    cache.set("queue:s1:active", 1, 3)
    cache.set("queue:s2:active", 2, 3)
    assert cache.get("queue:s1:active") == 1
    cache.invalidate_prefix("queue:s1:")
    assert cache.get("queue:s1:active") is None
    clock.now += 4
    assert cache.get("queue:s2:active") is None
