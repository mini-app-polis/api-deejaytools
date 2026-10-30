"""The DJ's routes, event-scoped (ADR-0012): queue, runs, sessions, delegation.

Every route the DJ gains is decided on the event the request is about: a DJ
(or event manager) of event A is allowed on A and refused on B, a plain
dancer is refused, and an admin, holding the scope through a role, is
allowed everywhere. Each decision is audited on ``event:<id>``.
"""

from __future__ import annotations

import itertools
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

import asyncpg
import httpx
import pytest

from .content_helpers import new_id
from .queue_helpers import dancer_pair, insert_entry, insert_floor_session, insert_run

HOUR = 3_600_000
DELEGATION = "deejaytools.delegation.act"


def _now() -> int:
    return int(time.time() * 1000)


async def _event(client: httpx.AsyncClient, admin: Any, name: str) -> str:
    today = date.today()
    res = await client.post(
        "/v1/events",
        json={
            "name": name,
            "start_date": (today - timedelta(days=2)).isoformat(),
            "end_date": (today + timedelta(days=2)).isoformat(),
            "timezone": "UTC",
        },
        headers=admin.headers,
    )
    assert res.status_code == 201, res.text
    event_id: str = res.json()["data"]["id"]
    return event_id


async def _grant(
    client: httpx.AsyncClient, admin: Any, event_id: str, user_id: str, role: str
) -> None:
    res = await client.put(
        f"/v1/events/{event_id}/staff/{user_id}/{role}", headers=admin.headers
    )
    assert res.status_code == 200, res.text


@dataclass
class World:
    """Events A and B; a DJ and an event manager of A; a dancer; an admin;
    and a dancer staff may act for."""

    admin: Any
    dj: Any
    manager: Any
    dancer: Any
    target: Any
    event_a: str
    event_b: str


@pytest.fixture
async def world(client: httpx.AsyncClient, person: Callable[..., Any]) -> World:
    admin = await person("admin", admin=True)
    dj = await person("dj")
    manager = await person("manager")
    dancer = await person("dancer")
    target = await person("target")
    event_a = await _event(client, admin, "A")
    event_b = await _event(client, admin, "B")
    await _grant(client, admin, event_a, dj.id, "dj")
    await _grant(client, admin, event_a, manager.id, "event-manager")
    return World(admin, dj, manager, dancer, target, event_a, event_b)


# --- per-route setups --------------------------------------------------------------
#
# Each builds fresh state in the event it is given, then makes the request as
# ``who``. Fresh per call: an allowed complete or withdraw consumes its entry.

Call = Callable[[httpx.AsyncClient, asyncpg.Connection, World, Any, str], Awaitable]


async def _entry_in(
    db: asyncpg.Connection, event_id: str, queue_type: str, position: int = 1
) -> dict[str, str]:
    session_id = await insert_floor_session(db, event_id=event_id)
    pair = await dancer_pair(db)
    return {
        "session_id": session_id,
        **await insert_entry(
            db,
            session_id=session_id,
            queue_type=queue_type,
            position=position,
            song_id=pair["song_id"],
            submitted_by=pair["user_id"],
            pair_id=pair["pair_id"],
        ),
    }


def _queue_action(path: str, queue_type: str) -> Call:
    async def call(client, db, w, who, event_id):  # type: ignore[no-untyped-def]
        entry = await _entry_in(db, event_id, queue_type)
        return await client.post(
            f"/v1/queue/{path}",
            json={"queueEntryId": entry["entry_id"]},
            headers=who.headers,
        )

    return call


async def _move_down(client, db, w, who, event_id):  # type: ignore[no-untyped-def]
    entry = await _entry_in(db, event_id, "non_priority")
    pair = await dancer_pair(db)
    await insert_entry(
        db,
        session_id=entry["session_id"],
        queue_type="non_priority",
        position=2,
        song_id=pair["song_id"],
        submitted_by=pair["user_id"],
        pair_id=pair["pair_id"],
    )
    return await client.post(
        "/v1/queue/move-down",
        json={"queueEntryId": entry["entry_id"]},
        headers=who.headers,
    )


def _queue_read(queue: str) -> Call:
    async def call(client, db, w, who, event_id):  # type: ignore[no-untyped-def]
        session_id = await insert_floor_session(db, event_id=event_id)
        return await client.get(f"/v1/queue/{session_id}/{queue}", headers=who.headers)

    return call


