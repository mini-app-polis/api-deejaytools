"""/v1/admin/event-song-submissions: every submission to one event."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import asyncpg
import httpx
import pytest
from sqlalchemy.exc import OperationalError

from api_deejaytools.routers import admin_event_submissions

from .content_helpers import (
    drive_jobs,
    insert_checkin,
    insert_event,
    insert_session,
    insert_song,
    new_id,
)

URL = "/v1/admin/event-song-submissions"


async def _submit(
    db: asyncpg.Connection,
    sid: str,
    event_id: str,
    song_id: str,
    user_id: str,
    created_at: int,
    division: str | None = None,
    round_: str | None = None,
) -> None:
    await db.execute(
        "INSERT INTO event_song_submissions (id, event_id, song_id,"
        " submitted_by_user_id, created_at, division, round)"
        " VALUES ($1, $2, $3, $4, $5, $6, $7)",
        sid,
        event_id,
        song_id,
        user_id,
        created_at,
        division,
        round_,
    )


async def test_requires_a_token_and_the_scope(
    client: httpx.AsyncClient, person: Callable
) -> None:
    alice = await person("alice")
    res = await client.get(f"{URL}?event_id=e")
    assert res.status_code == 401
    assert res.json()["error"]["code"] == "UNAUTHORIZED"
    res = await client.get(URL, headers=alice.headers)
    assert res.status_code == 403
    assert res.json()["error"] == {
        "code": "FORBIDDEN",
        "message": "Admin access required",
    }


async def test_shape_labels_and_order(
    client: httpx.AsyncClient, person: Callable, db: asyncpg.Connection
) -> None:
    admin = await person("admin", admin=True)
    alice = await person("alice")
    bob = await person("bob")
    event = await insert_event(db, name="RSC 2026")
    other = await insert_event(db, name="Elsewhere")
    await db.execute(
        "INSERT INTO partners (id, user_id, first_name, last_name, created_at,"
        " updated_at) VALUES ('p1', $1, 'Bob', 'Jones', 1, 1)",
        alice.id,
    )
    await db.execute(
        "INSERT INTO managed_partnerships (id, user_id, leader_first_name,"
        " leader_last_name, follower_first_name, follower_last_name, created_at,"
        " updated_at) VALUES ('mp1', $1, 'Lee', 'Der', 'Fol', 'Lower', 1, 1)",
        bob.id,
    )
    partnered = await insert_song(
        db,
        alice.id,
        partner_id="p1",
        division="Classic",
        season_year="2026",
        routine_name="Routine",
        processed_filename="x_v02.mp3",
    )
    managed = await insert_song(
        db, bob.id, managed_partnership_id="mp1", division="Showcase"
    )
    # The division it is entered under wins over the song's own.
    await _submit(db, "s_old", event, partnered, alice.id, 10, division="Masters")
    await _submit(db, "s_new", event, managed, bob.id, 20)
    await _submit(db, "s_other", other, partnered, alice.id, 30)

    res = await client.get(URL, params={"event_id": event}, headers=admin.headers)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["meta"] == {"version": "v1", "count": 2, "total": 2}
    assert body["data"] == [
        {
            "id": "s_new",
            "event_id": event,
            "event_name": "RSC 2026",
            "division": "Showcase",
            "round": "prelims_and_finals",
            "song_id": managed,
            "owner_user_id": bob.id,
            "song_label": "Lee Der & Fol Lower Showcase",
            "partnership_label": "Lee Der & Fol Lower",
            "duration_ms": None,
            "submitter_email": f"bob.{bob.id}@example.test",
            "created_at": 20,
        },
        {
            "id": "s_old",
            "event_id": event,
            "event_name": "RSC 2026",
            "division": "Masters",
            "round": "prelims_and_finals",
            "song_id": partnered,
            "owner_user_id": alice.id,
            "song_label": "Alice & Bob Jones Masters 2026 Routine v02",
            "partnership_label": "Alice & Bob Jones",
            "duration_ms": None,
            "submitter_email": f"alice.{alice.id}@example.test",
            "created_at": 10,
        },
    ]


async def test_label_reads_leader_first_and_proam_am_first(
    client: httpx.AsyncClient, person: Callable, db: asyncpg.Connection
) -> None:
    admin = await person("admin", admin=True)
    fay = await person("fay")
    event = await insert_event(db, name="RSC 2026")
    await db.execute(
        "INSERT INTO partners (id, user_id, first_name, last_name, partner_role,"
        " created_at, updated_at) VALUES ('lead1', $1, 'Lee', 'Lead', 'leader', 1, 1)",
        fay.id,
    )
    classic = await insert_song(db, fay.id, partner_id="lead1", division="Classic")
    proam = await insert_song(
        db, fay.id, partner_id="lead1", division="ProAm FollowerAm"
    )
    await _submit(db, "s_classic", event, classic, fay.id, 10)
    await _submit(db, "s_proam", event, proam, fay.id, 20)

    res = await client.get(URL, params={"event_id": event}, headers=admin.headers)

    assert res.status_code == 200, res.text
    labels = {r["id"]: r["partnership_label"] for r in res.json()["data"]}
    assert labels == {"s_classic": "Lee Lead & Fay", "s_proam": "Fay & Lee Lead"}


async def test_rows_carry_the_round_and_the_entered_division(
    client: httpx.AsyncClient, person: Callable, db: asyncpg.Connection
) -> None:
    admin = await person("admin", admin=True)
    fay = await person("fay")
    event = await insert_event(db, name="The Open 2026")
    await db.execute(
        "INSERT INTO partners (id, user_id, first_name, last_name, partner_role,"
        " created_at, updated_at) VALUES ('lead1', $1, 'Lee', 'Lead', 'leader', 1, 1)",
        fay.id,
    )
    prelims = await insert_song(db, fay.id, partner_id="lead1", division="Exhibition")
    finals = await insert_song(db, fay.id, partner_id="lead1", division="Classic")
    await _submit(
        db, "s_prelims", event, prelims, fay.id, 10, "Classic", "prelims_only"
    )
    await _submit(db, "s_finals", event, finals, fay.id, 20, round_="finals_only")

    res = await client.get(URL, params={"event_id": event}, headers=admin.headers)

    assert res.status_code == 200, res.text
    rows = {r["id"]: (r["division"], r["round"]) for r in res.json()["data"]}
    assert rows == {
        "s_prelims": ("Classic", "prelims_only"),
        "s_finals": ("Classic", "finals_only"),
    }


async def test_unknown_event_is_an_empty_list(
    client: httpx.AsyncClient, person: Callable
) -> None:
    admin = await person("admin", admin=True)
    res = await client.get(URL, params={"event_id": "evt_none"}, headers=admin.headers)
    assert res.status_code == 200
    assert res.json() == {"data": [], "meta": {"version": "v1", "count": 0, "total": 0}}


async def test_requires_event_id(client: httpx.AsyncClient, person: Callable) -> None:
    admin = await person("admin", admin=True)
    for qs in ("", "?event_id=", "?event_id=a&event_id=b"):
        res = await client.get(f"{URL}{qs}", headers=admin.headers)
        assert res.status_code == 400, qs
        assert res.json()["error"]["code"] == "VALIDATION_ERROR"


async def test_a_failed_query_is_internal(
    client: httpx.AsyncClient,
    person: Callable,
    db: asyncpg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admin = await person("admin", admin=True)
    event = await insert_event(db)
    song = await insert_song(db, admin.id)
    await _submit(db, "s1", event, song, admin.id, 1)

    def boom(**_: object) -> str:
        raise OperationalError("boom", {}, Exception("boom"))

    monkeypatch.setattr(admin_event_submissions, "build_structured_song_label", boom)
    res = await client.get(URL, params={"event_id": event}, headers=admin.headers)
    assert res.status_code == 500
    assert res.json() == {
        "error": {"code": "INTERNAL", "message": "Internal server error"}
    }


# --- PATCH /v1/admin/event-song-submissions/{id}: move to another division ---


async def _move(
    client: httpx.AsyncClient, who: Any, sid: str, **body: Any
) -> httpx.Response:
    return await client.patch(f"{URL}/{sid}", json=body, headers=who.headers)


async def _couple(db: asyncpg.Connection, user_id: str) -> str:
    """Joe Carey's partner Stephanie Meyer; returns the partner id."""
    pid = new_id("p")
    await db.execute(
        "INSERT INTO partners (id, user_id, first_name, last_name, created_at,"
        " updated_at) VALUES ($1, $2, 'Stephanie', 'Meyer', 1, 1)",
        pid,
        user_id,
    )
    return pid


