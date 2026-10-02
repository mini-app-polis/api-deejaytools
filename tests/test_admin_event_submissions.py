"""/v1/admin/event-song-submissions: every submission to one event."""

from __future__ import annotations

from collections.abc import Callable

import asyncpg
import httpx
import pytest

from api_deejaytools.routers import admin_event_submissions

from .content_helpers import insert_event, insert_song

URL = "/v1/admin/event-song-submissions"


async def _submit(
    db: asyncpg.Connection,
    sid: str,
    event_id: str,
    song_id: str,
    user_id: str,
    created_at: int,
    division: str | None = None,
) -> None:
    await db.execute(
        "INSERT INTO event_song_submissions (id, event_id, song_id,"
        " submitted_by_user_id, created_at, division)"
        " VALUES ($1, $2, $3, $4, $5, $6)",
        sid,
        event_id,
        song_id,
        user_id,
        created_at,
        division,
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
    # The submission's own division override is not what this list shows.
    await _submit(db, "s_old", event, partnered, alice.id, 10, division="Masters")
    await _submit(db, "s_new", event, managed, bob.id, 20)
    await _submit(db, "s_other", other, partnered, alice.id, 30)

    res = await client.get(URL, params={"event_id": event}, headers=admin.headers)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["meta"] == {"version": "v1", "count": 2}
    assert body["data"] == [
        {
            "id": "s_new",
            "event_id": event,
            "event_name": "RSC 2026",
            "division": "Showcase",
            "song_id": managed,
            "song_label": "Lee Der & Fol Lower Showcase",
            "partnership_label": "Lee Der & Fol Lower",
            "submitter_email": f"bob.{bob.id}@example.test",
            "created_at": 20,
        },
        {
            "id": "s_old",
            "event_id": event,
            "event_name": "RSC 2026",
            "division": "Classic",
            "song_id": partnered,
            "song_label": "Alice & Bob Jones Classic 2026 Routine v02",
            "partnership_label": "Alice & Bob Jones",
            "submitter_email": f"alice.{alice.id}@example.test",
            "created_at": 10,
        },
    ]


async def test_unknown_event_is_an_empty_list(
    client: httpx.AsyncClient, person: Callable
) -> None:
    admin = await person("admin", admin=True)
    res = await client.get(URL, params={"event_id": "evt_none"}, headers=admin.headers)
    assert res.status_code == 200
    assert res.json() == {"data": [], "meta": {"version": "v1", "count": 0}}


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
        raise RuntimeError("boom")

    monkeypatch.setattr(admin_event_submissions, "build_structured_song_label", boom)
    res = await client.get(URL, params={"event_id": event}, headers=admin.headers)
    assert res.status_code == 500
    assert res.json() == {
        "error": {"code": "INTERNAL", "message": "Internal server error"}
    }