async def _runs_by_event(client, db, w, who, event_id):  # type: ignore[no-untyped-def]
    session_id = await insert_floor_session(db, event_id=event_id)
    pair = await dancer_pair(db)
    await insert_run(
        db,
        session_id=session_id,
        song_id=pair["song_id"],
        completed_by=w.admin.id,
        pair_id=pair["pair_id"],
        event_id=event_id,
    )
    return await client.get(
        "/v1/runs", params={"event_id": event_id}, headers=who.headers
    )


async def _runs_by_session(client, db, w, who, event_id):  # type: ignore[no-untyped-def]
    session_id = await insert_floor_session(db, event_id=event_id)
    return await client.get(
        "/v1/runs", params={"session_id": session_id}, headers=who.headers
    )


def _session_times(offset_hours: int) -> dict[str, int]:
    # Inside the events' dates (today, UTC), and clear of each other.
    start = _now() - 12 * HOUR + offset_hours * HOUR
    return {
        "checkin_opens_at": start,
        "floor_trial_starts_at": start + HOUR // 2,
        "floor_trial_ends_at": start + HOUR,
    }


# Each new session gets its own hour, so none overlaps another in its event.
_hours = itertools.count()


async def _create_session(client, db, w, who, event_id):  # type: ignore[no-untyped-def]
    return await client.post(
        "/v1/sessions",
        json={
            "name": "Friday",
            "event_id": event_id,
            "divisions": [{"division_name": "Classic"}],
            **_session_times(next(_hours) % 20),
        },
        headers=who.headers,
    )


async def _put_divisions(client, db, w, who, event_id):  # type: ignore[no-untyped-def]
    session_id = await insert_floor_session(db, event_id=event_id)
    return await client.put(
        f"/v1/sessions/{session_id}/divisions",
        json={"divisions": [{"division_name": "Classic"}]},
        headers=who.headers,
    )


async def _patch_status(client, db, w, who, event_id):  # type: ignore[no-untyped-def]
    session_id = await insert_floor_session(db, event_id=event_id)
    return await client.patch(
        f"/v1/sessions/{session_id}/status",
        json={"status": "cancelled"},
        headers=who.headers,
    )


async def _patch_session(client, db, w, who, event_id):  # type: ignore[no-untyped-def]
    times = _session_times(next(_hours) % 20)
    session_id = await insert_floor_session(
        db,
        event_id=event_id,
        checkin_opens_at=times["checkin_opens_at"],
        starts_at=times["floor_trial_starts_at"],
        ends_at=times["floor_trial_ends_at"],
    )
    return await client.patch(
        f"/v1/sessions/{session_id}", json={"name": "Renamed"}, headers=who.headers
    )


async def _delete_session(client, db, w, who, event_id):  # type: ignore[no-untyped-def]
    session_id = await insert_floor_session(db, event_id=event_id)
    return await client.delete(f"/v1/sessions/{session_id}", headers=who.headers)


async def _submit(
    db: asyncpg.Connection, event_id: str, user_id: str, song: str
) -> None:
    await db.execute(
        "INSERT INTO event_song_submissions (id, event_id, song_id,"
        " submitted_by_user_id, created_at) VALUES ($1, $2, $3, $4, 1)",
        new_id("sub"),
        event_id,
        song,
        user_id,
    )


async def _checkin_for(client, db, w, who, event_id):  # type: ignore[no-untyped-def]
    now = _now()
    session_id = await insert_floor_session(
        db,
        event_id=event_id,
        status="checkin_open",
        checkin_opens_at=now - HOUR,
        starts_at=now + HOUR,
        ends_at=now + 2 * HOUR,
    )
    pair = await dancer_pair(db, user_id=w.target.id)
    await _submit(db, event_id, w.target.id, pair["song_id"])
    return await client.post(
        "/v1/checkins",
        json={
            "sessionId": session_id,
            "divisionName": "Classic",
            "songId": pair["song_id"],
            "on_behalf_of_user_id": w.target.id,
        },
        headers=who.headers,
    )


async def _upload_for(client, db, w, who, event_id):  # type: ignore[no-untyped-def]
    await _connect(db, event_id, w.target.id)
    return await _upload_chunk(client, who, w.target.id, event_id)


async def _connect(db: asyncpg.Connection, event_id: str, user_id: str) -> None:
    """Connect a dancer to an event: a song of theirs submitted to it."""
    pair = await dancer_pair(db, user_id=user_id)
    await _submit(db, event_id, user_id, pair["song_id"])


