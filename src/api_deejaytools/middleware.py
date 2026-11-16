"""Request limits (docs/API.md, ADR-0010, ADR-0013).

    body limit   every path   11 MiB      413 payload_too_large
    rate limit   /v1/* only   300 / min   429 too_many_requests   per caller
                 /v1/feedback 5 / min     (as well as the 300)
    deadline     /v1/* only   30 s        503 request_timeout
                              300 s on /v1/songs/upload/*

The rate limit's caller is the verified user for a signed-in request and the
client address for an anonymous one; an anonymous request to a public cached
read (``PUBLIC_READS``) is not counted (docs/OPERATIONS.md, "Rate limit").
Its windows are kept in Postgres (``PostgresWindows``), shared by every
worker and replica (ADR-0014).

Pure ASGI middleware rather than ``BaseHTTPMiddleware``: the body limit has
to see the stream before the app does, and the deadline has to answer while
the handler is still running.

The deadline's background handlers are process-local: each worker keeps its
own.
"""

from __future__ import annotations

import asyncio
import json
import math
import re
import time
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from ipaddress import IPv4Address, IPv6Address, ip_address, ip_network
from typing import Any

from identity.types import VerifiedSubject
from mini_app_polis import activity
from mini_app_polis.logger import LOG_FAILURE, LOG_WARNING, get_logger, with_log_prefix
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .auth import verified_credential
from .database import get_engine
from .errors import error_body

logger = get_logger()

BODY_LIMIT_BYTES = 11 * 1024 * 1024
RATE_LIMIT = 300
#: Routes with a lower per-caller limit of their own, on top of the global one.
#: Feedback is public and each valid request sends one email.
PATH_RATE_LIMITS = {"/v1/feedback": 5}
#: The public cached reads (GET): an anonymous request to one is not counted.
#: A whole event polls them from one venue address (ADR-0013); the response
#: cache, not a limit, is what keeps them off the database.
PUBLIC_READS = re.compile(
    r"/v1/(divisions|sessions(/[^/]+)?|queue/[^/]+/(active|waiting))"
)
RATE_WINDOW_SECONDS = 60.0
DEADLINE_SECONDS = 30.0
UPLOAD_DEADLINE_SECONDS = 300.0
UPLOAD_PREFIX = "/v1/songs/upload/"

# Cloudflare's published edge ranges (https://www.cloudflare.com/ips/). Only
# a request whose peer is in one of them came through Cloudflare; from any
# other peer, cf-connecting-ip is the client's own claim.
CLOUDFLARE_NETWORKS = tuple(
    ip_network(cidr)
    for cidr in (
        "173.245.48.0/20",
        "103.21.244.0/22",
        "103.22.200.0/22",
        "103.31.4.0/22",
        "141.101.64.0/18",
        "108.162.192.0/18",
        "190.93.240.0/20",
        "188.114.96.0/20",
        "197.234.240.0/22",
        "198.41.128.0/17",
        "162.158.0.0/15",
        "104.16.0.0/13",
        "104.24.0.0/14",
        "172.64.0.0/13",
        "131.0.72.0/22",
        "2400:cb00::/32",
        "2606:4700::/32",
        "2803:f800::/32",
        "2405:b500::/32",
        "2405:8100::/32",
        "2a06:98c0::/29",
        "2c0f:f248::/32",
    )
)


async def _send_json(
    send: Send,
    status: int,
    body: dict[str, Any],
    headers: list[tuple[bytes, bytes]] = [],
) -> None:
    payload = json.dumps(body, separators=(",", ":")).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(payload)).encode()),
                *headers,
            ],
        }
    )
    await send({"type": "http.response.body", "body": payload})


def _header(scope: Scope, name: bytes) -> str | None:
    for key, value in scope.get("headers", []):
        if key == name:
            return value.decode("latin-1")
    return None


def _versioned(scope: Scope) -> bool:
    return scope["type"] == "http" and scope["path"].startswith("/v1/")


class BodyLimitMiddleware:
    """Refuse a request body over 11 MiB with 413, before the app reads it.

    A declared Content-Length is checked up front. A body without one is read
    here, up to the limit, and replayed to the app.
    """

    def __init__(self, app: ASGIApp, limit: int = BODY_LIMIT_BYTES) -> None:
        self.app = app
        self.limit = limit

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        declared = _header(scope, b"content-length")
        if declared is not None:
            if declared.isdigit() and int(declared) > self.limit:
                await self._refuse(send)
                return
            await self.app(scope, receive, send)
            return

        chunks: list[bytes] = []
        size = 0
        while True:
            message = await receive()
            if message["type"] != "http.request":
                break
            chunk = message.get("body", b"")
            size += len(chunk)
            if size > self.limit:
                await self._refuse(send)
                return
            chunks.append(chunk)
            if not message.get("more_body", False):
                break

        replayed = False

        async def replay() -> Message:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {
                    "type": "http.request",
                    "body": b"".join(chunks),
                    "more_body": False,
                }
            return await receive()

        await self.app(scope, replay, send)

    @staticmethod
    async def _refuse(send: Send) -> None:
        await _send_json(
            send,
            413,
            error_body("payload_too_large", "Request body exceeds the 11 MB limit."),
        )


