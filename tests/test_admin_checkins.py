"""/v1/admin/checkins: synthetic test check-ins."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import asyncpg
import httpx

from .queue_helpers import (
    HOUR,
    dancer_pair,
    insert_entry,
    insert_floor_session,
    now_ms,
    positions,
    queue_events,
)

BODY = {
    "divisionName": "Classic",
    "leaderFirstName": " Lead ",
    "leaderLastName": "A",
    "followerFirstName": "Follow",
    "followerLastName": " A ",
}


def _error(res: httpx.Response) -> tuple[int, str, str]:
    body = res.json()["error"]
    return res.status_code, body["code"], body["message"]


async def test_auth(client: httpx.AsyncClient, person: Callable[..., Any]) -> None:
    dancer = await person()
    assert _error(await client.post("/v1/admin/checkins", json={}))[0] == 401
    for method, path in (
        ("POST", "/v1/admin/checkins"),
        ("GET", "/v1/admin/checkins/test"),
        ("DELETE", "/v1/admin/checkins/test"),
    ):
        assert (await client.request(method, path)).status_code == 401
        res = await client.request(
            method, path, json={"sessionId": "s", **BODY}, headers=dancer.headers
        )
        assert _error(res) == (403, "FORBIDDEN", "Admin access required"), path


async def test_inject_validation_and_refusals(
    client: httpx.AsyncClient, person: Callable[..., Any], db: asyncpg.Connection
) -> None:
    admin = await person("admin", admin=True)
    for body in (
        {**BODY},
        {"sessionId": "s", **BODY, "leaderFirstName": ""},
        {"sessionId": "s", **BODY, "notes": 1},
    ):
        res = await client.post("/v1/admin/checkins", json=body, headers=admin.headers)
        assert _error(res)[:2] == (400, "VALIDATION_ERROR")
    res = await client.post(
        "/v1/admin/checkins", json={"sessionId": "nope", **BODY}, headers=admin.headers
    )
    assert _error(res) == (404, "NOT_FOUND", "Session not found")

    session_id = await insert_floor_session(db)
    res = await client.post(
        "/v1/admin/checkins",
        json={"sessionId": session_id, **BODY, "divisionName": "Masters"},
        headers=admin.headers,
    )
    assert _error(res) == (
        400,
        "BAD_REQUEST",
        "Division not configured for this session",
    )
    # As in Node, the stub rows written before admission stay.
    assert (
        await db.fetchval(
            "SELECT count(*) FROM users WHERE email LIKE 'admin-injected-%@test.local'"
        )
        == 1
    )


async def test_inject_creates_stub_rows_and_waits_for_fill(
    client: httpx.AsyncClient, person: Callable[..., Any], db: asyncpg.Connection
) -> None:
    admin = await person("admin", admin=True)
    # Active slots open and the trial running: injection still does not fill.
    session_id = await insert_floor_session(db, divisions=(("Classic", True, 0),))
    d = await dancer_pair(db)
    await insert_entry(
        db,
        session_id=session_id,
        queue_type="non_priority",
        position=1,
        song_id=d["song_id"],
        submitted_by=d["user_id"],
        pair_id=d["pair_id"],
    )
    res = await client.post(
        "/v1/admin/checkins",
        json={"sessionId": session_id, **BODY, "notes": "  "},
        headers=admin.headers,
    )
    assert res.status_code == 201
    data = res.json()["data"]
    # Priority run limit 0: admitted to non_priority.
    assert data == {
        "id": data["id"],
        "sessionId": session_id,
        "divisionName": "Classic",
        "initialQueue": "non_priority",
        "pair": {
            "id": data["pair"]["id"],
            "partner_b_id": data["pair"]["partner_b_id"],
            "display_name": "Lead A & Follow A",
        },
    }
    checkin = await db.fetchrow("SELECT * FROM checkins WHERE id = $1", data["id"])
    assert checkin["submitted_by_user_id"] == admin.id
    assert checkin["notes"] is None
    assert checkin["entity_pair_id"] == data["pair"]["id"]
    stub = await db.fetchrow(
        "SELECT u.* FROM users u JOIN pairs p ON p.user_a_id = u.id WHERE p.id = $1",
        data["pair"]["id"],
    )
    assert stub["email"] == f"admin-injected-{stub['id']}@test.local"
    assert (
        stub["first_name"],
        stub["last_name"],
        stub["display_name"],
        stub["role"],
    ) == (
        "Lead",
        "A",
        "Lead A",
        "user",
    )
    song = await db.fetchrow("SELECT * FROM songs WHERE id = $1", checkin["song_id"])
    assert (song["user_id"], song["display_name"]) == (stub["id"], "[Test Placeholder]")
    partner = await db.fetchrow(
        "SELECT * FROM partners WHERE id = $1", data["pair"]["partner_b_id"]
    )
    assert (partner["first_name"], partner["last_name"], partner["partner_role"]) == (
        "Follow",
        "A",
        "follower",
    )
    assert [p for _, p in await positions(db, session_id, "non_priority")] == [1, 2]
    assert await positions(db, session_id, "active") == []
    [event] = await queue_events(db, session_id)
    assert (
        event["action"],
        event["to_queue"],
        event["to_position"],
        event["actor_user_id"],
        event["reason"],
    ) == ("checked_in", "non_priority", 2, admin.id, "admin_test_injection")


async def test_inject_ignores_the_checkin_window(
    client: httpx.AsyncClient, person: Callable[..., Any], db: asyncpg.Connection
) -> None:
    admin = await person("admin", admin=True)
    now = now_ms()
    session_id = await insert_floor_session(
        db,
        status="completed",
        checkin_opens_at=now - 3 * HOUR,
        starts_at=now - 2 * HOUR,
        ends_at=now - HOUR,
        divisions=(("Classic", True, 1),),
    )
    res = await client.post(
        "/v1/admin/checkins",
        json={"sessionId": session_id, **BODY},
        headers=admin.headers,
    )
    assert res.status_code == 201
    assert res.json()["data"]["initialQueue"] == "priority"


async def test_list_and_delete(
    client: httpx.AsyncClient, person: Callable[..., Any], db: asyncpg.Connection
) -> None:
    admin = await person("admin", admin=True)
    assert (
        await client.get("/v1/admin/checkins/test", headers=admin.headers)
    ).json() == {
        "data": [],
        "meta": {"version": "v1", "count": 0},
    }
    res = await client.delete("/v1/admin/checkins/test", headers=admin.headers)
    assert res.json() == {"data": {"deleted": 0}, "meta": {"version": "v1"}}

    session_id = await insert_floor_session(db, priority_max=1, non_priority_max=1)
    ids = []
    for name in ("One", "Two"):
        res = await client.post(
            "/v1/admin/checkins",
            json={"sessionId": session_id, **BODY, "leaderLastName": name},
            headers=admin.headers,
        )
        ids.append(res.json()["data"])
    await db.execute(
        "UPDATE pairs SET created_at = 2 WHERE id = $1", ids[1]["pair"]["id"]
    )
    # One goes active and completes, so it has a run and is off the queue.
    entry = await db.fetchval(
        "SELECT id FROM queue_entries WHERE checkin_id = $1", ids[0]["id"]
    )
    await db.execute(
        "UPDATE queue_entries SET queue_type = 'active', position = 1 WHERE id = $1",
        entry,
    )
    res = await client.post(
        "/v1/queue/complete", json={"queueEntryId": entry}, headers=admin.headers
    )
    assert res.status_code == 200
    # A stub pair with no check-in at all, and a real (non-stub) pair.
    lonely = await db.fetchval(
        "SELECT user_a_id FROM pairs WHERE id = $1", ids[1]["pair"]["id"]
    )
    await db.execute(
        "INSERT INTO pairs (id, user_a_id, partner_b_id, created_at) VALUES ('p_lonely', $1, NULL, 1)",
        lonely,
    )
    real = await dancer_pair(db)

    res = await client.get("/v1/admin/checkins/test", headers=admin.headers)
    body = res.json()
    assert body["meta"]["count"] == 3
    rows = body["data"]
    by_pair = {r["pair_id"]: r for r in rows}
    assert rows[0]["pair_id"] == ids[0]["pair"]["id"]
    two = by_pair[ids[1]["pair"]["id"]]
    assert two == {
        "pair_id": ids[1]["pair"]["id"],
        "created_at": 2,
        "leader_name": "Lead Two",
        "follower_name": "Follow A",
        "session_id": session_id,
        "session_name": "Floor",
        "division_name": "Classic",
        "queue_status": two["queue_status"],
        "position": two["position"],
    }
    one = by_pair[ids[0]["pair"]["id"]]
    assert (one["queue_status"], one["position"]) == ("off_queue", None)
    assert by_pair["p_lonely"]["follower_name"] is None
    assert by_pair["p_lonely"]["session_id"] is None
    assert by_pair["p_lonely"]["queue_status"] == "off_queue"
    assert real["pair_id"] not in by_pair
    assert [r["created_at"] for r in rows] == sorted(
        (r["created_at"] for r in rows), reverse=True
    )

    res = await client.delete("/v1/admin/checkins/test", headers=admin.headers)
    assert res.json() == {"data": {"deleted": 2}, "meta": {"version": "v1"}}
    for table in ("checkins", "queue_entries", "runs", "queue_events"):
        assert await db.fetchval(f"SELECT count(*) FROM {table}") == 0, table
    assert (
        await db.fetchval(
            "SELECT count(*) FROM users WHERE email LIKE 'admin-injected-%'"
        )
        == 0
    )
    assert await db.fetchval("SELECT count(*) FROM pairs") == 1
    assert (await client.get("/v1/admin/checkins/test", headers=admin.headers)).json()[
        "data"
    ] == []