async def _upload_chunk(
    client: httpx.AsyncClient, who: Any, target: str, event_id: str | None
) -> httpx.Response:
    """The first of two chunks: authorized and staged, nothing built yet."""
    form = {
        "upload_id": str(uuid.uuid4()),
        "chunk_index": "0",
        "total_chunks": "2",
        "original_filename": "routine.mp3",
        "division": "Classic",
        "on_behalf_of_user_id": target,
    }
    if event_id is not None:
        form["event_id"] = event_id
    return await client.post(
        "/v1/songs/upload/chunk",
        data=form,
        files={"chunk": ("chunk", b"x" * 100)},
        headers=who.headers,
    )


@dataclass(frozen=True)
class Route:
    name: str
    scope: str
    call: Call
    # The audit resource the decision is recorded on, for an event and target.
    resource: Callable[[str, str], str] = lambda event_id, _target: f"event:{event_id}"


def _delegated(event_id: str, target: str) -> str:
    return f"event:{event_id}/on-behalf-of/{target}"


ROUTES = [
    Route(
        "promote", "deejaytools.queue.manage", _queue_action("promote", "non_priority")
    ),
    Route("complete", "deejaytools.queue.manage", _queue_action("complete", "active")),
    Route(
        "incomplete", "deejaytools.queue.manage", _queue_action("incomplete", "active")
    ),
    Route("move-down", "deejaytools.queue.manage", _move_down),
    Route(
        "withdraw", "deejaytools.queue.manage", _queue_action("withdraw", "priority")
    ),
    Route("priority", "deejaytools.queue.read", _queue_read("priority")),
    Route("non-priority", "deejaytools.queue.read", _queue_read("non-priority")),
    Route("active-staff", "deejaytools.queue.read", _queue_read("active/staff")),
    Route("runs-by-event", "deejaytools.runs.read", _runs_by_event),
    Route("runs-by-session", "deejaytools.runs.read", _runs_by_session),
    Route("create-session", "deejaytools.sessions.write", _create_session),
    Route("put-divisions", "deejaytools.sessions.write", _put_divisions),
    Route("patch-status", "deejaytools.sessions.write", _patch_status),
    Route("patch-session", "deejaytools.sessions.write", _patch_session),
    Route("delete-session", "deejaytools.sessions.write", _delete_session),
    Route("checkin-on-behalf", DELEGATION, _checkin_for, _delegated),
    Route("upload-on-behalf", DELEGATION, _upload_for, _delegated),
]


@pytest.mark.parametrize("route", ROUTES, ids=[r.name for r in ROUTES])
async def test_the_event_staff_are_allowed_on_their_event_only(
    route: Route, client: httpx.AsyncClient, db: asyncpg.Connection, world: World
) -> None:
    w = world
    for who in (w.dj, w.manager, w.admin):
        res = await route.call(client, db, w, who, w.event_a)
        assert res.status_code < 300, (route.name, who.id, res.text)
    # An admin holds the scope through a role: every event.
    res = await route.call(client, db, w, w.admin, w.event_b)
    assert res.status_code < 300, res.text

    for who, event_id in ((w.dj, w.event_b), (w.manager, w.event_b)):
        res = await route.call(client, db, w, who, event_id)
        assert res.status_code == 403, (route.name, who.id, res.text)
        assert res.json()["error"] == {
            "code": "FORBIDDEN",
            "message": "Admin access required",
        }
    # A plain dancer, on either event.
    for event_id in (w.event_a, w.event_b):
        res = await route.call(client, db, w, w.dancer, event_id)
        assert res.status_code == 403, (route.name, res.text)

    # Every decision is audited, on the event the request was about.
    audited = await db.fetch(
        "SELECT subject, resource, allowed, reason FROM identity_audit_events"
        " WHERE scope = $1 AND subject = ANY($2::text[])",
        route.scope,
        [w.dj.id, w.dancer.id],
    )
    assert {
        (r["subject"], r["resource"], r["allowed"], r["reason"]) for r in audited
    } == {
        (
            w.dj.id,
            route.resource(w.event_a, w.target.id),
            True,
            "granted_by_explicit_grant",
        ),
        (w.dj.id, route.resource(w.event_b, w.target.id), False, "no_matching_scope"),
        (
            w.dancer.id,
            route.resource(w.event_a, w.target.id),
            False,
            "no_matching_scope",
        ),
        (
            w.dancer.id,
            route.resource(w.event_b, w.target.id),
            False,
            "no_matching_scope",
        ),
    }