def _ip(value: str) -> IPv4Address | IPv6Address | None:
    try:
        return ip_address(value.strip())
    except ValueError:
        return None


def client_address(scope: Scope) -> str:
    """The client's address an anonymous request's rate limit keys on
    (docs/OPERATIONS.md, "Client address").

    The peer is the last X-Forwarded-For hop, the one Railway's edge appends
    for whoever connected to it; every earlier hop is the client's to write.
    When the peer is a Cloudflare edge, Cloudflare's cf-connecting-ip names
    the client; from any other peer that header is ignored. Without
    X-Forwarded-For, "unknown".
    """
    hops = [
        hop.strip()
        for key, value in scope.get("headers", [])
        if key == b"x-forwarded-for"
        for hop in value.decode("latin-1").split(",")
        if hop.strip()
    ]
    if not hops:
        return "unknown"
    peer = hops[-1]
    peer_ip = _ip(peer)
    if peer_ip is not None and any(peer_ip in net for net in CLOUDFLARE_NETWORKS):
        cf = _ip(_header(scope, b"cf-connecting-ip") or "")
        if cf is not None:
            return str(cf)
    return peer


class WindowStore:
    """Where the rate limit's fixed windows are counted."""

    async def take(self, key: str, limit: int, window: float, now: float) -> int | None:
        """Count one request against ``key``; None if allowed, else the
        seconds until its window ends."""
        raise NotImplementedError


class MemoryWindows(WindowStore):
    """Windows in this process's memory: one process's view only.

    For a single process (tests, a local run). Under several uvicorn workers
    each keeps its own, so each limit would be that many times higher.
    """

    def __init__(self) -> None:
        self.windows: dict[str, list[float]] = {}  # key → [window_start, count]
        self._last_prune: float | None = None

    async def take(self, key: str, limit: int, window: float, now: float) -> int | None:
        """Count one request against ``key`` in this process's windows; None
        if allowed, else the seconds until its window ends."""
        self._prune(window, now)
        entry = self.windows.get(key)
        if entry is None or now - entry[0] >= window:
            self.windows[key] = [now, 1]
        elif entry[1] >= limit:
            return math.ceil(entry[0] + window - now)
        else:
            entry[1] += 1
        return None

    def clear(self) -> None:
        """Forget every window, so each limit starts afresh (between tests)."""
        self.windows.clear()

    def _prune(self, window: float, now: float) -> None:
        """Drop expired windows, at most once a window, so memory stays bounded."""
        if self._last_prune is not None and now - self._last_prune < max(window, 10.0):
            return
        self._last_prune = now
        for key in [
            k for k, (start, _) in self.windows.items() if now - start >= window
        ]:
            del self.windows[key]


class PostgresWindows(WindowStore):
    """Windows in ``rate_limit_windows`` (migration 013), shared by every
    worker and replica (ADR-0014).

    One statement per count: an upsert that starts a new window when the
    old one has ended and otherwise adds one, returning the window. Every
    request in a window is counted, refused ones too, so the answer is
    ``count <= limit``; a fixed window refuses the same requests either way.
    ``now`` is wall-clock epoch seconds, the same in every process.

    If the database cannot be reached the request is let through: a limit
    is not worth an outage, and the request would most likely fail on the
    database anyway. Expired windows are deleted at most once a window per
    process.
    """

    _TAKE = text(
        """
        INSERT INTO rate_limit_windows AS w (key, window_start, count)
        VALUES (:key, :now, 1)
        ON CONFLICT (key) DO UPDATE SET
          window_start = CASE WHEN :now - w.window_start >= :window
                              THEN :now ELSE w.window_start END,
          count = CASE WHEN :now - w.window_start >= :window
                       THEN 1 ELSE w.count + 1 END
        RETURNING window_start, count
        """
    )
    _PRUNE = text("DELETE FROM rate_limit_windows WHERE window_start <= :before")

    def __init__(self, engine: Callable[[], AsyncEngine] | None = None) -> None:
        self._engine = engine if engine is not None else get_engine
        self._last_prune: float | None = None

    async def take(self, key: str, limit: int, window: float, now: float) -> int | None:
        """Count one request against ``key`` in ``rate_limit_windows``; None
        if allowed (or the database is unreachable), else the seconds until
        its window ends."""
        try:
            async with self._engine().begin() as conn:
                if self._last_prune is None or now - self._last_prune >= window:
                    self._last_prune = now
                    await conn.execute(self._PRUNE, {"before": now - window})
                start, count = (
                    await conn.execute(
                        self._TAKE, {"key": key, "now": now, "window": window}
                    )
                ).one()
        except Exception as exc:  # noqa: BLE001 - fail open, logged
            logger.warning(
                with_log_prefix(LOG_WARNING, f"rate_limit_store_unavailable: {exc!r}")
            )
            return None
        if count <= limit:
            return None
        return max(1, math.ceil(start + window - now))


