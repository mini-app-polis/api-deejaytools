"""/v1/teams: a caller's own team names."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx

CONFLICT = {
    "code": "conflict",
    "message": "You already have a team with that name.",
}


async def _create(client: httpx.AsyncClient, who: Any, identifier: str) -> dict:
    res = await client.post(
        "/v1/teams", json={"identifier": identifier}, headers=who.headers
    )
    assert res.status_code == 201, res.text
    return res.json()["data"]


async def test_create_shape_and_casing(
    client: httpx.AsyncClient, person: Callable
) -> None:
    alice = await person("alice")
    created = await _create(client, alice, "  swing  kids JV mcX ")
    assert created == {
        "id": created["id"],
        "user_id": alice.id,
        "identifier": "Swing Kids JV mcX",
        "created_at": created["created_at"],
        "updated_at": created["created_at"],
    }
    # A word with a capital anywhere keeps its casing.
    assert (await _create(client, alice, "jtSwing team"))["identifier"] == (
        "jtSwing Team"
    )


async def test_validation(client: httpx.AsyncClient, person: Callable) -> None:
    alice = await person("alice")
    for identifier in ("", "   ", "Team-X", "x" * 101, None, 5):
        res = await client.post(
            "/v1/teams", json={"identifier": identifier}, headers=alice.headers
        )
        assert res.status_code == 400, identifier
        assert res.json()["error"]["code"] == "VALIDATION_ERROR"
    # 100 after trimming is fine.
    await _create(client, alice, "  " + "x" * 100 + "  ")


async def test_list_newest_first_and_own_only(
    client: httpx.AsyncClient, person: Callable
) -> None:
    alice = await person("alice")
    bob = await person("bob")
    first = await _create(client, alice, "One")
    second = await _create(client, alice, "Two")
    await _create(client, bob, "Bobs")
    res = await client.get("/v1/teams", headers=alice.headers)
    assert res.json()["meta"] == {"version": "v1", "count": 2}
    ids = [t["id"] for t in res.json()["data"]]
    if first["created_at"] != second["created_at"]:
        assert ids == [second["id"], first["id"]]
    else:
        assert set(ids) == {first["id"], second["id"]}


async def test_duplicate_name_conflicts(
    client: httpx.AsyncClient, person: Callable
) -> None:
    alice = await person("alice")
    bob = await person("bob")
    await _create(client, alice, "Swing Kids")
    dup = await client.post(
        "/v1/teams", json={"identifier": "swing kids"}, headers=alice.headers
    )
    assert dup.status_code == 409
    assert dup.json() == {"error": CONFLICT}
    # Per user only.
    await _create(client, bob, "Swing Kids")

    other = await _create(client, alice, "Other")
    rename = await client.patch(
        f"/v1/teams/{other['id']}",
        json={"identifier": "Swing Kids"},
        headers=alice.headers,
    )
    assert rename.status_code == 409
    assert rename.json() == {"error": CONFLICT}


async def test_rename(client: httpx.AsyncClient, person: Callable) -> None:
    alice = await person("alice")
    team = await _create(client, alice, "Old")
    res = await client.patch(
        f"/v1/teams/{team['id']}",
        json={"identifier": "new name"},
        headers=alice.headers,
    )
    assert res.status_code == 200
    data = res.json()["data"]
    assert data["identifier"] == "New Name"
    assert data["created_at"] == team["created_at"]
    assert data["updated_at"] >= team["updated_at"]


async def test_other_users_team_is_not_found(
    client: httpx.AsyncClient, person: Callable
) -> None:
    alice = await person("alice")
    bob = await person("bob")
    team = await _create(client, alice, "Mine")
    not_found = {"error": {"code": "NOT_FOUND", "message": "Team not found"}}
    res = await client.patch(
        f"/v1/teams/{team['id']}", json={"identifier": "Theirs"}, headers=bob.headers
    )
    assert (res.status_code, res.json()) == (404, not_found)
    res = await client.delete(f"/v1/teams/{team['id']}", headers=bob.headers)
    assert (res.status_code, res.json()) == (404, not_found)
    # Validation runs before the lookup, as zValidator does.
    res = await client.patch(
        f"/v1/teams/{team['id']}", json={"identifier": "!"}, headers=bob.headers
    )
    assert res.json()["error"]["code"] == "VALIDATION_ERROR"


async def test_delete(client: httpx.AsyncClient, person: Callable) -> None:
    alice = await person("alice")
    team = await _create(client, alice, "Gone")
    res = await client.delete(f"/v1/teams/{team['id']}", headers=alice.headers)
    assert res.status_code == 204
    assert res.content == b""
    assert (await client.get("/v1/teams", headers=alice.headers)).json()["data"] == []


async def test_auth_required(client: httpx.AsyncClient) -> None:
    for method, path in (
        ("GET", "/v1/teams"),
        ("POST", "/v1/teams"),
        ("PATCH", "/v1/teams/x"),
        ("DELETE", "/v1/teams/x"),
    ):
        res = await client.request(method, path, json={})
        assert res.status_code == 401, path
        assert res.json()["error"]["code"] == "UNAUTHORIZED"
