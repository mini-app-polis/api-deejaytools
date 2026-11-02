"""The cache bus: cross-worker invalidation of the response cache over NOTIFY."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import asyncpg

from api_deejaytools.cache_bus import APPLICATION_NAME, CacheBus


async def _until(check: Callable[[], object], timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not check():
        assert asyncio.get_running_loop().time() < deadline, "timed out"
        await asyncio.sleep(0.02)


async def test_a_write_reaches_every_other_worker_but_not_its_own(
    database: str,
) -> None:
    a_got: list[str] = []
    b_got: list[str] = []
    a = CacheBus(database, a_got.extend)
    b = CacheBus(database, b_got.extend)
    a.start()
    b.start()
    try:
        await _until(lambda: a.connected and b.connected)
        a.publish(["sessions:list:", "queue:s1:"])
        await _until(lambda: b_got)
        assert b_got == ["sessions:list:", "queue:s1:"]
        await asyncio.sleep(0.2)
        assert a_got == []
    finally:
        await a.stop()
        await b.stop()


async def test_a_worker_that_lost_its_connection_reconnects_and_drops_everything(
    database: str, db: asyncpg.Connection
) -> None:
    got: list[str] = []
    bus = CacheBus(database, got.extend)
    sender = CacheBus(database, lambda _prefixes: None)
    bus.start()
    sender.start()
    try:
        await _until(lambda: bus.connected and sender.connected)
        await db.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity"
            " WHERE application_name = $1",
            APPLICATION_NAME,
        )
        # It may have missed notifications while away, so it drops it all.
        await _until(lambda: "" in got, timeout=10.0)
        await _until(lambda: bus.connected and sender.connected, timeout=10.0)
        got.clear()
        sender.publish(["queue:s2:"])
        await _until(lambda: got)
        assert got == ["queue:s2:"]
    finally:
        await bus.stop()
        await sender.stop()


async def test_publishing_before_start_is_a_no_op(database: str) -> None:
    bus = CacheBus(database, lambda _prefixes: None)
    bus.publish(["sessions:"])
    await bus.stop()


async def test_the_app_cache_is_wired_to_the_bus_both_ways(database: str) -> None:
    """A write here reaches another worker; another worker's write empties
    this worker's cache."""
    from api_deejaytools import cache_bus
    from api_deejaytools.cache import (
        drop_local,
        invalidate_session_cache,
        response_cache,
    )

    other_got: list[str] = []
    other = CacheBus(database, other_got.extend)
    other.start()
    this = cache_bus.start(database, drop_local)
    try:
        await _until(lambda: this.connected and other.connected)

        invalidate_session_cache("s1")
        await _until(lambda: other_got)
        assert set(other_got) == {"sessions:base:s1", "sessions:list:", "queue:s1:"}

        response_cache.set("sessions:list:all", ["stale"], 60)
        other.publish(["sessions:list:"])
        await _until(lambda: response_cache.get("sessions:list:all") is None)
    finally:
        await cache_bus.stop()
        await other.stop()
