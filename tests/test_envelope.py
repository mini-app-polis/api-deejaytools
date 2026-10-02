"""The envelope and error rendering: deejaytools-api's shapes and codes."""

from __future__ import annotations

import httpx
from fastapi import FastAPI
from pydantic import BaseModel

from api_deejaytools.errors import (
    ErrorCode,
    api_error,
    install_error_handlers,
    success,
    success_list,
)


def test_success_shape() -> None:
    assert success({"id": "x"}) == {"data": {"id": "x"}, "meta": {"version": "v1"}}


def test_success_list_shape() -> None:
    assert success_list([1, 2, 3]) == {
        "data": [1, 2, 3],
        "meta": {"version": "v1", "count": 3},
    }
    assert success_list([])["meta"] == {"version": "v1", "count": 0}


class _Body(BaseModel):
    name: str
    count: int


def _app() -> FastAPI:
    app = FastAPI()
    install_error_handlers(app)

    @app.post("/things")
    async def create(body: _Body) -> dict:
        return success(body.model_dump())

    @app.get("/teapot")
    async def teapot() -> dict:
        raise api_error(409, "conflict", "You already have a team with that name.")

    @app.get("/boom")
    async def boom() -> dict:
        raise RuntimeError("database fell over")

    return app


async def _call(method: str, path: str, **kw) -> httpx.Response:
    transport = httpx.ASGITransport(app=_app(), raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        return await c.request(method, path, **kw)


async def test_api_error_renders_code_and_message_only() -> None:
    res = await _call("GET", "/teapot")
    assert res.status_code == 409
    assert res.json() == {
        "error": {
            "code": "conflict",
            "message": "You already have a team with that name.",
        }
    }


async def test_validation_failure_is_400_validation_error() -> None:
    res = await _call("POST", "/things", json={"name": 1})
    assert res.status_code == 400
    body = res.json()
    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message"}
    assert body["error"]["code"] == ErrorCode.VALIDATION_ERROR
    assert "name:" in body["error"]["message"]
    assert "count:" in body["error"]["message"]


async def test_invalid_json_is_400_validation_error() -> None:
    res = await _call(
        "POST", "/things", content=b"{", headers={"Content-Type": "application/json"}
    )
    assert res.status_code == 400
    assert res.json()["error"]["code"] == "VALIDATION_ERROR"


async def test_unknown_path_is_404_not_found() -> None:
    res = await _call("GET", "/nope")
    assert res.status_code == 404
    assert res.json() == {"error": {"code": "NOT_FOUND", "message": "Not found"}}


async def test_wrong_method_is_404_like_hono() -> None:
    res = await _call("DELETE", "/things")
    assert res.status_code == 404
    assert res.json() == {"error": {"code": "NOT_FOUND", "message": "Not found"}}


async def test_unhandled_exception_is_500_internal(monkeypatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    res = await _call("GET", "/boom")
    assert res.status_code == 500
    assert res.json() == {
        "error": {"code": "INTERNAL", "message": "Internal server error"}
    }


async def test_unhandled_exception_message_outside_production(monkeypatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "development")
    res = await _call("GET", "/boom")
    assert res.status_code == 500
    assert res.json()["error"] == {"code": "INTERNAL", "message": "database fell over"}
