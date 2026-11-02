"""Process-local TTL cache for hot read-only responses.

Queue reads are cached for 3 s and session reads for 5 s, so a room full of
polling phones hits the database once per window (ADR-0013). Entries are
shared: callers must not mutate what ``get`` returns.

The public reads are cached as the JSON bytes they answer with
(``get_or_build`` and ``render``), so a hit is sent as it is, without
validating and serialising it again, and concurrent misses for one key run
one build between them. Those responses also tell shared caches (Cloudflare)
to hold them for as long as this cache does (``cached_json``).

The cache is per worker process. A write drops the keys it affects here and,
through the cache bus (cache_bus.py), in every other worker.
"""

from __future__ import annotations

import asyncio
import functools
import math
import time
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import Request, Response
from pydantic import BaseModel, TypeAdapter

from . import cache_bus

QUEUE_TTL_SECONDS = 3.0
SESSION_TTL_SECONDS = 5.0


class _Flight:
    """One build in progress: the requests that missed the same key await it."""

    __slots__ = ("dropped", "future")

    def __init__(self) -> None:
        self.future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        #: Set when the key is invalidated mid-build: the result is then
        #: answered to the requests already waiting, but never stored.
        self.dropped = False


def _cancelling() -> bool:
    task = asyncio.current_task()
    return task is not None and task.cancelling() > 0


class TtlCache:
    """A dict whose entries expire, with prefix invalidation and single-flight
    builds."""

    def __init__(
        self,
        prune_every_seconds: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._store: dict[str, tuple[float, Any]] = {}
        self._flights: dict[str, _Flight] = {}
        #: Counts invalidations, so a build can tell whether one happened
        #: while it read (``set_unless_invalidated``).
        self.generation = 0
        self._clock = clock
        self._prune_every = prune_every_seconds
        self._last_prune = clock()

    def get(self, key: str) -> Any | None:
        """The live value for ``key``, or None."""
        entry = self._store.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if self._clock() > expires_at:
            del self._store[key]
            return None
        return value

    def set(self, key: str, value: Any, ttl_seconds: float) -> None:
        """Store ``value`` under ``key`` for ``ttl_seconds``."""
        now = self._clock()
        self._store[key] = (now + ttl_seconds, value)
        if now - self._last_prune >= self._prune_every:
            self._last_prune = now
            for k in [k for k, (exp, _) in self._store.items() if now > exp]:
                del self._store[k]

    def set_unless_invalidated(
        self, key: str, value: Any, ttl_seconds: float, generation: int
    ) -> None:
        """``set``, unless anything was invalidated since ``generation`` was
        read: what was read may be from before the write."""
        if self.generation == generation:
            self.set(key, value, ttl_seconds)

    async def get_or_build(
        self, key: str, build: Callable[[], Awaitable[Any]], ttl_seconds: float
    ) -> Any:
        """The live value for ``key``, or ``build()``'s, stored for
        ``ttl_seconds``.

        Concurrent misses for one key await a single build. If it raises,
        every waiting request gets the same error and nothing is stored, so
        the next request builds again. If the key is invalidated while it
        builds, the result answers the requests already waiting but is not
        stored, and later requests start a build of their own.
        """
        while True:
            hit = self.get(key)
            if hit is not None:
                return hit
            flight = self._flights.get(key)
            if flight is None:
                break
            try:
                return await asyncio.shield(flight.future)
            except asyncio.CancelledError:
                # The request building it was cancelled, not this one: go again.
                if flight.future.cancelled() and not _cancelling():
                    continue
                raise

        flight = _Flight()
        self._flights[key] = flight
        try:
            value = await build()
        except BaseException as exc:
            self._land(key, flight)
            if isinstance(exc, asyncio.CancelledError):
                flight.future.cancel()
            else:
                flight.future.set_exception(exc)
                # Retrieved here, so a build nobody else waited on is not
                # reported as an unretrieved exception.
                flight.future.exception()
            raise
        self._land(key, flight)
        if not flight.dropped:
            self.set(key, value, ttl_seconds)
        flight.future.set_result(value)
        return value

    def _land(self, key: str, flight: _Flight) -> None:
        if self._flights.get(key) is flight:
            del self._flights[key]

    def invalidate_prefix(self, prefix: str) -> None:
        """Remove every entry whose key starts with ``prefix``, and keep any
        build of such a key in progress from storing what it read."""
        self.generation += 1
        for key in [k for k in self._store if k.startswith(prefix)]:
            del self._store[key]
        for key in [k for k in self._flights if k.startswith(prefix)]:
            self._flights.pop(key).dropped = True


response_cache = TtlCache()


@functools.cache
def _adapter(model: type[BaseModel]) -> TypeAdapter[Any]:
    return TypeAdapter(model)


def render(
    model: type[BaseModel], content: Any, *, exclude_unset: bool = False
) -> bytes:
    """``content`` as the JSON bytes FastAPI sends for a route whose
    response_model is ``model``: validated, then dumped by alias."""
    adapter = _adapter(model)
    value = adapter.validate_python(content, from_attributes=True)
    return adapter.dump_json(value, by_alias=True, exclude_unset=exclude_unset)


def cache_control(request: Request, ttl_seconds: float) -> str:
    """Cache-Control for a public cached read.

    An anonymous request may be held by shared caches for as long as this
    cache holds it, and served stale for as long again while one request
    refreshes it. Browsers revalidate every time (max-age=0). A request that
    carries Authorization is never marked shareable: Cloudflare does not
    serve it from its cache, and its answer is not stored anywhere.
    """
    if "authorization" in request.headers:
        return "private, no-store"
    ttl = math.ceil(ttl_seconds)
    return f"public, max-age=0, s-maxage={ttl}, stale-while-revalidate={ttl}"


def cached_json(body: bytes, request: Request, ttl_seconds: float) -> Response:
    """Send rendered JSON as it is, with the read's Cache-Control."""
    return Response(
        content=body,
        media_type="application/json",
        headers={"Cache-Control": cache_control(request, ttl_seconds)},
    )


def drop(prefixes: list[str]) -> None:
    """Drop ``prefixes`` here and, through the cache bus, in every other
    worker process (cache_bus.py)."""
    for prefix in prefixes:
        response_cache.invalidate_prefix(prefix)
    cache_bus.publish(prefixes)


def drop_local(prefixes: list[str]) -> None:
    """Drop ``prefixes`` in this worker only: the cache bus's receiving end."""
    for prefix in prefixes:
        response_cache.invalidate_prefix(prefix)


def invalidate_queue_cache(session_id: str) -> None:
    """Invalidate every cached queue view for a session after a mutation."""
    drop([f"queue:{session_id}:"])


def invalidate_session_cache(session_id: str) -> None:
    """Invalidate a session's cached base data, every list, and its queues."""
    drop(
        [
            f"sessions:base:{session_id}",
            # A change to any session can affect every GET /v1/sessions variant.
            "sessions:list:",
            f"queue:{session_id}:",
        ]
    )


def invalidate_all_session_caches() -> None:
    """Invalidate every cached session and queue view.

    For an event change: session reads carry the event's name and timezone,
    and deleting an event deletes its sessions with their queues.
    """
    drop(["sessions:", "queue:"])
