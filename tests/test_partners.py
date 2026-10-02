"""/v1/partners and /v1/pairs/find-or-create: a caller's own partners and pairs."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import asyncpg
import httpx

from .content_helpers import (
    insert_checkin,
    insert_session,
    insert_song,
    new_id,
)

PAT = {"first_name": " Pat ", "last_name": "Partner ", "partner_role": "follower"}


async def _create(client: httpx.AsyncClient, who: Any, **overrides: Any) -> dict:
    res = await client.post(
        "/v1/partners", json={**PAT, **overrides}, headers=who.headers
    )
    assert res.status_code == 201, res.text
    return res.json()["data"]


async def _pair(client: httpx.AsyncClient, who: Any, partner_id: str) -> str:
    res = await client.post(
        "/v1/pairs/find-or-create", json={"partner_id": partner_id}, headers=who.headers
    )
    assert res.status_code in (200, 201), res.text
    return res.json()["data"]["id"]


async def test_create_and_read(client: httpx.AsyncClient, person: Callable) -> None:
    alice = await person("alice")
    created = await _create(client, alice, email="pat@example.test")
    assert created == {
        "id": created["id"],
        "user_id": alice.id,
        "first_name": "Pat",
        "last_name": "Partner",
        "partner_role": "follower",
        "email": "pat@example.test",
        "linked_user_id": None,
        "created_at": created["created_at"],
        "updated_at": created["created_at"],
        "display_name": "Pat Partner",
    }
    assert isinstance(created["created_at"], int)
    one = await client.get(f"/v1/partners/{created['id']}", headers=alice.headers)
    assert one.json() == {"data": created, "meta": {"version": "v1"}}


async def test_validation(client: httpx.AsyncClient, person: Callable) -> None:
    alice = await person("alice")
    for body in (
        {**PAT, "first_name": ""},
        {**PAT, "partner_role": "lead"},
        {**PAT, "email": "nope"},
        {**PAT, "email": None},
        {"first_name": "A", "last_name": "B"},
    ):
        res = await client.post("/v1/partners", json=body, headers=alice.headers)
        assert res.status_code == 400, body
        assert res.json()["error"]["code"] == "VALIDATION_ERROR"


async def test_list_is_own_partners_by_name_without_placeholders(
    client: httpx.AsyncClient, person: Callable, db: asyncpg.Connection
) -> None:
    alice = await person("alice")
    bob = await person("bob")
    await _create(client, alice, first_name="Zed", last_name="Brown")
    await _create(client, alice, first_name="Amy", last_name="Brown")
    await _create(client, alice, first_name="Cal", last_name="Adams")
    await _create(client, bob, first_name="Bob's", last_name="Partner")
    await db.execute(
        "INSERT INTO partners (id, user_id, first_name, last_name, kind, created_at,"
        " updated_at) VALUES ('ph', $1, 'Team X', '', 'team', 1, 1)",
        alice.id,
    )
    res = await client.get("/v1/partners", headers=alice.headers)
    assert res.json()["meta"] == {"version": "v1", "count": 3}
    assert [p["display_name"] for p in res.json()["data"]] == [
        "Cal Adams",
        "Amy Brown",
        "Zed Brown",
    ]


async def test_other_users_partner_is_not_found(
    client: httpx.AsyncClient, person: Callable
) -> None:
    alice = await person("alice")
    bob = await person("bob")
    pid = (await _create(client, alice))["id"]
    not_found = {"error": {"code": "NOT_FOUND", "message": "Partner not found"}}
    for res in (
        await client.get(f"/v1/partners/{pid}", headers=bob.headers),
        await client.get(f"/v1/partners/{pid}/associations", headers=bob.headers),
        await client.patch(
            f"/v1/partners/{pid}", json={"first_name": "M"}, headers=bob.headers
        ),
        await client.delete(f"/v1/partners/{pid}", headers=bob.headers),
        await client.post(
            "/v1/pairs/find-or-create", json={"partner_id": pid}, headers=bob.headers
        ),
    ):
        assert res.status_code == 404
        assert res.json() == not_found


async def test_auth_required(client: httpx.AsyncClient) -> None:
    for method, path in (
        ("GET", "/v1/partners"),
        ("GET", "/v1/partners/leading-pairs"),
        ("POST", "/v1/partners"),
        ("GET", "/v1/partners/x"),
        ("GET", "/v1/partners/x/associations"),
        ("PATCH", "/v1/partners/x"),
        ("DELETE", "/v1/partners/x"),
        ("POST", "/v1/pairs/find-or-create"),
    ):
        # A bad body too: auth is answered first.
        res = await client.request(method, path, json={"bad": 1})
        assert res.status_code == 401, path
        assert res.json()["error"]["code"] == "UNAUTHORIZED"


async def test_patch(client: httpx.AsyncClient, person: Callable) -> None:
    alice = await person("alice")
    created = await _create(client, alice, email="pat@example.test")
    pid = created["id"]

    untouched = await client.patch(
        f"/v1/partners/{pid}", json={}, headers=alice.headers
    )
    assert untouched.json()["data"] == created

    res = await client.patch(
        f"/v1/partners/{pid}",
        json={"first_name": " Patricia ", "partner_role": "leader", "email": None},
        headers=alice.headers,
    )
    data = res.json()["data"]
    assert data["first_name"] == "Patricia"
    assert data["last_name"] == "Partner"
    assert data["partner_role"] == "leader"
    assert data["email"] is None
    assert data["display_name"] == "Patricia Partner"
    assert data["updated_at"] >= created["updated_at"]

    for field in ("first_name", "last_name"):
        res = await client.patch(
            f"/v1/partners/{pid}", json={field: "  "}, headers=alice.headers
        )
        assert res.status_code == 400
        assert res.json()["error"] == {
            "code": "BAD_REQUEST",
            "message": f"{field} cannot be empty",
        }
    res = await client.patch(
        f"/v1/partners/{pid}", json={"first_name": None}, headers=alice.headers
    )
    assert res.json()["error"]["code"] == "VALIDATION_ERROR"


async def test_find_or_create_pair(
    client: httpx.AsyncClient, person: Callable, db: asyncpg.Connection
) -> None:
    alice = await person("alice")
    pid = (await _create(client, alice))["id"]
    first = await client.post(
        "/v1/pairs/find-or-create", json={"partner_id": pid}, headers=alice.headers
    )
    assert first.status_code == 201
    assert first.json() == {
        "data": {"id": first.json()["data"]["id"]},
        "meta": {"version": "v1"},
    }
    again = await client.post(
        "/v1/pairs/find-or-create", json={"partner_id": pid}, headers=alice.headers
    )
    assert again.status_code == 200
    assert again.json()["data"]["id"] == first.json()["data"]["id"]
    row = await db.fetchrow("SELECT user_a_id, partner_b_id FROM pairs")
    assert dict(row) == {"user_a_id": alice.id, "partner_b_id": pid}

    res = await client.post(
        "/v1/pairs/find-or-create", json={"partner_id": ""}, headers=alice.headers
    )
    assert res.json()["error"]["code"] == "VALIDATION_ERROR"


async def test_leading_pairs(
    client: httpx.AsyncClient, person: Callable, db: asyncpg.Connection
) -> None:
    alice = await person("alice")
    bob = await person("bob")
    pid = (await _create(client, alice))["id"]
    pair_id = await _pair(client, alice, pid)
    await db.execute(
        "INSERT INTO partners (id, user_id, first_name, last_name, kind, created_at,"
        " updated_at) VALUES ('ph', $1, 'Team X', '', 'team', 1, 1)",
        alice.id,
    )
    await db.execute(
        "INSERT INTO pairs (id, user_a_id, partner_b_id, created_at) VALUES"
        " ('placeholder-pair', $1, 'ph', 1), ('open-pair', $1, NULL, 1)",
        alice.id,
    )
    res = await client.get("/v1/partners/leading-pairs", headers=alice.headers)
    assert res.json()["meta"]["count"] == 2
    expected = [
        {"id": "open-pair", "partner_b_id": None, "display_name": "Open slot"},
        {"id": pair_id, "partner_b_id": pid, "display_name": "Pat Partner"},
    ]
    by_id = sorted(res.json()["data"], key=lambda p: p["id"])
    assert by_id == sorted(expected, key=lambda p: p["id"])
    other = await client.get("/v1/partners/leading-pairs", headers=bob.headers)
    assert other.json()["data"] == []


async def test_associations(
    client: httpx.AsyncClient, person: Callable, db: asyncpg.Connection
) -> None:
    alice = await person("alice")
    pid = (await _create(client, alice))["id"]
    url = f"/v1/partners/{pid}/associations"
    res = await client.get(url, headers=alice.headers)
    assert res.json() == {
        "data": {
            "song_count": 0,
            "has_active_checkin": False,
            "has_checkin_history": False,
        },
        "meta": {"version": "v1"},
    }

    song = await insert_song(db, alice.id, partner_id=pid)
    await db.execute("UPDATE songs SET deleted_at = 5 WHERE id = $1", song)
    await insert_song(db, alice.id, partner_id=pid)
    pair_id = await _pair(client, alice, pid)
    session_id = await insert_session(db)
    await insert_checkin(
        db,
        session_id=session_id,
        song_id=song,
        submitted_by=alice.id,
        pair_id=pair_id,
        queued=False,
    )
    data = (await client.get(url, headers=alice.headers)).json()["data"]
    # Soft-deleted songs are counted, as in deejaytools-api.
    assert data == {
        "song_count": 2,
        "has_active_checkin": False,
        "has_checkin_history": True,
    }

    await insert_checkin(
        db, session_id=session_id, song_id=song, submitted_by=alice.id, pair_id=pair_id
    )
    data = (await client.get(url, headers=alice.headers)).json()["data"]
    assert data["has_active_checkin"] is True


async def test_delete_blocked_by_active_checkin(
    client: httpx.AsyncClient, person: Callable, db: asyncpg.Connection
) -> None:
    alice = await person("alice")
    pid = (await _create(client, alice))["id"]
    pair_id = await _pair(client, alice, pid)
    song = await insert_song(db, alice.id, partner_id=pid)
    await insert_checkin(
        db,
        session_id=await insert_session(db),
        song_id=song,
        submitted_by=alice.id,
        pair_id=pair_id,
    )
    res = await client.delete(f"/v1/partners/{pid}", headers=alice.headers)
    assert res.status_code == 409
    assert res.json()["error"] == {
        "code": "PARTNER_IN_ACTIVE_CHECKIN",
        "message": "This partner is linked to a pair with an active check-in. "
        "Complete or withdraw the check-in first.",
    }


async def test_delete_orphans_historic_pairs_and_deletes_the_rest(
    client: httpx.AsyncClient, person: Callable, db: asyncpg.Connection
) -> None:
    alice = await person("alice")
    pid = (await _create(client, alice))["id"]
    historic_pair = await _pair(client, alice, pid)
    fresh_pair = new_id("pair")
    # A second pair row for the same partner (another user's pair is not
    # possible through the API; inserted directly).
    bob = await person("bob")
    await db.execute(
        "INSERT INTO pairs (id, user_a_id, partner_b_id, created_at) VALUES ($1, $2, $3, 1)",
        fresh_pair,
        bob.id,
        pid,
    )
    song = await insert_song(db, alice.id, partner_id=pid)
    await insert_checkin(
        db,
        session_id=await insert_session(db),
        song_id=song,
        submitted_by=alice.id,
        pair_id=historic_pair,
        queued=False,
    )

    res = await client.delete(f"/v1/partners/{pid}", headers=alice.headers)
    assert res.status_code == 204
    assert res.content == b""
    assert await db.fetchval("SELECT count(*) FROM partners") == 0
    assert await db.fetchval("SELECT partner_id FROM songs WHERE id = $1", song) is None
    pairs = {r["id"]: r["partner_b_id"] for r in await db.fetch("SELECT * FROM pairs")}
    assert pairs == {historic_pair: None}
    gone = await client.get(f"/v1/partners/{pid}", headers=alice.headers)
    assert gone.status_code == 404
