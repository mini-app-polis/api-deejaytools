"""A client that disconnects mid-request is a 499, not a 500 fault."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import sentry_sdk
from fastapi import FastAPI
from starlette.requests import ClientDisconnect

from api_deejaytools import errors


class _Sentry:
    """Records what the error middleware sends to Sentry."""

    def __init__(self) -> None:
        self.captured: list[BaseException] = []

    def capture_exception(self, exc: BaseException) -> str:
        self.captured.append(exc)
        return "evt-1"


def _app() -> FastAPI:
    app = FastAPI()
    errors.install_error_handlers(app)

    @app.post("/v1/gone")
    async def gone() -> dict:
        raise ClientDisconnect()

    @app.post("/v1/broken")
    async def broken() -> dict:
        raise RuntimeError("boom")

    return app


def _client(app: FastAPI) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    return httpx.AsyncClient(transport=transport, base_url="http://t")


@pytest.fixture
def sentry(monkeypatch: pytest.MonkeyPatch) -> _Sentry:
    fake = _Sentry()
    monkeypatch.setattr(errors, "sentry_sdk", fake)
    return fake


async def test_a_client_disconnect_is_499_and_not_sent_to_sentry(
    sentry: _Sentry,
) -> None:
    async with _client(_app()) as c:
        res = await c.post("/v1/gone")
    assert res.status_code == 499
    assert res.json()["error"]["code"] == "CLIENT_CLOSED_REQUEST"
    assert sentry.captured == []


async def test_any_other_exception_is_still_a_reported_500(sentry: _Sentry) -> None:
    async with _client(_app()) as c:
        res = await c.post("/v1/broken")
    assert res.status_code == 500
    assert res.json()["error"]["code"] == "INTERNAL"
    assert [type(e) for e in sentry.captured] == [RuntimeError]


def test_sentry_drops_client_disconnects_only(monkeypatch: pytest.MonkeyPatch) -> None:
    scope = sentry_sdk.get_global_scope()
    monkeypatch.setattr(scope, "_error_processors", [])
    errors.ignore_client_disconnects()

    def run(exc: BaseException) -> Any:
        hint = {"exc_info": (type(exc), exc, None)}
        return scope.run_error_processors({"message": "x"}, hint)

    assert run(ClientDisconnect()) is None
    assert run(RuntimeError("boom")) == {"message": "x"}
