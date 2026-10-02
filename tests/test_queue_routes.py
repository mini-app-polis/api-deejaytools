"""/v1/queue: floor-manager actions and the queue reads."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import asyncpg
import httpx

from api_deejaytools.cache import response_cache

from .content_helpers import insert_event, insert_song
from .queue_helpers import (
    HOUR,
    dancer_pair,
    insert_entry,
    insert_floor_session,
    insert_managed,
    insert_user,
    now_ms,
    positions,
    queue_events,
)


def _error(res: httpx.Response) -> tuple[int, str, str]:
    body = res.json()["error"]
    return res.status_code, body["code"], body["message"]


async def _fill(
    db: asyncpg.Connection, session_id: str, queue_type: str, n: int, **kw: Any
) -> list[dict[str, str]]:
    """``n`` pair entries at the bottom of a queue, in order."""
    start = len(await positions(db, session_id, queue_type))
    out = []
    for i in range(n):
        d = await dancer_pair(
            db, first=f"Lead{start + i}", partner=(f"Fol{start + i}", "X")
        )
        e = await insert_entry(
            db,
            session_id=session_id,
            queue_type=queue_type,
            position=start + i + 1,
            song_id=d["song_id"],
            submitted_by=d["user_id"],
            pair_id=d["pair_id"],
            **kw,
        )
        out.append({**d, **e})
    return out


def _closed_window() -> dict[str, int]:
    """A check-in window open, trial not started: actions never auto-fill."""
    now = now_ms()
    return {
        "checkin_opens_at": now - HOUR,
        "starts_at": now + HOUR,
        "ends_at": now + 2 * HOUR,
    }


# --- auth --------------------------------------------------------------------------


async def test_actions_need_queue_manage(
    client: httpx.AsyncClient, person: Callable[..., Any]
) -> None:
    dancer = await person()
    for path in ("promote", "complete", "incomplete", "move-down", "withdraw"):
        res = await client.post(f"/v1/queue/{path}", json={})
        assert _error(res)[:2] == (401, "UNAUTHORIZED"), path
        res = await client.post(
            f"/v1/queue/{path}", json={"queueEntryId": "x"}, headers=dancer.headers
        )
        assert _error(res) == (403, "FORBIDDEN", "Admin access required"), path


async def test_reads_public_and_admin(
    client: httpx.AsyncClient, person: Callable[..., Any], db: asyncpg.Connection
) -> None:
    dancer = await person()
    admin = await person("admin", admin=True)
    for view in ("active", "waiting"):
        res = await client.get(f"/v1/queue/s1/{view}")
        assert res.json() == {"data": [], "meta": {"version": "v1"}}
        # A bad token is ignored, not refused.
        res = await client.get(
            f"/v1/queue/s1/{view}", headers={"Authorization": "Bearer junk"}
        )
        assert res.status_code == 200
    for view in ("priority", "non-priority"):
        assert (await client.get(f"/v1/queue/s1/{view}")).status_code == 401
        res = await client.get(f"/v1/queue/s1/{view}", headers=dancer.headers)
        assert _error(res) == (403, "FORBIDDEN", "Admin access required")
        res = await client.get(f"/v1/queue/s1/{view}", headers=admin.headers)
        assert res.json() == {"data": [], "meta": {"version": "v1"}}


async def test_body_validation(
    client: httpx.AsyncClient, person: Callable[..., Any]
) -> None:
    admin = await person("admin", admin=True)
    for path in ("promote", "complete", "incomplete", "move-down", "withdraw"):
        for body in (
            {},
            {"queueEntryId": ""},
            {"queueEntryId": 5},
            {"queueEntryId": None},
        ):
            res = await client.post(
                f"/v1/queue/{path}", json=body, headers=admin.headers
            )
            assert _error(res)[:2] == (400, "VALIDATION_ERROR"), (path, body)
    for path in ("complete", "incomplete", "withdraw"):
        res = await client.post(
            f"/v1/queue/{path}",
            json={"queueEntryId": "x", "reason": 3},
            headers=admin.headers,
        )
        assert _error(res)[:2] == (400, "VALIDATION_ERROR")
        res = await client.post(
            f"/v1/queue/{path}",
            json={"queueEntryId": "x", "reason": None},
            headers=admin.headers,
        )
        assert res.status_code in (400, 404) and res.json()["error"]["code"] != (
            "VALIDATION_ERROR"
        )


# --- reads -------------------------------------------------------------------------


async def test_queue_entry_shape_and_labels(
    client: httpx.AsyncClient, person: Callable[..., Any], db: asyncpg.Connection
) -> None:
    admin = await person("admin", admin=True)
    session_id = await insert_floor_session(db, **_closed_window())
    pair = await dancer_pair(db, first="Ada", last="Lovelace", partner=("Bob", "Jones"))
    await db.execute(
        "UPDATE songs SET display_name = 'Our Routine', processed_filename = 'f_v01.mp3'"
        " WHERE id = $1",
        pair["song_id"],
    )
    team = await dancer_pair(
        db, first="Cy", last="Coach", partner=("Team", "Rocket"), kind="team"
    )
    solo = await insert_user(db, first="Sol", last="O")
    solo_song = await insert_song(db, solo)
    owner = await insert_user(db, first=None, last=None)
    mp = await insert_managed(
        db, owner, leader=("Lea", "Der"), follower=("Fol", "Lower")
    )
    nameless = await insert_user(db, first=None, last=None)
    nameless_song = await insert_song(db, nameless)

    a = await insert_entry(
        db,
        session_id=session_id,
        queue_type="priority",
        position=1,
        song_id=pair["song_id"],
        submitted_by=pair["user_id"],
        pair_id=pair["pair_id"],
        notes="note",
        created_at=7,
    )
    await insert_entry(
        db,
        session_id=session_id,
        queue_type="priority",
        position=2,
        song_id=team["song_id"],
        submitted_by=team["user_id"],
        pair_id=team["pair_id"],
    )
    await insert_entry(
        db,
        session_id=session_id,
        queue_type="non_priority",
        position=1,
        song_id=solo_song,
        submitted_by=solo,
        solo_user_id=solo,
    )
    await insert_entry(
        db,
        session_id=session_id,
        queue_type="non_priority",
        position=2,
        song_id=solo_song,
        submitted_by=owner,
        managed_partnership_id=mp,
    )
    await insert_entry(
        db,
        session_id=session_id,
        queue_type="active",
        position=1,
        song_id=nameless_song,
        submitted_by=nameless,
        solo_user_id=nameless,
    )

    waiting = (await client.get(f"/v1/queue/{session_id}/waiting")).json()
    assert "count" not in waiting["meta"]
    rows = waiting["data"]
    assert rows[0] == {
        "queueEntryId": a["entry_id"],
        "checkinId": a["checkin_id"],
        "position": 1,
        "enteredQueueAt": 7,
        "entityPairId": pair["pair_id"],
        "entitySoloUserId": None,
        "entityManagedPartnershipId": None,
        "entityLabel": "Ada Lovelace & Bob Jones",
        "divisionName": "Classic",
        "songId": pair["song_id"],
        "songDisplayName": "Our Routine",
        "songProcessedFilename": "f_v01.mp3",
        "notes": "note",
        "initialQueue": "priority",
        "checkedInAt": 7,
        "subQueue": "priority",
    }
    assert [(r["entityLabel"], r["subQueue"], r["position"]) for r in rows] == [
        ("Ada Lovelace & Bob Jones", "priority", 1),
        ("Team Rocket", "priority", 2),
        ("Sol O", "non_priority", 1),
        ("Lea Der & Fol Lower", "non_priority", 2),
    ]
    active = (await client.get(f"/v1/queue/{session_id}/active")).json()["data"]
    assert [r["entityLabel"] for r in active] == ["—"]
    assert "subQueue" not in active[0]
    priority = (
        await client.get(f"/v1/queue/{session_id}/priority", headers=admin.headers)
    ).json()["data"]
    assert [r["position"] for r in priority] == [1, 2]
    assert "subQueue" not in priority[0]
    standard = (
        await client.get(f"/v1/queue/{session_id}/non-priority", headers=admin.headers)
    ).json()["data"]
    assert [r["entityLabel"] for r in standard] == ["Sol O", "Lea Der & Fol Lower"]


async def test_reads_are_cached_until_a_mutation(
    client: httpx.AsyncClient, person: Callable[..., Any], db: asyncpg.Connection
) -> None:
    admin = await person("admin", admin=True)
    session_id = await insert_floor_session(db, **_closed_window())
    other = await insert_floor_session(db, **_closed_window())
    [first] = await _fill(db, session_id, "non_priority", 1)
    assert (
        len((await client.get(f"/v1/queue/{session_id}/waiting")).json()["data"]) == 1
    )
    assert (await client.get(f"/v1/queue/{other}/waiting")).json()["data"] == []
    # A write behind the API's back is not seen within the 3 s window...
    await _fill(db, session_id, "non_priority", 1)
    await _fill(db, other, "non_priority", 1)
    assert (
        len((await client.get(f"/v1/queue/{session_id}/waiting")).json()["data"]) == 1
    )
    assert response_cache.get(f"queue:{session_id}:waiting") is not None
    # ...and a mutation through the API invalidates that session's views only.
    res = await client.post(
        "/v1/queue/withdraw",
        json={"queueEntryId": first["entry_id"]},
        headers=admin.headers,
    )
    assert res.status_code == 200
    assert response_cache.get(f"queue:{session_id}:waiting") is None
    assert (
        len((await client.get(f"/v1/queue/{session_id}/waiting")).json()["data"]) == 1
    )
    assert (await client.get(f"/v1/queue/{other}/waiting")).json()["data"] == []


# --- promote -------------------------------------------------------------------------


async def test_promote_refusals(
    client: httpx.AsyncClient, person: Callable[..., Any], db: asyncpg.Connection
) -> None:
    admin = await person("admin", admin=True)
    session_id = await insert_floor_session(
        db, priority_max=2, non_priority_max=1, **_closed_window()
    )

    async def promote(entry_id: str) -> httpx.Response:
        return await client.post(
            "/v1/queue/promote", json={"queueEntryId": entry_id}, headers=admin.headers
        )

    assert _error(await promote("missing")) == (
        404,
        "NOT_FOUND",
        "Queue entry not found",
    )
    [active] = await _fill(db, session_id, "active", 1)
    assert _error(await promote(active["entry_id"])) == (
        400,
        "BAD_REQUEST",
        "Entry is already active",
    )
    [p1] = await _fill(db, session_id, "priority", 1)
    [s1] = await _fill(db, session_id, "non_priority", 1)
    assert _error(await promote(s1["entry_id"])) == (
        400,
        "BAD_REQUEST",
        "Cannot promote a standard entry while the priority queue has 1 waiting "
        "entry. Promote priority entries first.",
    )
    await _fill(db, session_id, "priority", 1)
    assert _error(await promote(s1["entry_id"]))[2].startswith(
        "Cannot promote a standard entry while the priority queue has 2 waiting entries."
    )
    assert (await promote(p1["entry_id"])).status_code == 200
    [p2] = await _fill(db, session_id, "priority", 1)
    assert _error(await promote(p2["entry_id"])) == (
        400,
        "BAD_REQUEST",
        "Active queue is at its priority cap (2/2 active).",
    )
    await db.execute("DELETE FROM queue_entries WHERE queue_type = 'priority'")
    assert _error(await promote(s1["entry_id"])) == (
        400,
        "BAD_REQUEST",
        "Active queue is at its standard cap (2/1 active). Finish or withdraw an "
        "active entry before promoting another standard entry.",
    )


async def test_promote_moves_to_bottom_of_active_with_audit(
    client: httpx.AsyncClient, person: Callable[..., Any], db: asyncpg.Connection
) -> None:
    admin = await person("admin", admin=True)
    session_id = await insert_floor_session(db, **_closed_window())
    [a] = await _fill(db, session_id, "active", 1)
    p = await _fill(db, session_id, "priority", 3)
    res = await client.post(
        "/v1/queue/promote",
        json={"queueEntryId": p[1]["entry_id"]},
        headers=admin.headers,
    )
    assert res.json() == {"data": {"promoted": True}, "meta": {"version": "v1"}}
    active = await db.fetch(
        "SELECT checkin_id, position FROM queue_entries WHERE session_id = $1"
        " AND queue_type = 'active' ORDER BY position",
        session_id,
    )
    assert [(r["checkin_id"], r["position"]) for r in active] == [
        (a["checkin_id"], 1),
        (p[1]["checkin_id"], 2),
    ]
    assert await positions(db, session_id, "priority") == [
        (p[0]["entry_id"], 1),
        (p[2]["entry_id"], 2),
    ]
    [event] = await queue_events(db, session_id)
    assert (
        event["action"],
        event["from_queue"],
        event["from_position"],
        event["to_queue"],
        event["to_position"],
        event["actor_user_id"],
        event["reason"],
    ) == ("promoted_to_active", "priority", 2, "active", 2, admin.id, None)


async def test_promote_skips_caps_on_closed_sessions(
    client: httpx.AsyncClient, person: Callable[..., Any], db: asyncpg.Connection
) -> None:
    admin = await person("admin", admin=True)
    for status in ("completed", "cancelled"):
        session_id = await insert_floor_session(
            db, status=status, priority_max=0, non_priority_max=0
        )
        await _fill(db, session_id, "priority", 1)
        [s] = await _fill(db, session_id, "non_priority", 1)
        res = await client.post(
            "/v1/queue/promote",
            json={"queueEntryId": s["entry_id"]},
            headers=admin.headers,
        )
        assert res.status_code == 200, status


async def test_concurrent_promotes_respect_the_cap(
    client: httpx.AsyncClient, person: Callable[..., Any], db: asyncpg.Connection
) -> None:
    """Two promotes race for the last active slot: the session lock makes the
    second see the first's result, so exactly one wins and the cap holds."""
    admin = await person("admin", admin=True)
    session_id = await insert_floor_session(
        db, priority_max=1, non_priority_max=1, **_closed_window()
    )
    p = await _fill(db, session_id, "priority", 2)
    results = await asyncio.gather(
        *(
            client.post(
                "/v1/queue/promote",
                json={"queueEntryId": e["entry_id"]},
                headers=admin.headers,
            )
            for e in p
        )
    )
    assert sorted(r.status_code for r in results) == [200, 400]
    loser = next(r for r in results if r.status_code == 400)
    assert _error(loser)[2] == "Active queue is at its priority cap (1/1 active)."
    assert [pos for _, pos in await positions(db, session_id, "active")] == [1]
    assert [pos for _, pos in await positions(db, session_id, "priority")] == [1]


