"""Request limits, as deejaytools-api applies them (API.md, ADR-009).

    body limit   every path   11 MiB      413 payload_too_large
    rate limit   /v1/* only   300 / min   429 too_many_requests   per client IP
    deadline     /v1/* only   30 s        503 request_timeout
                              300 s on /v1/songs/upload/*

Pure ASGI middleware rather than ``BaseHTTPMiddleware``: the body limit has
to see the stream before the app does, and the deadline has to answer while
the handler is still running.

State is process-local (one window table, one set of background handlers),
as in deejaytools-api: the service runs as a single process.
"""

from __future__ import annotations

import asyncio
import json
import math
import time
from collections.abc import Callable
from typing import Any

from mini_app_polis import activity
from mini_app_polis.logger import LOG_FAILURE, get_logger, with_log_prefix
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .errors import error_body

logger = get_logger()

BODY_LIMIT_BYTES = 11 * 1024 * 1024
RATE_LIMIT = 300
RATE_WINDOW_SECONDS = 60.0
DEADLINE_SECONDS = 30.0
UPLOAD_DEADLINE_SECONDS = 300.0
UPLOAD_PREFIX = "/v1/songs/upload/"


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


def client_address(scope: Scope) -> str:
    """The client's address as deejaytools-api keys it: Cloudflare's header,
    then the first X-Forwarded-For hop (Railway's proxy), then "unknown"."""
    cf = _header(scope, b"cf-connecting-ip")
    if cf is not None:
        return cf
    forwarded = _header(scope, b"x-forwarded-for")
    if forwarded is not None:
        return forwarded.split(",")[0].strip()
    return "unknown"


class RateLimitMiddleware:
    """Fixed window per client address on /v1/*: 300 requests a minute."""

    def __init__(
        self,
        app: ASGIApp,
        limit: int = RATE_LIMIT,
        window_seconds: float = RATE_WINDOW_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.app = app
        self.limit = limit
        self.window = window_seconds
        self.clock = clock
        self.windows: dict[str, list[float]] = {}  # key → [window_start, count]
        self._last_prune = clock()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if not _versioned(scope):
            await self.app(scope, receive, send)
            return
        now = self.clock()
        self._prune(now)
        key = client_address(scope)
        entry = self.windows.get(key)
        if entry is None or now - entry[0] >= self.window:
            self.windows[key] = [now, 1]
        elif entry[1] >= self.limit:
            retry_after = math.ceil(entry[0] + self.window - now)
            await _send_json(
                send,
                429,
                error_body(
                    "too_many_requests", "Rate limit exceeded. Please slow down."
                ),
                [(b"retry-after", str(retry_after).encode())],
            )
            return
        else:
            entry[1] += 1
        await self.app(scope, receive, send)

    def _prune(self, now: float) -> None:
        """Drop expired windows, at most once a window, so memory stays bounded."""
        if now - self._last_prune < max(self.window, 10.0):
            return
        self._last_prune = now
        for key in [
            k for k, (start, _) in self.windows.items() if now - start >= self.window
        ]:
            del self.windows[key]


class DeadlineMiddleware:
    """Answer 503 request_timeout when a /v1/* handler overruns its deadline.

    As in deejaytools-api the handler is not cancelled: it runs on in the
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