async def test_runs_without_a_filter_need_the_scope_through_a_role(
    client: httpx.AsyncClient, db: asyncpg.Connection, world: World
) -> None:
    w = world
    assert (await client.get("/v1/runs", headers=w.dj.headers)).status_code == 403
    assert (await client.get("/v1/runs", headers=w.admin.headers)).status_code == 200
    denied = await db.fetchval(
        "SELECT count(*) FROM identity_audit_events WHERE subject = $1"
        " AND scope = 'deejaytools.runs.read' AND resource IS NULL AND NOT allowed",
        w.dj.id,
    )
    assert denied == 1
    # A repeated event_id names no event, so it decides as if none were sent.
    res = await client.get(
        f"/v1/runs?event_id={w.event_a}&event_id={w.event_a}", headers=w.dj.headers
    )
    assert res.status_code == 403


async def test_runs_by_event_list_only_that_events_runs(
    client: httpx.AsyncClient, db: asyncpg.Connection, world: World
) -> None:
    w = world
    for event_id in (w.event_a, w.event_b):
        await _runs_by_event(client, db, w, w.admin, event_id)
    res = await client.get(
        "/v1/runs", params={"event_id": w.event_a}, headers=w.dj.headers
    )
    assert res.status_code == 200
    assert {r["event_id"] for r in res.json()["data"]} == {w.event_a}


async def test_a_session_outside_the_djs_event_is_refused(
    client: httpx.AsyncClient, db: asyncpg.Connection, world: World
) -> None:
    w = world
    # A session with no event, an unknown one, and a create with no event:
    # only a role can allow them, and the DJ learns nothing about the id.
    loose = await insert_floor_session(db)
    for res in (
        await client.patch(
            f"/v1/sessions/{loose}", json={"name": "x"}, headers=w.dj.headers
        ),
        await client.delete("/v1/sessions/ses_none", headers=w.dj.headers),
        await client.get("/v1/queue/ses_none/priority", headers=w.dj.headers),
        await client.post(
            "/v1/queue/promote", json={"queueEntryId": "qe_none"}, headers=w.dj.headers
        ),
        await client.post(
            "/v1/sessions",
            json={"name": "Loose", **_session_times(0)},
            headers=w.dj.headers,
        ),
    ):
        assert res.status_code == 403, res.text
    assert (
        await client.delete("/v1/sessions/ses_none", headers=w.admin.headers)
    ).status_code == 404


async def test_moving_a_session_needs_the_scope_on_both_events(
    client: httpx.AsyncClient,
    db: asyncpg.Connection,
    world: World,
    person: Callable[..., Any],
) -> None:
    w = world
    event_c = await _event(client, w.admin, "C")
    await _grant(client, w.admin, event_c, w.dj.id, "dj")
    session_id = await insert_floor_session(db, event_id=w.event_a)

    def patch(who: Any, event_id: str | None) -> Awaitable[httpx.Response]:
        return client.patch(
            f"/v1/sessions/{session_id}",
            json={"event_id": event_id},
            headers=who.headers,
        )

    # To an event they are not staff of, or out of every event: refused.
    assert (await patch(w.dj, w.event_b)).status_code == 403
    assert (await patch(w.dj, None)).status_code == 403
    assert (
        await db.fetchval("SELECT event_id FROM sessions WHERE id = $1", session_id)
        == w.event_a
    )
    # Unchanged event_id is no move.
    assert (await patch(w.dj, w.event_a)).status_code == 200
    # To another event they are DJ of: allowed.
    res = await patch(w.dj, event_c)
    assert res.status_code == 200, res.text
    assert res.json()["data"]["event_id"] == event_c

    decided = await db.fetch(
        "SELECT resource, allowed FROM identity_audit_events WHERE subject = $1"
        " AND scope = 'deejaytools.sessions.write' ORDER BY occurred_at",
        w.dj.id,
    )
    assert [(r["resource"], r["allowed"]) for r in decided] == [
        (f"event:{w.event_a}", True),
        (f"event:{w.event_b}", False),
        (f"event:{w.event_a}", True),
        (None, False),
        (f"event:{w.event_a}", True),
        (f"event:{w.event_a}", True),
        (f"event:{event_c}", True),
    ]
    # An admin moves it anywhere, and out of every event.
    assert (await patch(w.admin, w.event_b)).status_code == 200
    assert (await patch(w.admin, None)).status_code == 200


