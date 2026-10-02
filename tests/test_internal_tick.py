"""``GET /internal/tick``: the secret gate (failing closed) and the answer."""

from __future__ import annotations

import httpx
import pytest

from api_deejaytools.config import get_settings
from api_deejaytools.services import scheduler

FORBIDDEN = {"error": {"code": "FORBIDDEN", "message": "Admin access required"}}


@pytest.fixture
def ticks(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    calls: list[int] = []

    async def fake_tick() -> None:
        calls.append(1)

    monkeypatch.setattr(scheduler, "run_tick", fake_tick)
    return calls


@pytest.fixture
def secret(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setattr(get_settings(), "TICK_SECRET", "s3cret")
    return "s3cret"


async def test_matching_secret_runs_the_pass(
    client: httpx.AsyncClient, ticks: list[int], secret: str
) -> None:
    res = await client.get("/internal/tick", headers={"x-tick-secret": secret})
    assert res.status_code == 200
    assert res.json() == {"data": {"ticked": True}, "meta": {"version": "v1"}}
    assert ticks == [1]


async def test_answers_after_the_pass_finishes(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, secret: str
) -> None:
    import asyncio

    done: list[bool] = []

    async def slow() -> None:
        await asyncio.sleep(0.02)
        done.append(True)

    monkeypatch.setattr(scheduler, "run_tick", slow)
    res = await client.get("/internal/tick", headers={"x-tick-secret": secret})
    assert res.status_code == 200
    assert done == [True]


@pytest.mark.parametrize("header", [{"x-tick-secret": "wrong"}, {}])
async def test_mismatch_or_missing_header_is_forbidden(
    client: httpx.AsyncClient, ticks: list[int], secret: str, header: dict[str, str]
) -> None:
    res = await client.get("/internal/tick", headers=header)
    assert res.status_code == 403
    assert res.json() == FORBIDDEN
    assert ticks == []


@pytest.mark.parametrize("header", [{"x-tick-secret": ""}, {"x-tick-secret": "x"}, {}])
async def test_unset_secret_fails_closed(
    client: httpx.AsyncClient,
    ticks: list[int],
    monkeypatch: pytest.MonkeyPatch,
    header: dict[str, str],
) -> None:
    monkeypatch.setattr(get_settings(), "TICK_SECRET", None)
    res = await client.get("/internal/tick", headers=header)
    assert res.status_code == 403
    assert res.json() == FORBIDDEN
    assert ticks == []


async def test_empty_secret_is_a_set_secret(
    client: httpx.AsyncClient, ticks: list[int], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "TICK_SECRET", "")
    assert (await client.get("/internal/tick")).status_code == 403
    res = await client.get("/internal/tick", headers={"x-tick-secret": ""})
    assert res.status_code == 200
    assert ticks == [1]


async def test_not_rate_limited_or_versioned(
    client: httpx.AsyncClient, ticks: list[int], secret: str
) -> None:
    # 300 a minute is the /v1/* limit; this path is outside it.
    for _ in range(305):
        res = await client.get("/internal/tick", headers={"x-tick-secret": secret})
        assert res.status_code == 200
    assert (await client.get("/v1/internal/tick")).status_code == 404


async def test_runs_the_real_pass_against_the_database(
    client: httpx.AsyncClient, secret: str
) -> None:
    # No Drive configured and nothing queued: the pass completes quietly.
    res = await client.get("/internal/tick", headers={"x-tick-secret": secret})
    assert res.status_code == 200