async def _checked_in(
    db: asyncpg.Connection,
    event_id: str,
    song_id: str,
    user_id: str,
    division: str,
    *,
    status: str = "scheduled",
) -> None:
    session_id = await insert_session(db, status=status, division=division)
    await db.execute(
        "UPDATE sessions SET event_id = $1 WHERE id = $2", event_id, session_id
    )
    await insert_checkin(
        db,
        session_id=session_id,
        song_id=song_id,
        submitted_by=user_id,
        division=division,
    )


async def test_move_needs_a_token_and_the_admin_scope(
    client: httpx.AsyncClient, person: Callable, db: asyncpg.Connection
) -> None:
    joe = await person("joe")
    event = await insert_event(db)
    song = await insert_song(db, joe.id, division="Sophisticated")
    await _submit(db, "s1", event, song, joe.id, 1)

    res = await client.patch(f"{URL}/s1", json={"division": "Masters"})
    assert res.status_code == 401
    # Even the dancer whose entry it is: a move is the admin's only.
    res = await _move(client, joe, "s1", division="Masters")
    assert res.status_code == 403
    assert res.json()["error"] == {
        "code": "FORBIDDEN",
        "message": "Admin access required",
    }
    assert (
        await db.fetchval("SELECT division FROM event_song_submissions WHERE id = 's1'")
        is None
    )
    assert await drive_jobs(db) == []


