"""The rate limit's windows are shared through Postgres, so they hold across
uvicorn workers and replicas (ADR-0014, middleware.PostgresWindows).

Each worker is its own RateLimitMiddleware with its own store object, as
in production; only the database is shared.
"""

from __future__ import annotations

from typing import Any

import asyncpg
import httpx
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine

from api_deejaytools.middleware import (
    PATH_RATE_LIMITS,
    PostgresWindows,
    RateLimitMiddleware,
)

VENUE = {"x-forwarded-for": "198.51.100.7"}
FEEDBACK = "/v1/feedback"


class Clock:
    def __init__(self, now: float = 1_800_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _worker(clock: Clock, **kwargs: Any) -> httpx.AsyncClient:
    """One worker: its own app, middleware and store, on the shared database."""
    app = FastAPI()

    @app.post(FEEDBACK, status_code=201)
    async def feedback() -> dict:
        return {"ok": True}

    @app.get("/v1/ping")
    async def ping() -> dict:
        return {"ok": True}

    app.add_middleware(
        RateLimitMiddleware, store=PostgresWindows(), clock=clock, **kwargs
    )
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    )


async def test_feedback_limit_holds_across_workers(db: asyncpg.Connection) -> None:
    clock = Clock()
    workers = [_worker(clock) for _ in range(4)]
    allowed = PATH_RATE_LIMITS[FEEDBACK]
    codes = []
    # Round-robin, as uvicorn spreads connections across its workers.
    for n in range(allowed + 1):
        res = await workers[n % len(workers)].post(FEEDBACK, headers=VENUE)
        codes.append(res.status_code)
    assert codes == [201] * allowed + [429]
    for worker in workers:
        await worker.aclose()


async def test_global_limit_holds_across_workers(db: asyncpg.Connection) -> None:
    clock = Clock()
    workers = [_worker(clock, limit=3) for _ in range(3)]
    codes = [
        (await workers[n % 3].get("/v1/ping", headers=VENUE)).status_code
        for n in range(4)
    ]
    assert codes == [200, 200, 200, 429]
    for worker in workers:
        await worker.aclose()


async def test_a_new_window_starts_when_the_old_one_ends(
    db: asyncpg.Connection,
) -> None:
    clock = Clock()
    a, b = _worker(clock, limit=1), _worker(clock, limit=1)
    assert (await a.get("/v1/ping", headers=VENUE)).status_code == 200
    refused = await b.get("/v1/ping", headers=VENUE)
    assert refused.status_code == 429
    assert refused.headers["retry-after"] == "60"
    clock.now += 30
    assert (await b.get("/v1/ping", headers=VENUE)).headers["retry-after"] == "30"
    clock.now += 30
    assert (await b.get("/v1/ping", headers=VENUE)).status_code == 200
    await a.aclose()
    await b.aclose()


async def test_expired_windows_are_pruned(db: asyncpg.Connection) -> None:
    clock = Clock()
    worker = _worker(clock)
    await worker.get("/v1/ping", headers=VENUE)
    await worker.get("/v1/ping", headers={"x-forwarded-for": "203.0.113.9"})
    assert await db.fetchval("SELECT count(*) FROM rate_limit_windows") == 2
    clock.now += 61
    await worker.get("/v1/ping", headers={"x-forwarded-for": "192.0.2.1"})
    # The two expired windows went; the new one stays.
    assert await db.fetchval("SELECT count(*) FROM rate_limit_windows") == 1
    await worker.aclose()


async def test_an_unreachable_store_lets_requests_through(
    db: asyncpg.Connection,
) -> None:
    def broken() -> AsyncEngine:
        raise OSError("database down")

    app = FastAPI()

    @app.get("/v1/ping")
    async def ping() -> dict:
        return {"ok": True}

    app.add_middleware(
        RateLimitMiddleware, store=PostgresWindows(engine=broken), limit=1
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as c:
        codes = [(await c.get("/v1/ping", headers=VENUE)).status_code for _ in range(3)]
    assert codes == [200, 200, 200]