async def test_uploads_on_behalf_by_staff_name_the_event(
    client: httpx.AsyncClient,
    db: asyncpg.Connection,
    world: World,
    person: Callable[..., Any],
) -> None:
    w = world
    stranger = await person("stranger")
    await _connect(db, w.event_a, w.target.id)
    # A scoped caller without event_id: refused, and the decision audited,
    # naming the dancer acted for.
    res = await _upload_chunk(client, w.dj, w.target.id, None)
    assert res.status_code == 403
    assert res.json()["error"]["code"] == "FORBIDDEN"
    assert (
        await _upload_chunk(client, w.dj, w.target.id, w.event_a)
    ).status_code == 200
    # Someone with no tie to the event: the DJ's grant there does not reach
    # them, so it is decided on roles alone, and refused.
    assert (
        await _upload_chunk(client, w.dj, stranger.id, w.event_a)
    ).status_code == 403
    assert (
        await _upload_chunk(client, w.dj, w.target.id, "evt_none")
    ).status_code == 403
    # An admin needs no event_id, and may act for anyone.
    assert (await _upload_chunk(client, w.admin, w.target.id, None)).status_code == 200
    assert (
        await _upload_chunk(client, w.admin, stranger.id, w.event_a)
    ).status_code == 200

    audited = await db.fetch(
        "SELECT subject, resource, allowed FROM identity_audit_events"
        " WHERE scope = $1 ORDER BY occurred_at",
        DELEGATION,
    )
    assert [(r["subject"], r["resource"], r["allowed"]) for r in audited] == [
        (w.dj.id, w.target.id, False),
        (w.dj.id, f"event:{w.event_a}/on-behalf-of/{w.target.id}", True),
        (w.dj.id, stranger.id, False),
        (w.dj.id, w.target.id, False),
        (w.admin.id, w.target.id, True),
        (w.admin.id, stranger.id, True),
    ]


async def test_a_dj_cannot_check_in_someone_with_no_tie_to_the_event(
    client: httpx.AsyncClient,
    db: asyncpg.Connection,
    world: World,
    person: Callable[..., Any],
) -> None:
    w = world
    stranger = await person("stranger")
    now = _now()
    session_id = await insert_floor_session(
        db,
        event_id=w.event_a,
        status="checkin_open",
        checkin_opens_at=now - HOUR,
        starts_at=now + HOUR,
        ends_at=now + 2 * HOUR,
    )
    pair = await dancer_pair(db, user_id=stranger.id)
    res = await client.post(
        "/v1/checkins",
        json={
            "sessionId": session_id,
            "divisionName": "Classic",
            "songId": pair["song_id"],
            "on_behalf_of_user_id": stranger.id,
        },
        headers=w.dj.headers,
    )
    assert res.status_code == 403, res.text
    assert (
        await db.fetchval(
            "SELECT count(*) FROM checkins WHERE session_id = $1", session_id
        )
        == 0
    )


async def test_a_dj_checks_in_a_dancer_and_is_named_as_actor(
    client: httpx.AsyncClient, db: asyncpg.Connection, world: World
) -> None:
    w = world
    res = await _checkin_for(client, db, w, w.dj, w.event_a)
    assert res.status_code == 201, res.text
    checkin = await db.fetchrow(
        "SELECT session_id, submitted_by_user_id FROM checkins WHERE id = $1",
        res.json()["data"]["id"],
    )
    assert checkin["submitted_by_user_id"] == w.target.id
    actor = await db.fetchval(
        "SELECT actor_user_id FROM queue_events WHERE session_id = $1",
        checkin["session_id"],
    )
    assert actor == w.dj.id


async def test_a_dj_gets_no_admin_route(
    client: httpx.AsyncClient, world: World
) -> None:
    w = world
    for path in (
        "/v1/admin/users",
        "/v1/admin/songs",
        "/v1/admin/drive-jobs",
        "/v1/admin/checkins/test",
        f"/v1/admin/event-song-submissions?event_id={w.event_a}",
    ):
        res = await client.get(path, headers=w.dj.headers)
        assert res.status_code == 403, path
    res = await client.patch(
        f"/v1/events/{w.event_a}", json={"name": "Mine"}, headers=w.dj.headers
    )
    assert res.status_code == 403
