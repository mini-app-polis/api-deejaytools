"""GET /v1/divisions: the division list, public, from domain.py."""

from __future__ import annotations

import httpx

from api_deejaytools.domain import DIVISION_GROUPS, DIVISIONS


async def test_the_division_list_is_public_and_comes_from_domain(
    client: httpx.AsyncClient,
) -> None:
    res = await client.get("/v1/divisions")
    assert res.status_code == 200, res.text
    assert res.json() == {
        "data": {
            "groups": [list(group) for group in DIVISION_GROUPS],
            "divisions": list(DIVISIONS),
        },
        "meta": {"version": "v1"},
    }


async def test_a_token_is_not_needed_or_checked(client: httpx.AsyncClient) -> None:
    res = await client.get(
        "/v1/divisions", headers={"Authorization": "Bearer not-a-real-token"}
    )
    assert res.status_code == 200
