"""Body limit, rate limit and deadline, with the contract's answers."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from identity.types import VerifiedSubject
from starlette.types import Message, Receive, Scope, Send

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
    assert other.status_code == 200  # keyed on the last hop, Railway's
    assert all(r.status_code == 200 for r in health)  # outside /v1
    assert after.status_code == 200  # new window


async def test_a_path_limit_is_lower_than_the_global_one() -> None:
    clock = Clock()
    app = _echo_app()
    app.add_middleware(
        RateLimitMiddleware,
        limit=10,
        window_seconds=60,
        clock=clock,
        path_limits={"/v1/echo": 2},
    )
    a = {"x-forwarded-for": "10.0.0.1"}
    b = {"x-forwarded-for": "10.0.0.2"}
    async with _client(app) as c:
        assert (await c.post("/v1/echo", headers=a)).status_code == 200
        assert (await c.post("/v1/echo", headers=a)).status_code == 200
        limited = await c.post("/v1/echo", headers=a)
        other_address = await c.post("/v1/echo", headers=b)
        other_path = await c.get("/v1/ping", headers=a)
        clock.now += 60
        after = await c.post("/v1/echo", headers=a)

    assert limited.status_code == 429
    assert limited.json()["error"]["code"] == "too_many_requests"
    assert other_address.status_code == 200
    assert other_path.status_code == 200
    assert after.status_code == 200


async def test_feedback_has_its_own_limit_far_below_the_global_one() -> None:
    from api_deejaytools.middleware import PATH_RATE_LIMITS, RATE_LIMIT

    assert PATH_RATE_LIMITS["/v1/feedback"] <= RATE_LIMIT // 10


async def test_rate_limit_ignores_headers_the_client_writes() -> None:
    """Rotating cf-connecting-ip or the X-Forwarded-For hops before Railway's
    gets no fresh window: off Cloudflare the key is the peer Railway saw,
    and through Cloudflare it is cf-connecting-ip, which Cloudflare sets."""
    clock = Clock()
    app = _echo_app()
    app.add_middleware(RateLimitMiddleware, limit=2, window_seconds=60, clock=clock)
    direct = "203.0.113.7"  # straight to Railway, not through Cloudflare
    edge = "172.64.1.1"  # a Cloudflare edge
    async with _client(app) as c:
        spoofed = [
            await c.get(
                "/v1/ping",
                headers={
                    "cf-connecting-ip": f"198.51.100.{i}",
                    "x-forwarded-for": f"192.0.2.{i}, {direct}",
                },
            )
            for i in range(3)
        ]
        via_cloudflare = [
            await c.get(
                "/v1/ping",
                headers={
                    "cf-connecting-ip": "198.51.100.50",
                    "x-forwarded-for": f"192.0.2.{i}, 198.51.100.50, {edge}",
                },
            )
            for i in range(3)
        ]
        another_client = await c.get(
            "/v1/ping",
            headers={
                "cf-connecting-ip": "198.51.100.51",
                "x-forwarded-for": f"198.51.100.51, {edge}",
            },
        )

    assert [r.status_code for r in spoofed] == [200, 200, 429]
    assert [r.status_code for r in via_cloudflare] == [200, 200, 429]
    assert another_client.status_code == 200  # same edge, its own window


@asynccontextmanager
async def _stand_in_verify(
    authorization: str | None,
) -> AsyncIterator[VerifiedSubject | None]:
    """``auth.verified_credential`` without Clerk: ``Bearer user_…`` verifies
    as that user, any other credential fails."""
    if authorization and authorization.startswith("Bearer user_"):
        yield VerifiedSubject(
            issuer="https://clerk.test.local",
            subject=authorization[len("Bearer ") :],
            kind="human",
            claims={},
        )
    else:
        yield None


async def _ok(scope: Scope, receive: Receive, send: Send) -> None:
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b""})


VENUE = "198.51.100.7"


async def _status(
    layer: RateLimitMiddleware,
    path: str,
    *,
    method: str = "GET",
    user: str | None = None,
    token: str | None = None,
) -> int:
    """One request through the rate limit straight over ASGI, from the venue
    address; the status it was answered with."""
    headers = [(b"x-forwarded-for", VENUE.encode())]
    if user is not None or token is not None:
        headers.append((b"authorization", f"Bearer {token or user}".encode()))
    sent: list[Message] = []

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Message) -> None:
        sent.append(message)

    await layer(
        {"type": "http", "method": method, "path": path, "headers": headers},
        receive,
        send,
    )
    return int(sent[0]["status"])


async def test_an_event_behind_one_address_is_not_limited() -> None:
    """ADR-0013's steady state: 3,000 signed-in dancers on one venue address,
    each at 3 requests a minute, and the anonymous polls of the public
    reads beside them. Nobody gets a 429."""
    clock = Clock()
    layer = RateLimitMiddleware(_ok, clock=clock, verify=_stand_in_verify)
    users = [f"user_{n:04d}" for n in range(3000)]
    statuses: set[int] = set()
    for _ in range(3):
        for user in users:
            statuses.add(await _status(layer, "/v1/checkins/mine", user=user))
            statuses.add(await _status(layer, "/v1/queue/s1/active"))
        clock.now += 20
    assert statuses == {200}


async def test_an_anonymous_address_is_still_limited() -> None:
    clock = Clock()
    layer = RateLimitMiddleware(_ok, limit=3, clock=clock, verify=_stand_in_verify)
    anonymous = [await _status(layer, "/v1/events") for _ in range(4)]
    # A credential that does not verify shares the address's window: a
    # forged subject gets no fresh one.
    forged = await _status(layer, "/v1/events", token="forged.sub.token")
    signed_in = await _status(layer, "/v1/events", user="user_a")
    clock.now += 60
    after = await _status(layer, "/v1/events")
    assert anonymous == [200, 200, 200, 429]
    assert forged == 429
    assert signed_in == 200  # the user's own window, not the address's
    assert after == 200


async def test_a_signed_in_caller_is_limited_per_user() -> None:
    clock = Clock()
    layer = RateLimitMiddleware(
        _ok,
        limit=2,
        clock=clock,
        path_limits={"/v1/feedback": 1},
        verify=_stand_in_verify,
    )
    a = [await _status(layer, "/v1/runs", user="user_a") for _ in range(3)]
    b = await _status(layer, "/v1/runs", user="user_b")
    # A signed-in caller's public reads are personalised, so they count too.
    a_public = await _status(layer, "/v1/sessions/s1", user="user_a")
    anonymous = await _status(layer, "/v1/runs")
    feedback = [
        await _status(layer, "/v1/feedback", method="POST", user="user_b")
        for _ in range(2)
    ]
    feedback_other = await _status(layer, "/v1/feedback", method="POST")
    assert a == [200, 200, 429]
    assert b == 200
    assert a_public == 429
    assert anonymous == 200
    assert feedback == [200, 429]  # its own limit, per caller
    assert feedback_other == 200


async def test_only_anonymous_public_reads_go_uncounted() -> None:
    layer = RateLimitMiddleware(_ok, limit=1, clock=Clock(), verify=_stand_in_verify)
    public = [
        await _status(layer, path)
        for path in (
            "/v1/divisions",
            "/v1/sessions",
            "/v1/sessions/s1",
            "/v1/queue/s1/active",
            "/v1/queue/s1/waiting",
        )
        for _ in range(3)
    ]
    assert set(public) == {200}
    # The staff queue read, a write to a session and a nested session path
    # are not public reads: the address's one request, then 429.
    assert await _status(layer, "/v1/queue/s1/active/staff") == 200
    assert await _status(layer, "/v1/queue/s1/priority") == 429
    assert await _status(layer, "/v1/sessions", method="POST") == 429
    assert await _status(layer, "/v1/sessions/s1/divisions") == 429


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
    await asyncio.wait_for(finished.wait(), 1)  # not cancelled
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
