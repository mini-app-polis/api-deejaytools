"""Process-local TTL cache for hot read-only responses (deejaytools-api lib/cache.ts).

Queue reads are cached for 3 s and the shared part of session reads for 5 s,
so a room full of polling phones hits the database once per window. Entries
are shared: callers must not mutate what ``get`` returns.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

QUEUE_TTL_SECONDS = 3.0
SESSION_TTL_SECONDS = 5.0


class TtlCache:
    """A dict whose entries expire, with prefix invalidation."""

    def __init__(
        self,
        prune_every_seconds: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._store: dict[str, tuple[float, Any]] = {}
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

    def invalidate_prefix(self, prefix: str) -> None:
        """Remove every entry whose key starts with ``prefix``."""
        for key in [k for k in self._store if k.startswith(prefix)]:
            del self._store[key]


response_cache = TtlCache()


def invalidate_queue_cache(session_id: str) -> None:
    """Invalidate every cached queue view for a session after a mutation."""
    response_cache.invalidate_prefix(f"queue:{session_id}:")


def invalidate_session_cache(session_id: str) -> None:
    """Invalidate a session's cached base data, every list, and its queues."""
    response_cache.invalidate_prefix(f"sessions:base:{session_id}")
    # A change to any session can affect every GET /v1/sessions variant.
    response_cache.invalidate_prefix("sessions:list:")
    invalidate_queue_cache(session_id)
