"""/v1/runs: run history with display labels."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import asyncpg
import httpx

from .content_helpers import insert_event, insert_song
from .queue_helpers import (
    dancer_pair,
    insert_floor_session,
    insert_managed,
    insert_run,
    insert_user,
)


async def test_auth(client: httpx.AsyncClient, person: Callable[..., Any]) -> None:
    dancer = await person()
    assert (await client.get("/v1/runs")).status_code == 401
    res = await client.get("/v1/runs", headers=dancer.headers)
    assert res.status_code == 403
    assert res.json()["error"] == {
        "code": "FORBIDDEN",
        "message": "Admin access required",
    }
    # Auth is checked before the query.
    assert (await client.get("/v1/runs?limit=0")).status_code == 401


async def test_empty_list(
    client: httpx.AsyncClient, person: Callable[..., Any]
) -> None:
    admin = await person("admin", admin=True)
    res = await client.get("/v1/runs", headers=admin.headers)
    assert res.json() == {"data": [], "meta": {"version": "v1", "count": 0}}


async def test_limit_is_coerced_like_zod(
    client: httpx.AsyncClient, person: Callable[..., Any], db: asyncpg.Connection
) -> None:
    admin = await person("admin", admin=True)
    for bad in ("0", "501", "1.5", "abc", "", "-1", "Infinity", "1e3", "-0x1"):
        res = await client.get("/v1/runs", params={"limit": bad}, headers=admin.headers)
        assert res.status_code == 400, bad
        assert res.json()["error"]["code"] == "VALIDATION_ERROR"
    for good in ("1", " 2 ", "500", "1e2", "0x10", "3.0"):
        res = await client.get(
            "/v1/runs", params={"limit": good}, headers=admin.headers
        )
        assert res.status_code == 200, good
    res = await client.get("/v1/runs?limit=2&limit=3", headers=admin.headers)
    assert res.status_code == 400
    res = await client.get("/v1/runs?session_id=a&session_id=b", headers=admin.headers)
    assert res.status_code == 400


async def test_labels_filters_order_and_limit(
    client: httpx.AsyncClient, person: Callable[..., Any], db: asyncpg.Connection
) -> None:
    admin = await person("admin", admin=True)
    await db.execute(
        "UPDATE users SET first_name = 'Ann', last_name = 'Admin' WHERE id = $1",
        admin.id,
    )
    event_id = await insert_event(db, name="Fling")
    session_id = await insert_floor_session(db, event_id=event_id)
    other_session = await insert_floor_session(db)

    pair = await dancer_pair(db, first="Ada", last="Lovelace", partner=("Bob", "Jones"))
    await db.execute(
        "UPDATE songs SET division = 'Classic', season_year = '2026', routine_name = 'Waltz',"
        " processed_filename = 'x_v03.mp3' WHERE id = $1",
        pair["song_id"],
    )
    team = await dancer_pair(
        db, first="Cy", last="Coach", partner=("Rockets", ""), kind="team"
    )
    await db.execute(
        "UPDATE songs SET division = 'Teams' WHERE id = $1", team["song_id"]
    )
    solo = await insert_user(db, first="Sol", last="O")
    solo_song = await insert_song(db, solo)
    await db.execute(
        "UPDATE songs SET display_name = 'Solo Song' WHERE id = $1", solo_song
    )
    owner = await insert_user(db)
    mp = await insert_managed(
        db, owner, leader=("Lea", "Der"), follower=("Fol", "Lower")
    )
    mp_song = await insert_song(
        db, owner, managed_partnership_id=mp, division="Showcase"
    )
    nameless = await insert_user(db, first=None, last=None)
    nameless_song = await insert_song(db, nameless)

    r_pair = await insert_run(
        db,
        session_id=session_id,
        event_id=event_id,
        song_id=pair["song_id"],
        completed_by=admin.id,
        pair_id=pair["pair_id"],
        completed_at=500,
    )
    await insert_run(
        db,
        session_id=session_id,
        event_id=event_id,
        song_id=team["song_id"],
        completed_by=admin.id,
        pair_id=team["pair_id"],
        completed_at=400,
    )
    await insert_run(
        db,
        session_id=session_id,
        song_id=solo_song,
        completed_by=solo,
        solo_user_id=solo,
        completed_at=300,
    )
    await insert_run(
        db,
        session_id=session_id,
        event_id=event_id,
        song_id=mp_song,
        completed_by=nameless,
        managed_partnership_id=mp,
        completed_at=200,
    )
    await insert_run(
        db,
        session_id=other_session,
        song_id=nameless_song,
        completed_by=admin.id,
        solo_user_id=nameless,
        completed_at=100,
    )

    res = await client.get("/v1/runs", headers=admin.headers)
    body = res.json()
    assert body["meta"] == {"version": "v1", "count": 5}
    rows = body["data"]
    assert rows[0] == {
        "id": r_pair,
        "completed_at": 500,
        "division_name": "Classic",
        "session_id": session_id,
        "session_floor_trial_starts_at": rows[0]["session_floor_trial_starts_at"],
        "event_id": event_id,
        "event_name": "Fling",
        "song_id": pair["song_id"],
        "song_label": "Ada Lovelace & Bob Jones Classic 2026 Waltz v03",
        "entity_label": "Ada Lovelace & Bob Jones",
        "entity_key": f"pair:{pair['pair_id']}",
        "completed_by_label": "Ann Admin",
    }
    assert isinstance(rows[0]["session_floor_trial_starts_at"], int)
    assert [
        (r["entity_label"], r["song_label"], r["completed_by_label"]) for r in rows[1:]
    ] == [
        ("Rockets", "Rockets Teams", "Ann Admin"),
        ("Sol O", "Solo Song", "Sol O"),
        ("Lea Der & Fol Lower", "Lea Der & Fol Lower Showcase", "Admin"),
        ("—", nameless_song, "Ann Admin"),
    ]
    assert [r["entity_key"] for r in rows[2:]] == [
        f"solo:{solo}",
        f"managed:{mp}",
        f"solo:{nameless}",
    ]
    assert rows[2]["event_id"] is None and rows[2]["event_name"] is None

    res = await client.get(
        "/v1/runs", params={"session_id": other_session}, headers=admin.headers
    )
    assert [r["completed_at"] for r in res.json()["data"]] == [100]
    res = await client.get(
        "/v1/runs", params={"event_id": event_id}, headers=admin.headers
    )
    assert [r["completed_at"] for r in res.json()["data"]] == [500, 400, 200]
    res = await client.get(
        "/v1/runs",
        params={"event_id": event_id, "session_id": other_session},
        headers=admin.headers,
    )
    assert res.json()["data"] == []
    res = await client.get("/v1/runs", params={"limit": "2"}, headers=admin.headers)
    assert [r["completed_at"] for r in res.json()["data"]] == [500, 400]
    # An empty filter is no filter, as in Node.
    res = await client.get("/v1/runs?session_id=&event_id=", headers=admin.headers)
    assert res.json()["meta"]["count"] == 5