# --- complete --------------------------------------------------------------------


async def test_complete_records_run_compacts_and_refills(
    client: httpx.AsyncClient, person: Callable[..., Any], db: asyncpg.Connection
) -> None:
    admin = await person("admin", admin=True)
    event_id = await insert_event(db)
    session_id = await insert_floor_session(
        db, event_id=event_id, priority_max=2, non_priority_max=2
    )
    owner = await insert_user(db)
    mp = await insert_managed(db, owner)
    song = await insert_song(db, owner, managed_partnership_id=mp)
    first = await insert_entry(
        db,
        session_id=session_id,
        queue_type="active",
        position=1,
        song_id=song,
        submitted_by=owner,
        managed_partnership_id=mp,
    )
    [second] = await _fill(db, session_id, "active", 1)
    [waiting] = await _fill(db, session_id, "non_priority", 1)

    res = await client.post(
        "/v1/queue/complete",
        json={"queueEntryId": waiting["entry_id"]},
        headers=admin.headers,
    )
    assert _error(res) == (400, "BAD_REQUEST", "Active queue entry not found")

    res = await client.post(
        "/v1/queue/complete",
        json={"queueEntryId": first["entry_id"], "reason": "clean"},
        headers=admin.headers,
    )
    assert res.json() == {"data": {"completed": True}, "meta": {"version": "v1"}}
    run = await db.fetchrow(
        "SELECT * FROM runs WHERE checkin_id = $1", first["checkin_id"]
    )
    assert run["event_id"] == event_id
    assert run["entity_managed_partnership_id"] == mp
    assert run["entity_pair_id"] is None
    assert run["song_id"] == song
    assert run["division_name"] == "Classic"
    assert run["completed_by_user_id"] == admin.id
    active = await db.fetch(
        "SELECT checkin_id, position FROM queue_entries WHERE session_id = $1"
        " AND queue_type = 'active' ORDER BY position",
        session_id,
    )
    assert [(r["checkin_id"], r["position"]) for r in active] == [
        (second["checkin_id"], 1),
        (waiting["checkin_id"], 2),
    ]
    events = await queue_events(db, session_id)
    assert [
        (e["action"], e["from_queue"], e["from_position"], e["to_queue"], e["reason"])
        for e in events
    ] == [
        ("run_completed", "active", 1, None, "clean"),
        ("promoted_to_active", "non_priority", 1, "active", None),
    ]


