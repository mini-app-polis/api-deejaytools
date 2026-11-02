"""Cross-worker invalidation for the response cache, over Postgres NOTIFY.

The response cache (cache.py) lives in each uvicorn worker process. A write
clears it in the worker that served the write; this module tells every other
worker to clear the same keys, so a read served by another worker reflects
the write within milliseconds instead of after the cache's TTL.

Each worker holds one dedicated connection (outside the SQLAlchemy pool) that
LISTENs on ``CHANNEL`` and also sends this worker's own notifications. A
notification names the key prefixes to drop and the worker that sent it,
which ignores its own.

If the connection is lost the cache falls back to its TTL alone until the
connection is back; on reconnecting the worker drops its whole cache, since it
may have missed notifications while it was away.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from collections.abc import Callable
from typing import Any

import asyncpg
from mini_app_polis.logger import LOG_WARNING, get_logger, with_log_prefix

logger = get_logger()

CHANNEL = "deejaytools_cache"
#: How the connection shows in pg_stat_activity.
APPLICATION_NAME = "api-deejaytools-cache-bus"

#: How long the sender waits for work before checking the connection is alive.
HEALTH_CHECK_SECONDS = 30.0
#: Waits between reconnect attempts, the last repeated.
RECONNECT_BACKOFF_SECONDS = (1.0, 2.0, 5.0, 10.0, 30.0)
#: Notifications queued while the connection is down beyond this are dropped:
#: the other workers fall back to the TTL for them.
MAX_PENDING = 1000

Invalidate = Callable[[list[str]], None]


class CacheBus:
    """One worker's end of the invalidation channel."""

    def __init__(self, dsn: str, invalidate: Invalidate) -> None:
        self._dsn = dsn
        self._invalidate = invalidate
        self.origin = uuid.uuid4().hex
        self._pending: asyncio.Queue[list[str]] = asyncio.Queue(maxsize=MAX_PENDING)
        self._task: asyncio.Task[None] | None = None
        self._lost = asyncio.Event()
        #: True while the connection is up and listening.
        self.connected = False

    # -- publishing ---------------------------------------------------------

    def publish(self, prefixes: list[str]) -> None:
        """Ask every other worker to drop ``prefixes``. Never blocks or raises."""
        if self._task is None or not prefixes:
            return
        with contextlib.suppress(asyncio.QueueFull):
            self._pending.put_nowait(prefixes)

    def _payload(self, prefixes: list[str]) -> str:
        return json.dumps({"origin": self.origin, "prefixes": prefixes})

    # -- receiving ----------------------------------------------------------

    def _on_notify(self, _conn: Any, _pid: int, _channel: str, payload: str) -> None:
        try:
            message = json.loads(payload)
            if message.get("origin") == self.origin:
                return
            prefixes = [p for p in message.get("prefixes", []) if isinstance(p, str)]
        except (ValueError, AttributeError):
            logger.warning(
                with_log_prefix(
                    LOG_WARNING, f"cache_bus: unreadable payload {payload!r}"
                )
            )
            return
        self._invalidate(prefixes)

    def _on_terminate(self, _conn: Any) -> None:
        self._lost.set()

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        """Connect and run in the background, reconnecting as needed."""
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._run())

    async def stop(self) -> None:
        """Stop the background task and close the connection."""
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _run(self) -> None:
        attempt = 0
        first = True
        while True:
            try:
                conn = await asyncpg.connect(
                    self._dsn,
                    statement_cache_size=0,
                    server_settings={"application_name": APPLICATION_NAME},
                )
            except (OSError, asyncpg.PostgresError) as exc:
                wait = RECONNECT_BACKOFF_SECONDS[
                    min(attempt, len(RECONNECT_BACKOFF_SECONDS) - 1)
                ]
                attempt += 1
                logger.warning(
                    with_log_prefix(
                        LOG_WARNING,
                        f"cache_bus: connect failed ({exc!r}); retrying in {wait:.0f}s",
                    )
                )
                await asyncio.sleep(wait)
                continue
            attempt = 0
            try:
                self._lost.clear()
                conn.add_termination_listener(self._on_terminate)
                await conn.add_listener(CHANNEL, self._on_notify)
                self.connected = True
                if not first:
                    # Notifications sent while this worker was away are lost.
                    self._invalidate([""])
                first = False
                await self._send_until_lost(conn)
            except (OSError, asyncpg.PostgresError, asyncpg.InterfaceError) as exc:
                logger.warning(
                    with_log_prefix(
                        LOG_WARNING, f"cache_bus: connection lost ({exc!r})"
                    )
                )
            finally:
                self.connected = False
                with contextlib.suppress(Exception):
                    await conn.close(timeout=2)

    async def _send_until_lost(self, conn: asyncpg.Connection) -> None:
        lost = asyncio.ensure_future(self._lost.wait())
        try:
            while True:
                get = asyncio.ensure_future(self._pending.get())
                await asyncio.wait(
                    {get, lost},
                    timeout=HEALTH_CHECK_SECONDS,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if lost.done():
                    get.cancel()
                    if get.done() and not get.cancelled():
                        # Taken just as the connection went: send it next time.
                        with contextlib.suppress(asyncio.QueueFull):
                            self._pending.put_nowait(get.result())
                    raise asyncpg.InterfaceError("connection terminated")
                if not get.done():
                    get.cancel()
                    await conn.execute("SELECT 1")
                    continue
                await conn.execute(
                    "SELECT pg_notify($1, $2)", CHANNEL, self._payload(get.result())
                )
        finally:
            lost.cancel()


_bus: CacheBus | None = None


def start(dsn: str, invalidate: Invalidate) -> CacheBus:
    """Start this worker's bus (from the app's lifespan)."""
    global _bus
    _bus = CacheBus(dsn, invalidate)
    _bus.start()
    return _bus


async def stop() -> None:
    """Stop this worker's bus, if it was started."""
    global _bus
    bus, _bus = _bus, None
    if bus is not None:
        await bus.stop()


def publish(prefixes: list[str]) -> None:
    """Tell the other workers to drop ``prefixes``; a no-op before start."""
    if _bus is not None:
        _bus.publish(prefixes)
