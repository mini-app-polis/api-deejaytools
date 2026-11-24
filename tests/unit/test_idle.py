"""Sleeping when idle: no idle connections, and background work that holds off
while nobody is using the service (idle.py)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import pytest
from sqlalchemy.pool import NullPool

from api_deejaytools import cache_bus, database, idle
from api_deejaytools.config import Settings
from api_deejaytools.services import scheduler


def _settings(monkeypatch: pytest.MonkeyPatch, **env: str) -> Settings:
    monkeypatch.setenv("DEEJAYTOOLS_DATABASE_URL", "postgresql://user@localhost/db")
    # Production refuses to start without the feedback addresses.
    monkeypatch.setenv("FEEDBACK_FROM_EMAIL", "from@example.com")
    monkeypatch.setenv("FEEDBACK_TO_EMAIL", "to@example.com")
    for name in ("ENVIRONMENT", "SLEEP_WHEN_IDLE"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    return Settings()


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({"ENVIRONMENT": "development"}, True),
        ({"ENVIRONMENT": "production"}, False),
        ({"ENVIRONMENT": "test"}, False),
        ({"ENVIRONMENT": "development", "SLEEP_WHEN_IDLE": "false"}, False),
        ({"ENVIRONMENT": "production", "SLEEP_WHEN_IDLE": "true"}, True),
        # The vault holds every optional name, empty until it is given a value.
        ({"ENVIRONMENT": "development", "SLEEP_WHEN_IDLE": ""}, True),
    ],
)
def test_sleeping_is_on_in_development_unless_the_setting_says(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str], expected: bool
) -> None:
    assert idle.enabled(_settings(monkeypatch, **env)) is expected


def test_quiet_only_after_no_request_for_the_quiet_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    on = _settings(monkeypatch, ENVIRONMENT="development")
    idle.touch(now=1000.0)
    assert not idle.quiet(on, now=1000.0 + idle.QUIET_AFTER_SECONDS - 1)
    assert idle.quiet(on, now=1000.0 + idle.QUIET_AFTER_SECONDS + 1)
    idle.touch(now=2000.0)
    assert not idle.quiet(on, now=2000.0 + 1)


def test_never_quiet_where_sleeping_is_off(monkeypatch: pytest.MonkeyPatch) -> None:
    off = _settings(monkeypatch, ENVIRONMENT="production")
    idle.touch(now=0.0)
    assert not idle.quiet(off, now=1_000_000.0)


async def test_every_http_request_counts_as_activity() -> None:
    seen: list[str] = []

    async def app(scope: dict[str, Any], _receive: Any, _send: Any) -> None:
        seen.append(scope["type"])

    middleware = idle.ActivityMiddleware(app)
    idle.touch(now=0.0)
    await middleware({"type": "lifespan"}, None, None)
    assert idle._last_request == 0.0
    await middleware({"type": "http"}, None, None)
    assert idle._last_request > 0.0
    assert seen == ["lifespan", "http"]


def test_the_engine_keeps_no_connections_when_sleeping_is_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    on = _settings(monkeypatch, ENVIRONMENT="development")
    monkeypatch.setattr(database, "get_settings", lambda: on)
    assert database._pool_options() == {"poolclass": NullPool}


def test_the_engine_pools_where_sleeping_is_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    off = _settings(monkeypatch, ENVIRONMENT="production")
    monkeypatch.setattr(database, "get_settings", lambda: off)
    options = database._pool_options()
    assert "poolclass" not in options
    assert options["pool_size"] == 8


async def test_the_scheduler_skips_its_pass_while_quiet() -> None:
    quiet = True
    runs = 0

    async def tick() -> None:
        nonlocal runs
        runs += 1

    s = scheduler.Scheduler(1000, tick=tick, quiet=lambda: quiet)
    s._fire()
    await asyncio.sleep(0)
    assert runs == 0
    quiet = False
    s._fire()
    await asyncio.sleep(0)
    assert runs == 1


class _FakeConnection:
    def __init__(self) -> None:
        self.closed = False

    def add_termination_listener(self, _listener: Callable[..., None]) -> None:
        pass

    async def add_listener(self, _channel: str, _listener: Callable[..., None]) -> None:
        pass

    async def execute(self, *_args: Any) -> None:
        pass

    async def close(self, timeout: float | None = None) -> None:
        self.closed = True


async def _until(check: Callable[[], object], timeout: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not check():
        assert asyncio.get_running_loop().time() < deadline, "timed out"
        await asyncio.sleep(0.005)


async def test_the_cache_bus_closes_while_quiet_and_drops_the_cache_on_return(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connections: list[_FakeConnection] = []

    async def connect(*_args: Any, **_kwargs: Any) -> _FakeConnection:
        conn = _FakeConnection()
        connections.append(conn)
        return conn

    monkeypatch.setattr(cache_bus.asyncpg, "connect", connect)
    monkeypatch.setattr(cache_bus, "HEALTH_CHECK_SECONDS", 0.01)
    monkeypatch.setattr(cache_bus, "QUIET_POLL_SECONDS", 0.005)

    quiet = False
    dropped: list[str] = []
    bus = cache_bus.CacheBus("postgresql://unused", dropped.extend, lambda: quiet)
    bus.start()
    try:
        await _until(lambda: bus.connected)
        quiet = True
        await _until(lambda: connections[0].closed and not bus.connected)
        await asyncio.sleep(0.05)
        assert len(connections) == 1  # stays closed while quiet
        assert dropped == []
        quiet = False
        await _until(lambda: len(connections) == 2 and bus.connected)
        # It may have missed notifications while closed, so it drops it all.
        assert dropped == [""]
    finally:
        await bus.stop()