# --- incomplete --------------------------------------------------------------------


async def test_incomplete_rotates_to_bottom(
    client: httpx.AsyncClient, person: Callable[..., Any], db: asyncpg.Connection
) -> None:
    admin = await person("admin", admin=True)
    session_id = await insert_floor_session(db, **_closed_window())
    a = await _fill(db, session_id, "active", 3)
    [w] = await _fill(db, session_id, "priority", 1)

    res = await client.post(
        "/v1/queue/incomplete",
        json={"queueEntryId": w["entry_id"]},
        headers=admin.headers,
    )
    assert _error(res) == (400, "BAD_REQUEST", "Active queue entry not found")

    res = await client.post(
        "/v1/queue/incomplete",
        json={"queueEntryId": a[0]["entry_id"], "reason": "music stopped"},
        headers=admin.headers,
    )
    assert res.json() == {"data": {"rotated": True}, "meta": {"version": "v1"}}
    assert await positions(db, session_id, "active") == [
        (a[1]["entry_id"], 1),
        (a[2]["entry_id"], 2),
        (a[0]["entry_id"], 3),
    ]
    entered = await db.fetchval(
        "SELECT entered_queue_at FROM queue_entries WHERE id = $1", a[0]["entry_id"]
    )
    assert entered > 1
    [event] = await queue_events(db, session_id)
    assert (
        event["action"],
        event["from_position"],
        event["to_position"],
        event["reason"],
    ) == ("run_incomplete_rotated", 1, 3, "music stopped")
    assert await db.fetchval("SELECT count(*) FROM runs") == 0

    # Already at the bottom: a no-op that still answers rotated.
    res = await client.post(
        "/v1/queue/incomplete",
        json={"queueEntryId": a[0]["entry_id"]},
        headers=admin.headers,
    )
    assert res.json()["data"] == {"rotated": True}
    assert len(await queue_events(db, session_id)) == 1