async def test_move_changes_the_entry_in_place_and_queues_one_drive_move(
    client: httpx.AsyncClient, person: Callable, db: asyncpg.Connection
) -> None:
    admin = await person("admin", admin=True)
    joe = await person("joe")
    pid = await _couple(db, joe.id)
    event = await insert_event(db, name="The Open 2026")
    song = await insert_song(
        db,
        joe.id,
        partner_id=pid,
        division="Sophisticated",
        processed_filename="Joe_StephanieMeyer_Sophisticated_2026_v01.mp3",
    )
    await _submit(db, "s1", event, song, joe.id, 10)
    await db.execute(
        "UPDATE event_song_submissions SET drive_copy_file_id = 'copy_1'"
        " WHERE id = 's1'"
    )

    res = await _move(client, admin, "s1", division="Masters")

    assert res.status_code == 200, res.text
    body = res.json()
    assert body["meta"] == {"version": "v1", "count": 1, "total": 1}
    data = body["data"]
    assert (data["id"], data["division"], data["round"]) == (
        "s1",
        "Masters",
        "prelims_and_finals",
    )
    assert data["partnership_label"] == "Joe & Stephanie Meyer"
    # The same row, its copy and submitter kept; the song's own division too.
    row = await db.fetchrow(
        "SELECT id, division, round, drive_copy_file_id, submitted_by_user_id,"
        " created_at FROM event_song_submissions"
    )
    assert dict(row) == {
        "id": "s1",
        "division": "Masters",
        "round": None,
        "drive_copy_file_id": "copy_1",
        "submitted_by_user_id": joe.id,
        "created_at": 10,
    }
    assert (
        await db.fetchval("SELECT division FROM songs WHERE id = $1", song)
        == "Sophisticated"
    )
    # One Drive job moves and renames the copy; nothing is copied or trashed.
    assert await drive_jobs(db) == [("move", "s1", None, "pending")]

    # The event's lists show it in the new division.
    res = await client.get(URL, params={"event_id": event}, headers=admin.headers)
    assert [(r["id"], r["division"]) for r in res.json()["data"]] == [("s1", "Masters")]
    res = await client.get(f"/v1/events/{event}/entities", headers=joe.headers)
    assert res.status_code == 200, res.text
    divisions = {d["division"] for d in res.json()["data"]}
    assert "Masters" in divisions
    assert "Sophisticated" not in divisions


