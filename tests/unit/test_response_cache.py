"""The response cache: single-flight builds, invalidation mid-build, the
rendered bytes, and Cache-Control."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Request

from api_deejaytools.cache import TtlCache, cache_control, render
from api_deejaytools.errors import success, success_list
from api_deejaytools.routers.queue.reads import QueueListResponse
from api_deejaytools.routers.sessions import SessionResponse


class Builds:
    """A build that waits to be released and counts how often it ran."""

    def __init__(self, value: Any = b"v") -> None:
        self.calls = 0
        self.release = asyncio.Event()
        self.value = value
        self.error: BaseException | None = None

    async def __call__(self) -> Any:
        self.calls += 1
        await self.release.wait()
        if self.error is not None:
            raise self.error
        return self.value


async def _settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


async def test_concurrent_misses_share_one_build() -> None:
    cache = TtlCache()
    build = Builds()
    tasks = [
        asyncio.ensure_future(cache.get_or_build("k", build, 60)) for _ in range(50)
    ]
    await _settle()
    build.release.set()

    assert await asyncio.gather(*tasks) == [b"v"] * 50
    assert build.calls == 1
    assert cache.get("k") == b"v"
    assert await cache.get_or_build("k", build, 60) == b"v"
    assert build.calls == 1


async def test_a_failed_build_reaches_every_waiter_and_is_not_kept() -> None:
    cache = TtlCache()
    build = Builds()
    build.error = RuntimeError("database down")
    tasks = [
        asyncio.ensure_future(cache.get_or_build("k", build, 60)) for _ in range(5)
    ]
    await _settle()
    build.release.set()

    results = await asyncio.gather(*tasks, return_exceptions=True)
    assert all(isinstance(r, RuntimeError) for r in results)
    assert build.calls == 1
    assert cache.get("k") is None

    build.error = None
    assert await cache.get_or_build("k", build, 60) == b"v"
    assert build.calls == 2


async def test_an_invalidation_mid_build_keeps_its_result_out_of_the_cache() -> None:
    """A write lands while a read is building: the requests already waiting
    get what was read, but it is not stored, and a request after the write
    builds again rather than joining the stale build."""
    cache = TtlCache()
    stale = Builds(b"before")
    first = asyncio.ensure_future(cache.get_or_build("queue:s1:active", stale, 60))
    await _settle()

    cache.invalidate_prefix("queue:s1:")
    fresh = Builds(b"after")
    second = asyncio.ensure_future(cache.get_or_build("queue:s1:active", fresh, 60))
    await _settle()
    assert fresh.calls == 1

    stale.release.set()
    assert await first == b"before"
    assert cache.get("queue:s1:active") is None
    fresh.release.set()
    assert await second == b"after"
    assert cache.get("queue:s1:active") == b"after"


async def test_an_unrelated_invalidation_does_not_stop_a_build_being_kept() -> None:
    cache = TtlCache()
    build = Builds()
    task = asyncio.ensure_future(cache.get_or_build("queue:s1:active", build, 60))
    await _settle()
    cache.invalidate_prefix("queue:s2:")
    build.release.set()
    await task
    assert cache.get("queue:s1:active") == b"v"


async def test_a_cancelled_build_hands_over_to_a_waiter() -> None:
    cache = TtlCache()
    hung = Builds()
    leader = asyncio.ensure_future(cache.get_or_build("k", hung, 60))
    await _settle()
    retry = Builds(b"retried")
    retry.release.set()

    async def waiter() -> Any:
        return await cache.get_or_build("k", retry, 60)

    follower = asyncio.ensure_future(waiter())
    await _settle()
    leader.cancel()

    assert await follower == b"retried"
    with pytest.raises(asyncio.CancelledError):
        await leader
    assert cache.get("k") == b"retried"


async def test_a_cancelled_waiter_leaves_the_build_running() -> None:
    cache = TtlCache()
    build = Builds()
    leader = asyncio.ensure_future(cache.get_or_build("k", build, 60))
    await _settle()
    follower = asyncio.ensure_future(cache.get_or_build("k", build, 60))
    await _settle()
    follower.cancel()
    await _settle()
    build.release.set()

    assert await leader == b"v"
    assert follower.cancelled()
    assert cache.get("k") == b"v"


def _request(headers: dict[str, str]) -> Request:
    raw = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
    return Request({"type": "http", "headers": raw})


def test_cache_control_is_public_only_without_authorization() -> None:
    assert cache_control(_request({}), 3.0) == (
        "public, max-age=0, s-maxage=3, stale-while-revalidate=3"
    )
    assert cache_control(_request({}), 5.0).startswith("public, max-age=0, s-maxage=5")
    for value in ("Bearer token", "junk", ""):
        request = _request({"Authorization": value})
        assert cache_control(request, 5.0) == "private, no-store"


SESSION: dict[str, Any] = {
    "id": "s1",
    "event_id": None,
    "name": "Friday “late” é",
    "date": None,
    "checkin_opens_at": 1,
    "floor_trial_starts_at": 2,
    "floor_trial_ends_at": 3,
    "active_priority_max": 6,
    "active_non_priority_max": 4,
    "status": "completed",
    "created_by": None,
    "created_at": 1,
    "event_name": None,
    "event_timezone": "UTC",
    "divisions": [
        {
            "id": "d1",
            "division_name": "Classic",
            "is_priority": True,
            "sort_order": 0,
            "priority_run_limit": 2,
        }
    ],
    "queue_depth": {"priority": 0, "non_priority": 1, "active": 0},
}

QUEUE_ROW: dict[str, Any] = {
    "queueEntryId": "q1",
    "checkinId": "c1",
    "position": 1,
    "enteredQueueAt": 1,
    "entityPairId": None,
    "entitySoloUserId": "u1",
    "entityManagedPartnershipId": None,
    "entityLabel": "Ada Lovelace",
    "divisionName": "Classic",
    "songId": None,
    "songDisplayName": None,
    "songProcessedFilename": None,
    "notes": None,
    "initialQueue": "priority",
    "checkedInAt": 1,
}


async def test_rendered_bytes_are_what_fastapi_sends() -> None:
    """A hit is sent without FastAPI's own validation and serialisation, so
    what was rendered must be byte for byte what FastAPI would have sent."""
    app = FastAPI()
    session = success(SESSION)
    queue = success_list([QUEUE_ROW])

    @app.get(
        "/session", response_model=SessionResponse, response_model_exclude_unset=True
    )
    async def one() -> Any:
        return session

    @app.get("/queue", response_model=QueueListResponse)
    async def queue_read() -> Any:
        return queue

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        assert (await c.get("/session")).content == render(
            SessionResponse, session, exclude_unset=True
        )
        assert (await c.get("/queue")).content == render(QueueListResponse, queue)


def test_a_value_read_before_an_invalidation_is_not_stored() -> None:
    cache = TtlCache()
    generation = cache.generation
    cache.invalidate_prefix("sessions:")
    cache.set_unless_invalidated("sessions:list:all", "stale", 60, generation)
    assert cache.get("sessions:list:all") is None
    cache.set_unless_invalidated("sessions:list:all", "ok", 60, cache.generation)
    assert cache.get("sessions:list:all") == "ok"