# --- move-down ---------------------------------------------------------------------


async def test_move_down_swaps_with_one_audit_row(
    client: httpx.AsyncClient, person: Callable[..., Any], db: asyncpg.Connection
) -> None:
    admin = await person("admin", admin=True)
    session_id = await insert_floor_session(db, **_closed_window())
    w = await _fill(db, session_id, "non_priority", 3)

    async def move(entry_id: str) -> httpx.Response:
        return await client.post(
            "/v1/queue/move-down",
            json={"queueEntryId": entry_id},
            headers=admin.headers,
        )

    assert _error(await move("missing")) == (404, "NOT_FOUND", "Queue entry not found")
    assert _error(await move(w[2]["entry_id"])) == (
        400,
        "BAD_REQUEST",
        "Entry is already at the bottom of its queue",
    )
    res = await move(w[0]["entry_id"])
    assert res.json() == {"data": {"moved": True}, "meta": {"version": "v1"}}
    assert await positions(db, session_id, "non_priority") == [
        (w[1]["entry_id"], 1),
        (w[0]["entry_id"], 2),
        (w[2]["entry_id"], 3),
    ]
    [event] = await queue_events(db, session_id)
    assert (
        event["action"],
        event["checkin_id"],
        event["from_queue"],
        event["from_position"],
        event["to_queue"],
        event["to_position"],
        event["actor_user_id"],
    ) == (
        "moved_within_queue",
        w[0]["checkin_id"],
        "non_priority",
        1,
        "non_priority",
        2,
        admin.id,
    )