async def test_move_drops_the_hide_of_a_scratched_entry_in_the_old_division(
    client: httpx.AsyncClient, person: Callable, db: asyncpg.Connection
) -> None:
    admin = await person("admin", admin=True)
    joe = await person("joe")
    event = await insert_event(db)
    song = await insert_song(db, joe.id, division="Sophisticated")
    await _submit(db, "s1", event, song, joe.id, 1)
    await db.execute(
        "INSERT INTO event_registrations (id, event_id, position, division,"
        " dancer_1, entry_key, scratched, created_at, updated_at)"
        " VALUES ('r1', $1, 0, 'Sophisticated', 'Joe', 'k', true, 1, 1)",
        event,
    )
    await db.execute(
        "UPDATE event_song_submissions SET hidden_by_registration_id = 'r1'"
    )

    res = await _move(client, admin, "s1", division="Masters")

    assert res.status_code == 200, res.text
    assert (
        await db.fetchval(
            "SELECT hidden_by_registration_id FROM event_song_submissions"
        )
        is None
    )
    # No registration is linked by the move: the web app matches by name.
    assert await db.fetchval("SELECT linked_label FROM event_registrations") is None


async def test_move_out_of_classic_at_the_open_resets_the_round(
    client: httpx.AsyncClient, person: Callable, db: asyncpg.Connection
) -> None:
    admin = await person("admin", admin=True)
    joe = await person("joe")
    event = await insert_event(db, name="The Open 2026")
    song = await insert_song(db, joe.id, division="Classic")
    await _submit(db, "s1", event, song, joe.id, 1, round_="finals_only")

    res = await _move(client, admin, "s1", division="Showcase")

    assert res.status_code == 200, res.text
    assert res.json()["data"]["round"] == "prelims_and_finals"
    assert await db.fetchval("SELECT round FROM event_song_submissions") is None


async def test_move_is_refused_while_checked_in_in_the_old_division(
    client: httpx.AsyncClient, person: Callable, db: asyncpg.Connection
) -> None:
    admin = await person("admin", admin=True)
    joe = await person("joe")
    event = await insert_event(db)
    song = await insert_song(db, joe.id, division="Sophisticated")
    await _submit(db, "s1", event, song, joe.id, 1)
    await _checked_in(db, event, song, joe.id, "Sophisticated")

    res = await _move(client, admin, "s1", division="Masters")

    assert res.status_code == 409
    assert res.json()["error"] == {
        "code": "SUBMISSION_CHECKED_IN",
        "message": "This entry is checked in to a session in Sophisticated. "
        "Complete or withdraw the check-in before moving it.",
    }
    assert await db.fetchval("SELECT division FROM event_song_submissions") is None
    assert await drive_jobs(db) == []


async def test_a_finished_or_other_division_check_in_does_not_block_a_move(
    client: httpx.AsyncClient, person: Callable, db: asyncpg.Connection
) -> None:
    admin = await person("admin", admin=True)
    joe = await person("joe")
    event = await insert_event(db)
    song = await insert_song(db, joe.id, division="Sophisticated")
    await _submit(db, "s1", event, song, joe.id, 1)
    await _checked_in(db, event, song, joe.id, "Sophisticated", status="completed")
    await _checked_in(db, event, song, joe.id, "Showcase")
    other = await insert_event(db, name="Elsewhere")
    await _checked_in(db, other, song, joe.id, "Sophisticated")

    res = await _move(client, admin, "s1", division="Masters")

    assert res.status_code == 200, res.text


