"""The route ledger's own rules (TEST-018), on a stand-in app."""

from __future__ import annotations

import pytest
from fastapi import FastAPI

from . import route_ledger


@pytest.fixture
def stand_in(monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    app = FastAPI()

    @app.get("/v1/things/{id}")
    async def thing(id: str) -> dict[str, str]:
        return {"id": id}

    @app.post("/v1/things")
    async def make() -> dict[str, str]:
        return {}

    monkeypatch.setattr(route_ledger, "CALLED", set())
    monkeypatch.setattr(route_ledger, "EXEMPT", {})
    return app


def test_routes_are_registered_by_method_and_template(stand_in: FastAPI) -> None:
    assert route_ledger.registered(stand_in) == {
        ("GET", "/v1/things/{id}"),
        ("POST", "/v1/things"),
    }


def test_an_uncalled_route_is_refused(stand_in: FastAPI) -> None:
    route_ledger.CALLED.add(("GET", "/v1/things/{id}"))
    assert route_ledger.problems(stand_in) == [
        "not called by any test: POST /v1/things"
    ]


def test_an_exemption_covers_an_uncalled_route_until_it_is_called(
    stand_in: FastAPI,
) -> None:
    route_ledger.CALLED.add(("GET", "/v1/things/{id}"))
    route_ledger.EXEMPT[("POST", "/v1/things")] = "a reason"
    assert route_ledger.problems(stand_in) == []
    route_ledger.CALLED.add(("POST", "/v1/things"))
    assert route_ledger.problems(stand_in) == [
        "exempt but called by a test; remove its exemption: POST /v1/things"
    ]


def test_an_exemption_for_an_unregistered_route_is_refused(stand_in: FastAPI) -> None:
    route_ledger.CALLED |= route_ledger.registered(stand_in)
    route_ledger.EXEMPT[("DELETE", "/v1/things/{id}")] = "a reason"
    assert route_ledger.problems(stand_in) == [
        "exempt but not a registered route; remove its exemption: DELETE /v1/things/{id}"
    ]