# --- withdraw ----------------------------------------------------------------------


async def test_withdraw_any_queue_compacts_and_refills(
    client: httpx.AsyncClient, person: Callable[..., Any], db: asyncpg.Connection
) -> None:
    admin = await person("admin", admin=True)
    session_id = await insert_floor_session(db, priority_max=1, non_priority_max=1)
    [a] = await _fill(db, session_id, "active", 1)
    s = await _fill(db, session_id, "non_priority", 3)

    res = await client.post(
        "/v1/queue/withdraw", json={"queueEntryId": "missing"}, headers=admin.headers
    )
    assert _error(res) == (404, "NOT_FOUND", "Queue entry not found")

    res = await client.post(
        "/v1/queue/withdraw",
        json={"queueEntryId": s[1]["entry_id"], "reason": "no show"},
        headers=admin.headers,
    )
    assert res.json() == {"data": {"withdrawn": True}, "meta": {"version": "v1"}}
    assert await positions(db, session_id, "non_priority") == [
        (s[0]["entry_id"], 1),
        (s[2]["entry_id"], 2),
    ]
    # The active slot was full, so nothing moved up.
    res = await client.post(
        "/v1/queue/withdraw",
        json={"queueEntryId": a["entry_id"]},
        headers=admin.headers,
    )
    assert res.status_code == 200
    events = await queue_events(db, session_id)
    assert [
        (e["action"], e["from_queue"], e["from_position"], e["reason"]) for e in events
    ] == [
        ("withdrawn", "non_priority", 2, "no show"),
        ("withdrawn", "active", 1, None),
        ("promoted_to_active", "non_priority", 1, None),
    ]
    active = await db.fetch(
        "SELECT checkin_id FROM queue_entries WHERE session_id = $1"
        " AND queue_type = 'active'",
        session_id,
    )
    assert [r["checkin_id"] for r in active] == [s[0]["checkin_id"]]
