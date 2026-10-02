"""/v1/checkins: check in, my live check-ins, self-withdraw."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import asyncpg
import httpx

from .content_helpers import insert_event, insert_song, new_id
from .queue_helpers import (
    HOUR,
    dancer_pair,
    insert_entry,
    insert_floor_session,
    insert_managed,
    insert_run,
    insert_user,
    now_ms,
    positions,
    queue_events,
)


async def _submit(
    db: asyncpg.Connection, event_id: str, song_id: str, user_id: str
) -> None:
    await db.execute(
        "INSERT INTO event_song_submissions (id, event_id, song_id,"
        " submitted_by_user_id, created_at) VALUES ($1, $2, $3, $4, 1)",
        new_id("sub"),
        event_id,
        song_id,
        user_id,
    )


def _body(session_id: str, d: dict[str, str], **extra: Any) -> dict[str, Any]:
    return {
        "sessionId": session_id,
        "divisionName": "Classic",
        "entityPairId": d["pair_id"],
        "songId": d["song_id"],
        **extra,
    }


async def _waiting_session(db: asyncpg.Connection, **kw: Any) -> str:
    """A session in its check-in window whose trial has not started: nothing
    is auto-filled."""
    now = now_ms()
    return await insert_floor_session(
        db,
        status="checkin_open",
        checkin_opens_at=now - HOUR,
        starts_at=now + HOUR,
        ends_at=now + 2 * HOUR,
        **kw,
    )


def _error(res: httpx.Response) -> tuple[int, str, str]:
    body = res.json()["error"]
    return res.status_code, body["code"], body["message"]


# --- POST /v1/checkins ----------------------------------------------------------


async def test_post_requires_auth_before_validation(client: httpx.AsyncClient) -> None:
    res = await client.post("/v1/checkins", json={})
    assert _error(res)[:2] == (401, "UNAUTHORIZED")


async def test_post_entity_xor(
    client: httpx.AsyncClient, person: Callable[..., Any], db: asyncpg.Connection
) -> None:
    me = await person()
    base = {"sessionId": "s", "divisionName": "Classic", "songId": "x"}
    for body in (
        base,
        {**base, "entityPairId": "p", "entityManagedPartnershipId": "m"},
        {**base, "entityPairId": ""},
        {**base, "on_behalf_of_user_id": "u", "entityPairId": "p"},
        {**base, "sessionId": ""},
        {**base, "entityPairId": "p", "notes": 5},
    ):
        res = await client.post("/v1/checkins", json=body, headers=me.headers)
        assert _error(res)[:2] == (400, "VALIDATION_ERROR"), body
    # null is allowed where zod said nullish; unknown keys are dropped.
    res = await client.post(
        "/v1/checkins",
        json={
            **base,
            "entityPairId": "p",
            "entityManagedPartnershipId": None,
            "on_behalf_of_user_id": None,
            "notes": None,
            "entitySoloUserId": "ignored",
        },
        headers=me.headers,
    )
    assert _error(res) == (404, "NOT_FOUND", "Session not found")


async def test_post_window_and_ownership_refusals(
    client: httpx.AsyncClient, person: Callable[..., Any], db: asyncpg.Connection
) -> None:
    me = await person()
    d = await dancer_pair(db, user_id=me.id)
    now = now_ms()
    early = await insert_floor_session(
        db,
        checkin_opens_at=now + HOUR,
        starts_at=now + 2 * HOUR,
        ends_at=now + 3 * HOUR,
    )
    late = await insert_floor_session(
        db,
        checkin_opens_at=now - 3 * HOUR,
        starts_at=now - 2 * HOUR,
        ends_at=now - HOUR,
    )
    res = await client.post("/v1/checkins", json=_body(early, d), headers=me.headers)
    assert _error(res) == (400, "BAD_REQUEST", "Check-in has not opened yet")
    res = await client.post("/v1/checkins", json=_body(late, d), headers=me.headers)
    assert _error(res) == (400, "BAD_REQUEST", "Check-in is closed for this session")

    session_id = await _waiting_session(db)
    other = await dancer_pair(db)
    res = await client.post(
        "/v1/checkins",
        json=_body(session_id, {**d, "song_id": other["song_id"]}),
        headers=me.headers,
    )
    assert _error(res) == (404, "NOT_FOUND", "Song not found")
    await db.execute("UPDATE songs SET deleted_at = 1 WHERE id = $1", other["song_id"])

    res = await client.post(
        "/v1/checkins",
        json=_body(session_id, {**d, "pair_id": "missing"}),
        headers=me.headers,
    )
    assert _error(res) == (400, "BAD_REQUEST", "Pair not found")
    res = await client.post(
        "/v1/checkins",
        json=_body(session_id, {**d, "pair_id": other["pair_id"]}),
        headers=me.headers,
    )
    assert _error(res) == (400, "BAD_REQUEST", "You are not a member of this pair")

    res = await client.post(
        "/v1/checkins",
        json={
            "sessionId": session_id,
            "divisionName": "Classic",
            "entityManagedPartnershipId": "mp",
            "songId": d["song_id"],
        },
        headers=me.headers,
    )
    assert _error(res) == (
        400,
        "BAD_REQUEST",
        "This song is not associated with a managed partnership",
    )

    res = await client.post(
        "/v1/checkins",
        json=_body(session_id, d, divisionName="Masters"),
        headers=me.headers,
    )
    assert _error(res) == (
        400,
        "BAD_REQUEST",
        "Division not configured for this session",
    )


async def test_post_event_session_requires_submission(
    client: httpx.AsyncClient, person: Callable[..., Any], db: asyncpg.Connection
) -> None:
    me = await person()
    d = await dancer_pair(db, user_id=me.id)
    event_id = await insert_event(db)
    session_id = await _waiting_session(db, event_id=event_id)
    res = await client.post(
        "/v1/checkins", json=_body(session_id, d), headers=me.headers
    )
    assert _error(res) == (
        400,
        "BAD_REQUEST",
        "This song hasn't been submitted to this event. Add it to the event on "
        "My Content before checking in.",
    )
    await _submit(db, event_id, d["song_id"], me.id)
    res = await client.post(
        "/v1/checkins", json=_body(session_id, d), headers=me.headers
    )
    assert res.status_code == 201, res.text


async def test_post_creates_checkin_entry_and_audit(
    client: httpx.AsyncClient, person: Callable[..., Any], db: asyncpg.Connection
) -> None:
    me = await person()
    d = await dancer_pair(db, user_id=me.id)
    session_id = await _waiting_session(db, divisions=(("Classic", True, 1),))
    res = await client.post(
        "/v1/checkins", json=_body(session_id, d, notes="hi"), headers=me.headers
    )
    assert res.status_code == 201
    body = res.json()
    assert body["meta"] == {"version": "v1"}
    data = body["data"]
    assert data == {
        "id": data["id"],
        "sessionId": session_id,
        "divisionName": "Classic",
        "initialQueue": "priority",
    }
    checkin = await db.fetchrow("SELECT * FROM checkins WHERE id = $1", data["id"])
    assert checkin["entity_pair_id"] == d["pair_id"]
    assert checkin["entity_solo_user_id"] is None
    assert checkin["submitted_by_user_id"] == me.id
    assert checkin["notes"] == "hi"
    assert checkin["initial_queue"] == "priority"
    assert [p for _, p in await positions(db, session_id, "priority")] == [1]
    events = await queue_events(db, session_id)
    assert [
        (
            e["action"],
            e["from_queue"],
            e["to_queue"],
            e["to_position"],
            e["actor_user_id"],
        )
        for e in events
    ] == [("checked_in", None, "priority", 1, me.id)]

    # The single-entry rule.
    res = await client.post(
        "/v1/checkins", json=_body(session_id, d), headers=me.headers
    )
    assert _error(res) == (
        409,
        "conflict",
        "This entity already has a live queue entry in this session",
    )


async def test_post_admission_demotes_after_run_limit(
    client: httpx.AsyncClient, person: Callable[..., Any], db: asyncpg.Connection
) -> None:
    me = await person()
    d = await dancer_pair(db, user_id=me.id)
    session_id = await _waiting_session(db, divisions=(("Classic", True, 1),))
    await insert_run(
        db,
        session_id=session_id,
        song_id=d["song_id"],
        completed_by=me.id,
        pair_id=d["pair_id"],
    )
    res = await client.post(
        "/v1/checkins", json=_body(session_id, d), headers=me.headers
    )
    assert res.json()["data"]["initialQueue"] == "non_priority"
    plain = await _waiting_session(db)
    res = await client.post("/v1/checkins", json=_body(plain, d), headers=me.headers)
    assert res.json()["data"]["initialQueue"] == "non_priority"


async def test_post_auto_fills_inside_the_trial(
    client: httpx.AsyncClient, person: Callable[..., Any], db: asyncpg.Connection
) -> None:
    me = await person()
    d = await dancer_pair(db, user_id=me.id)
    session_id = await insert_floor_session(db)
    # Read once so a stale cached view would show.
    assert (await client.get(f"/v1/queue/{session_id}/active")).json()["data"] == []
    res = await client.post(
        "/v1/checkins", json=_body(session_id, d), headers=me.headers
    )
    assert res.status_code == 201
    # Answered with the admission queue even though it is already active.
    assert res.json()["data"]["initialQueue"] == "non_priority"
    active = (await client.get(f"/v1/queue/{session_id}/active")).json()["data"]
    assert [e["entityPairId"] for e in active] == [d["pair_id"]]
    events = await queue_events(db, session_id)
    assert [e["action"] for e in events] == ["checked_in", "promoted_to_active"]
    assert events[1]["actor_user_id"] == me.id


async def test_post_managed_song_uses_its_partnership(
    client: httpx.AsyncClient, person: Callable[..., Any], db: asyncpg.Connection
) -> None:
    me = await person()
    d = await dancer_pair(db, user_id=me.id)
    mp = await insert_managed(db, me.id)
    mp2 = await insert_managed(db, me.id, leader=("Two", "Lead"))
    song = await insert_song(db, me.id, managed_partnership_id=mp)
    song2 = await insert_song(db, me.id, managed_partnership_id=mp2)
    session_id = await _waiting_session(db)
    # The song's partnership wins over the pair the body names.
    res = await client.post(
        "/v1/checkins",
        json=_body(session_id, {**d, "song_id": song}),
        headers=me.headers,
    )
    assert res.status_code == 201
    row = await db.fetchrow(
        "SELECT entity_pair_id, entity_managed_partnership_id FROM queue_entries"
        " WHERE checkin_id = $1",
        res.json()["data"]["id"],
    )
    assert (row["entity_pair_id"], row["entity_managed_partnership_id"]) == (None, mp)
    # A second managed partnership checks in alongside; the same one cannot.
    res = await client.post(
        "/v1/checkins",
        json={
            "sessionId": session_id,
            "divisionName": "Classic",
            "entityManagedPartnershipId": mp2,
            "songId": song2,
        },
        headers=me.headers,
    )
    assert res.status_code == 201
    res = await client.post(
        "/v1/checkins",
        json=_body(session_id, {**d, "song_id": song}),
        headers=me.headers,
    )
    assert res.status_code == 409

    deleted = await insert_managed(db, me.id, deleted=True)
    song3 = await insert_song(db, me.id, managed_partnership_id=deleted)
    res = await client.post(
        "/v1/checkins",
        json=_body(session_id, {**d, "song_id": song3}),
        headers=me.headers,
    )
    assert _error(res) == (400, "BAD_REQUEST", "Managed partnership not found")


async def test_post_on_behalf(
    client: httpx.AsyncClient, person: Callable[..., Any], db: asyncpg.Connection
) -> None:
    admin = await person("admin", admin=True)
    dancer = await person("dancer")
    session_id = await _waiting_session(db)
    partner_id = (await dancer_pair(db, user_id=dancer.id))["partner_id"]
    # A song whose pair does not exist yet: the server creates it.
    await db.execute("DELETE FROM pairs WHERE user_a_id = $1", dancer.id)
    song = await insert_song(db, dancer.id, partner_id=partner_id)
    body = {
        "sessionId": session_id,
        "divisionName": "Classic",
        "songId": song,
        "on_behalf_of_user_id": f"  {dancer.id}  ",
    }

    res = await client.post("/v1/checkins", json=body, headers=dancer.headers)
    assert _error(res) == (403, "FORBIDDEN", "Admin access required")
    denied = await db.fetchval(
        "SELECT count(*) FROM identity_audit_events WHERE scope ="
        " 'deejaytools.delegation.act' AND NOT allowed AND resource = $1",
        dancer.id,
    )
    assert denied == 1

    res = await client.post(
        "/v1/checkins",
        json={**body, "on_behalf_of_user_id": "user_missing"},
        headers=admin.headers,
    )
    assert _error(res) == (400, "BAD_REQUEST", "Target user not found")

    res = await client.post("/v1/checkins", json=body, headers=admin.headers)
    assert res.status_code == 201, res.text
    checkin = await db.fetchrow(
        "SELECT * FROM checkins WHERE id = $1", res.json()["data"]["id"]
    )
    pair_owner = await db.fetchval(
        "SELECT user_a_id FROM pairs WHERE id = $1", checkin["entity_pair_id"]
    )
    assert pair_owner == dancer.id
    assert checkin["submitted_by_user_id"] == dancer.id
    events = await queue_events(db, session_id)
    assert events[0]["actor_user_id"] == admin.id

    partnerless = await insert_song(db, dancer.id)
    res = await client.post(
        "/v1/checkins", json={**body, "songId": partnerless}, headers=admin.headers
    )
    assert _error(res) == (
        400,
        "BAD_REQUEST",
        "This song has no partner or managed partnership. Attach one on the song "
        "before checking in.",
    )


async def test_concurrent_checkins_serialize_on_the_session_lock(
    client: httpx.AsyncClient, person: Callable[..., Any], db: asyncpg.Connection
) -> None:
    """Eight check-ins into one session at once: the session lock serializes
    them, so every one lands, positions are dense and none collide on the
    (session, queue, position) unique index."""
    session_id = await insert_floor_session(db, priority_max=3, non_priority_max=3)
    dancers = []
    for i in range(8):
        p = await person(f"d{i}")
        dancers.append((p, await dancer_pair(db, user_id=p.id)))

    results = await asyncio.gather(
        *(
            client.post("/v1/checkins", json=_body(session_id, d), headers=p.headers)
            for p, d in dancers
        )
    )
    assert [r.status_code for r in results] == [201] * 8, [r.text for r in results]
    assert [p for _, p in await positions(db, session_id, "active")] == [1, 2, 3]
    assert [p for _, p in await positions(db, session_id, "non_priority")] == [
        1,
        2,
        3,
        4,
        5,
    ]
    events = await queue_events(db, session_id)
    assert sorted(e["action"] for e in events).count("promoted_to_active") == 3


# --- GET /v1/checkins/mine -------------------------------------------------------


async def test_mine_lists_live_checkins_with_positions(
    client: httpx.AsyncClient, person: Callable[..., Any], db: asyncpg.Connection
) -> None:
    me = await person("ada")
    assert _error(await client.get("/v1/checkins/mine"))[:2] == (401, "UNAUTHORIZED")
    assert (await client.get("/v1/checkins/mine", headers=me.headers)).json() == {
        "data": [],
        "meta": {"version": "v1"},
    }

    await db.execute(
        "UPDATE users SET first_name = 'Ada', last_name = 'Lovelace' WHERE id = $1",
        me.id,
    )
    d = await dancer_pair(db, user_id=me.id)
    mp = await insert_managed(db, me.id, leader=("Lea", "Der"), follower=("", ""))
    event_id = await insert_event(db, name="Fling")
    session_id = await insert_floor_session(db, event_id=event_id)
    other = await dancer_pair(db)
    # Ahead of mine: two active, one priority.
    for i, qt in enumerate(("active", "active", "priority")):
        o = await dancer_pair(db)
        await insert_entry(
            db,
            session_id=session_id,
            queue_type=qt,
            position=1 if qt == "priority" else i + 1,
            song_id=o["song_id"],
            submitted_by=o["user_id"],
            pair_id=o["pair_id"],
        )
    mine = await insert_entry(
        db,
        session_id=session_id,
        queue_type="non_priority",
        position=1,
        song_id=d["song_id"],
        submitted_by=me.id,
        pair_id=d["pair_id"],
        notes="n",
        created_at=10,
    )
    managed = await insert_entry(
        db,
        session_id=session_id,
        queue_type="priority",
        position=2,
        song_id=d["song_id"],
        submitted_by=me.id,
        managed_partnership_id=mp,
        created_at=20,
    )
    await insert_run(
        db,
        session_id=session_id,
        song_id=d["song_id"],
        completed_by=me.id,
        pair_id=d["pair_id"],
    )
    await insert_run(
        db,
        session_id=session_id,
        song_id=other["song_id"],
        completed_by=other["user_id"],
        pair_id=other["pair_id"],
    )

    res = await client.get("/v1/checkins/mine", headers=me.headers)
    assert res.status_code == 200
    body = res.json()
    assert body["meta"] == {"version": "v1"}
    rows = body["data"]
    assert [r["id"] for r in rows] == [managed["checkin_id"], mine["checkin_id"]]
    assert rows[1] == {
        "id": mine["checkin_id"],
        "sessionId": session_id,
        "eventName": "Fling",
        "sessionName": "Floor",
        "sessionFloorTrialStartsAt": rows[1]["sessionFloorTrialStartsAt"],
        "sessionStatus": "in_progress",
        "eventTimezone": "America/Chicago",
        "divisionName": "Classic",
        "entityPairId": d["pair_id"],
        "entitySoloUserId": None,
        "entityManagedPartnershipId": None,
        "entityLabel": "Ada Lovelace & Bob Jones",
        "songDisplayName": None,
        "songProcessedFilename": None,
        "notes": "n",
        "checkedInAt": 10,
        "queueEntryId": mine["entry_id"],
        "queueType": "non_priority",
        "queuePosition": 1,
        "overallPosition": 5,
        "runCount": 1,
    }
    assert rows[0]["entityLabel"] == "Lea Der"
    assert rows[0]["overallPosition"] == 4
    assert rows[0]["runCount"] == 0


# --- DELETE /v1/checkins/{id} ----------------------------------------------------


async def test_delete_withdraws_own_entry(
    client: httpx.AsyncClient, person: Callable[..., Any], db: asyncpg.Connection
) -> None:
    me = await person()
    stranger = await person("bob")
    session_id = await _waiting_session(db)
    entries = []
    for _ in range(2):
        o = await dancer_pair(db)
        entries.append(o)
    d = await dancer_pair(db, user_id=me.id)
    first = await insert_entry(
        db,
        session_id=session_id,
        queue_type="non_priority",
        position=1,
        song_id=entries[0]["song_id"],
        submitted_by=entries[0]["user_id"],
        pair_id=entries[0]["pair_id"],
    )
    mine = await insert_entry(
        db,
        session_id=session_id,
        queue_type="non_priority",
        position=2,
        song_id=d["song_id"],
        submitted_by=me.id,
        pair_id=d["pair_id"],
    )
    last = await insert_entry(
        db,
        session_id=session_id,
        queue_type="non_priority",
        position=3,
        song_id=entries[1]["song_id"],
        submitted_by=entries[1]["user_id"],
        pair_id=entries[1]["pair_id"],
    )

    assert _error(await client.delete(f"/v1/checkins/{mine['checkin_id']}"))[0] == 401
    res = await client.delete("/v1/checkins/missing", headers=me.headers)
    assert _error(res) == (404, "NOT_FOUND", "Check-in not found")
    res = await client.delete(
        f"/v1/checkins/{mine['checkin_id']}", headers=stranger.headers
    )
    assert _error(res) == (403, "FORBIDDEN", "Admin access required")

    res = await client.delete(f"/v1/checkins/{mine['checkin_id']}", headers=me.headers)
    assert res.json() == {"data": {"withdrawn": True}, "meta": {"version": "v1"}}
    assert await positions(db, session_id, "non_priority") == [
        (first["entry_id"], 1),
        (last["entry_id"], 2),
    ]
    events = await queue_events(db, session_id)
    assert [
        (
            e["action"],
            e["from_queue"],
            e["from_position"],
            e["actor_user_id"],
            e["reason"],
        )
        for e in events
    ] == [("withdrawn", "non_priority", 2, me.id, "self_withdrew")]
    # Withdrawn: no live entry, so 404 now.
    res = await client.delete(f"/v1/checkins/{mine['checkin_id']}", headers=me.headers)
    assert res.status_code == 404


async def test_delete_managed_owner_and_refill(
    client: httpx.AsyncClient, person: Callable[..., Any], db: asyncpg.Connection
) -> None:
    me = await person()
    session_id = await insert_floor_session(db, priority_max=1, non_priority_max=1)
    mp = await insert_managed(db, me.id)
    song = await insert_song(db, me.id, managed_partnership_id=mp)
    mine = await insert_entry(
        db,
        session_id=session_id,
        queue_type="active",
        position=1,
        song_id=song,
        submitted_by=me.id,
        managed_partnership_id=mp,
    )
    o = await dancer_pair(db)
    waiting = await insert_entry(
        db,
        session_id=session_id,
        queue_type="non_priority",
        position=1,
        song_id=o["song_id"],
        submitted_by=o["user_id"],
        pair_id=o["pair_id"],
    )
    res = await client.delete(f"/v1/checkins/{mine['checkin_id']}", headers=me.headers)
    assert res.status_code == 200
    active = await db.fetch(
        "SELECT checkin_id, position FROM queue_entries WHERE session_id = $1"
        " AND queue_type = 'active'",
        session_id,
    )
    assert [(r["checkin_id"], r["position"]) for r in active] == [
        (waiting["checkin_id"], 1)
    ]
    actions = [e["action"] for e in await queue_events(db, session_id)]
    assert actions == ["withdrawn", "promoted_to_active"]


async def test_delete_solo_legacy_entry(
    client: httpx.AsyncClient, person: Callable[..., Any], db: asyncpg.Connection
) -> None:
    me = await person()
    session_id = await _waiting_session(db)
    song = await insert_song(db, me.id)
    solo = await insert_entry(
        db,
        session_id=session_id,
        queue_type="priority",
        position=1,
        song_id=song,
        submitted_by=me.id,
        solo_user_id=me.id,
    )
    other = await insert_user(db)
    assert other
    res = await client.delete(f"/v1/checkins/{solo['checkin_id']}", headers=me.headers)
    assert res.status_code == 200


async def test_post_admission_database_error_is_400(
    client: httpx.AsyncClient, person: Callable[..., Any], db: asyncpg.Connection
) -> None:
    """As deejaytools-api: any error from the admission lookup is a 400 with
    the database's message (here Postgres refusing a NUL byte)."""
    me = await person()
    d = await dancer_pair(db, user_id=me.id)
    session_id = await _waiting_session(db, divisions=(("Classic", True, 1),))
    res = await client.post(
        "/v1/checkins",
        json=_body(session_id, d, divisionName="Clas\u0000sic"),
        headers=me.headers,
    )
    assert _error(res) == (
        400,
        "BAD_REQUEST",
        'invalid byte sequence for encoding "UTF8": 0x00',
    )
    # The session is usable afterwards (the failed transaction was rolled back).
    res = await client.post(
        "/v1/checkins", json=_body(session_id, d), headers=me.headers
    )
    assert res.status_code == 201