async def test_move_is_refused_when_the_partnership_has_a_song_in_the_new_division(
    client: httpx.AsyncClient, person: Callable, db: asyncpg.Connection
) -> None:
    admin = await person("admin", admin=True)
    joe = await person("joe")
    pid = await _couple(db, joe.id)
    event = await insert_event(db)
    soph = await insert_song(db, joe.id, partner_id=pid, division="Sophisticated")
    masters = await insert_song(db, joe.id, partner_id=pid, division="Masters")
    await _submit(db, "s_soph", event, soph, joe.id, 1)
    await _submit(db, "s_masters", event, masters, joe.id, 2)

    res = await _move(client, admin, "s_soph", division="Masters")

    assert res.status_code == 409
    assert res.json()["error"] == {
        "code": "ENTITY_SLOT_TAKEN",
        "message": "This entity already has a song submitted for Masters. "
        "Remove it before adding another.",
    }
    assert (
        await db.fetchval(
            "SELECT division FROM event_song_submissions WHERE id = 's_soph'"
        )
        is None
    )
    assert await drive_jobs(db) == []


async def test_move_into_classic_at_the_open_is_checked_by_round(
    client: httpx.AsyncClient, person: Callable, db: asyncpg.Connection
) -> None:
    admin = await person("admin", admin=True)
    joe = await person("joe")
    pid = await _couple(db, joe.id)
    event = await insert_event(db, name="The Open 2026")
    prelims = await insert_song(db, joe.id, partner_id=pid, division="Classic")
    other = await insert_song(db, joe.id, partner_id=pid, division="Sophisticated")
    await _submit(db, "s_prelims", event, prelims, joe.id, 1, round_="prelims_only")
    await _submit(db, "s_other", event, other, joe.id, 2)

    # Both rounds overlap the prelims song already there.
    res = await _move(client, admin, "s_other", division="Classic")
    assert res.status_code == 409
    assert res.json()["error"]["code"] == "ENTITY_SLOT_TAKEN"

    res = await _move(client, admin, "s_other", division="Classic", round="finals_only")
    assert res.status_code == 200, res.text
    assert (res.json()["data"]["division"], res.json()["data"]["round"]) == (
        "Classic",
        "finals_only",
    )


async def test_move_rounds_are_for_classic_at_the_open_only(
    client: httpx.AsyncClient, person: Callable, db: asyncpg.Connection
) -> None:
    admin = await person("admin", admin=True)
    joe = await person("joe")
    open_event = await insert_event(db, name="The Open 2026")
    other_event = await insert_event(db, name="Swing Fling")
    a = await insert_song(db, joe.id, division="Showcase")
    b = await insert_song(db, joe.id, division="Showcase")
    await _submit(db, "s_open", open_event, a, joe.id, 1)
    await _submit(db, "s_other", other_event, b, joe.id, 2)

    for sid, division in (("s_open", "Masters"), ("s_other", "Classic")):
        res = await _move(client, admin, sid, division=division, round="finals_only")
        assert res.status_code == 400, sid
        assert res.json()["error"]["code"] == "BAD_REQUEST"
    assert await drive_jobs(db) == []


async def test_move_bad_requests(
    client: httpx.AsyncClient, person: Callable, db: asyncpg.Connection
) -> None:
    admin = await person("admin", admin=True)
    joe = await person("joe")
    event = await insert_event(db)
    song = await insert_song(db, joe.id, division="Sophisticated")
    await _submit(db, "s1", event, song, joe.id, 1)

    res = await _move(client, admin, "s1", division="Sophisticated")
    assert res.status_code == 400
    assert res.json()["error"] == {
        "code": "BAD_REQUEST",
        "message": "This entry is already in Sophisticated.",
    }
    for body in ({}, {"division": "Not A Division"}, {"division": " Masters "}):
        res = await client.patch(f"{URL}/s1", json=body, headers=admin.headers)
        assert res.status_code == 400, body
        assert res.json()["error"]["code"] == "VALIDATION_ERROR"
    res = await _move(client, admin, "nope", division="Masters")
    assert res.status_code == 404
    assert res.json()["error"]["code"] == "NOT_FOUND"
    assert await drive_jobs(db) == []