class RateLimitMiddleware:
    """Fixed window per caller on /v1/*: 300 requests a minute, and for the
    paths in ``path_limits`` a lower limit of their own.

    The caller is the verified user when the request carries a credential
    that verifies, so a forged or expired token gets the address's window,
    never a fresh one; otherwise it is the client address. An anonymous
    request to a public cached read is not counted. ``verify`` is
    ``auth.verified_credential``: the route reuses its outcome rather than
    verifying the credential a second time.

    ``store`` holds the windows. The app passes ``PostgresWindows`` so the
    limits hold across workers and replicas (ADR-0014); the default,
    ``MemoryWindows``, is one process's view, for tests.
    """

    def __init__(
        self,
        app: ASGIApp,
        limit: int = RATE_LIMIT,
        window_seconds: float = RATE_WINDOW_SECONDS,
        clock: Callable[[], float] = time.time,
        path_limits: dict[str, int] | None = None,
        verify: Callable[
            [str | None], AbstractAsyncContextManager[VerifiedSubject | None]
        ] = verified_credential,
        store: WindowStore | None = None,
    ) -> None:
        self.app = app
        self.limit = limit
        self.path_limits = PATH_RATE_LIMITS if path_limits is None else path_limits
        self.window = window_seconds
        self.clock = clock
        self.verify = verify
        self.store = store if store is not None else MemoryWindows()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if not _versioned(scope):
            await self.app(scope, receive, send)
            return
        async with self.verify(_header(scope, b"authorization")) as subject:
            if subject is not None:
                key = f"user {subject.issuer} {subject.subject}"
            elif scope["method"] == "GET" and PUBLIC_READS.fullmatch(scope["path"]):
                await self.app(scope, receive, send)
                return
            else:
                key = f"address {client_address(scope)}"
            now = self.clock()
            retry_after = await self.store.take(key, self.limit, self.window, now)
            path_limit = self.path_limits.get(scope["path"])
            if retry_after is None and path_limit is not None:
                retry_after = await self.store.take(
                    f"{scope['path']} {key}", path_limit, self.window, now
                )
            if retry_after is not None:
                await _send_json(
                    send,
                    429,
                    error_body(
                        "too_many_requests", "Rate limit exceeded. Please slow down."
                    ),
                    [(b"retry-after", str(retry_after).encode())],
                )
                return
            await self.app(scope, receive, send)


class DeadlineMiddleware:
    """Answer 503 request_timeout when a /v1/* handler overruns its deadline.

    The handler is not cancelled: it runs on in the
    background and whatever it writes still lands, but its response is
    discarded. A handler that has already started responding is left to
    finish.
    """

    def __init__(
        self,
        app: ASGIApp,
        default_seconds: float = DEADLINE_SECONDS,
        upload_seconds: float = UPLOAD_DEADLINE_SECONDS,
    ) -> None:
        self.app = app
        self.default_seconds = default_seconds
        self.upload_seconds = upload_seconds
        self._background: set[asyncio.Task[None]] = set()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if not _versioned(scope):
            await self.app(scope, receive, send)
            return
        seconds = (
            self.upload_seconds
            if scope["path"].startswith(UPLOAD_PREFIX)
            else self.default_seconds
        )
        started = False
        abandoned = False

        async def guarded_send(message: Message) -> None:
            nonlocal started
            if abandoned:
                return
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        task = asyncio.ensure_future(self.app(scope, receive, guarded_send))
        done, _ = await asyncio.wait({task}, timeout=seconds)
        if task in done or started:
            await task
            return

        abandoned = True
        self._background.add(task)
        task.add_done_callback(self._finished)
        ms = int(seconds * 1000)
        # For the Discord fault report of this 503.
        activity.record_fault_detail(scope, f"deadline exceeded after {ms}ms")
        await _send_json(
            send, 503, error_body("request_timeout", f"Request timed out after {ms}ms")
        )

    def _finished(self, task: asyncio.Task[None]) -> None:
        self._background.discard(task)
        if not task.cancelled() and task.exception() is not None:
            logger.error(
                with_log_prefix(
                    LOG_FAILURE,
                    f"handler failed after its deadline: {task.exception()!r}",
                )
            )
