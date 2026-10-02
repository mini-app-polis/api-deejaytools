"""/v1/event-song-submissions: a caller's songs entered in events."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import asyncpg
import httpx

from api_deejaytools.routers.events import compute_status

from .content_helpers import (
    drive_jobs,
    insert_event,
    insert_song,
)

URL = "/v1/event-song-submissions"


async def _submit(client: httpx.AsyncClient, who: Any, **body: Any) -> httpx.Response:
    return await client.post(URL, json=body, headers=who.headers)


async def _partner(db: asyncpg.Connection, user_id: str, pid: str = "pat") -> str:
    await db.execute(
        "INSERT INTO partners (id, user_id, first_name, last_name, created_at,"
        " updated_at) VALUES ($1, $2, 'Pat', 'Partner', 1, 1)",
        pid,
        user_id,
    )
    return pid


async def test_submit_shape_and_copy_job(
    client: httpx.AsyncClient, person: Callable, db: asyncpg.Connection
) -> None:
    alice = await person("alice")
    pid = await _partner(db, alice.id)
    event = await insert_event(db, name="Swing Fling")
    song = await insert_song(
        db,
        alice.id,
        partner_id=pid,
        division="Classic",
        season_year="2026",
        routine_name="Blue",
        processed_filename="Alice_PatPartner_Classic_2026_Blue_v03.mp3",
    )
    res = await _submit(client, alice, event_id=event, song_id=song)
    assert res.status_code == 201, res.text
    data = res.json()["data"]
    assert data == {
        "id": data["id"],
        "event_id": event,
        "event_name": "Swing Fling",
        "event_start_date": "2026-05-01",
        "event_status": compute_status("2026-05-01", "2026-05-03", "America/Chicago"),
        "song_id": song,
        "song_label": "Alice & Pat Partner Classic 2026 Blue v03",
        "division": "Classic",
        "round": "prelims_and_finals",
        "created_at": data["created_at"],
    }
    assert res.json()["meta"] == {"version": "v1"}
    stored = await db.fetchrow(
        "SELECT division, round, submitted_by_user_id FROM event_song_submissions"
    )
    assert dict(stored) == {
        "division": None,
        "round": None,
        "submitted_by_user_id": alice.id,
    }
    assert await drive_jobs(db) == [("copy", data["id"], None, "pending")]


async def test_labels_for_managed_and_unstructured_songs(
    client: httpx.AsyncClient, person: Callable, db: asyncpg.Connection
) -> None:
    alice = await person("alice")
    await db.execute(
        "INSERT INTO managed_partnerships (id, user_id, leader_first_name,"
        " leader_last_name, follower_first_name, follower_last_name, created_at,"
        " updated_at) VALUES ('mp1', $1, 'Lee', 'L', 'Fay', 'F', 1, 1)",
        alice.id,
    )
    event = await insert_event(db)
    managed = await insert_song(
        db, alice.id, managed_partnership_id="mp1", division="Showcase"
    )
    bare = await insert_song(db, alice.id)
    await db.execute("UPDATE songs SET display_name = ' Tune ' WHERE id = $1", bare)
    r1 = await _submit(client, alice, event_id=event, song_id=managed)
    r2 = await _submit(client, alice, event_id=event, song_id=bare)
    assert r1.json()["data"]["song_label"] == "Lee L & Fay F Showcase"
    assert r2.json()["data"]["song_label"] == "Tune"
    assert r2.json()["data"]["division"] is None


async def test_not_found_and_validation(
    client: httpx.AsyncClient, person: Callable, db: asyncpg.Connection
) -> None:
    alice = await person("alice")
    bob = await person("bob")
    event = await insert_event(db)
    song = await insert_song(db, alice.id, division="Classic")

    res = await _submit(client, bob, event_id=event, song_id=song)
    assert res.status_code == 404
    assert res.json()["error"] == {"code": "NOT_FOUND", "message": "Song not found"}
    res = await _submit(client, alice, event_id="nope", song_id=song)
    assert res.status_code == 404
    assert res.json()["error"] == {"code": "NOT_FOUND", "message": "Event not found"}

    for body in (
        {"event_id": "", "song_id": song},
        {"event_id": event},
        {"event_id": event, "song_id": song, "division": "classic"},
        {"event_id": event, "song_id": song, "round": "semis"},
        {"event_id": event, "song_id": song, "division": None},
    ):
        res = await client.post(URL, json=body, headers=alice.headers)
        assert res.status_code == 400, body
        assert res.json()["error"]["code"] == "VALIDATION_ERROR"


async def test_duplicate_and_entity_slot(
    client: httpx.AsyncClient, person: Callable, db: asyncpg.Connection
) -> None:
    alice = await person("alice")
    event = await insert_event(db)
    first = await insert_song(db, alice.id, division="Classic")
    second = await insert_song(db, alice.id, division=" Classic ")
    other_division = await insert_song(db, alice.id, division="Showcase")
    assert (
        await _submit(client, alice, event_id=event, song_id=first)
    ).status_code == 201

    dup = await _submit(client, alice, event_id=event, song_id=first)
    assert dup.status_code == 409
    assert dup.json()["error"] == {
        "code": "conflict",
        "message": "That song is already submitted to this event.",
    }
    taken = await _submit(client, alice, event_id=event, song_id=second)
    assert taken.status_code == 409
    assert taken.json()["error"] == {
        "code": "ENTITY_SLOT_TAKEN",
        "message": "This entity already has a song submitted for Classic. "
        "Remove it before adding another.",
    }
    # An override moves it into a free slot; it is stored.
    moved = await _submit(
        client, alice, event_id=event, song_id=second, division="Masters"
    )
    assert moved.status_code == 201
    assert moved.json()["data"]["division"] == "Masters"
    assert (
        await _submit(client, alice, event_id=event, song_id=other_division)
    ).status_code == 201
    assert len(await drive_jobs(db)) == 3

    blank_a = await insert_song(db, alice.id)
    blank_b = await insert_song(db, alice.id)
    assert (
        await _submit(client, alice, event_id=event, song_id=blank_a)
    ).status_code == 201
    res = await _submit(client, alice, event_id=event, song_id=blank_b)
    assert res.json()["error"]["message"] == (
        "This entity already has a song submitted for this division. "
        "Remove it before adding another."
    )


async def test_rounds_only_for_classic_at_the_open(
    client: httpx.AsyncClient, person: Callable, db: asyncpg.Connection
) -> None:
    alice = await person("alice")
    elsewhere = await insert_event(db, name="Open Practice Night")
    the_open = await insert_event(db, name="THE OPEN 2026")
    prelims = await insert_song(db, alice.id, division="Classic")
    finals = await insert_song(db, alice.id, division="Classic")
    both = await insert_song(db, alice.id, division="Classic")
    showcase = await insert_song(db, alice.id, division="Showcase")

    res = await _submit(
        client, alice, event_id=elsewhere, song_id=prelims, round="prelims_only"
    )
    assert res.status_code == 400
    assert res.json()["error"] == {
        "code": "BAD_REQUEST",
        "message": "Round selection is only available for The Open",
    }
    res = await _submit(
        client, alice, event_id=the_open, song_id=showcase, round="finals_only"
    )
    assert res.json()["error"] == {
        "code": "BAD_REQUEST",
        "message": "Round selection is only available for the Classic division",
    }

    res = await _submit(
        client, alice, event_id=the_open, song_id=prelims, round="prelims_only"
    )
    assert res.status_code == 201
    assert res.json()["data"]["round"] == "prelims_only"
    res = await _submit(
        client, alice, event_id=the_open, song_id=finals, round="finals_only"
    )
    assert res.status_code == 201
    res = await _submit(client, alice, event_id=the_open, song_id=both)
    assert res.status_code == 409
    assert res.json()["error"]["code"] == "ENTITY_SLOT_TAKEN"


async def test_list_own_newest_first_with_filter(
    client: httpx.AsyncClient, person: Callable, db: asyncpg.Connection
) -> None:
    alice = await person("alice")
    bob = await person("bob")
    e1 = await insert_event(db, name="One")
    e2 = await insert_event(db, name="Two")
    s1 = await insert_song(db, alice.id, division="Classic")
    s2 = await insert_song(db, alice.id, division="Classic")
    sb = await insert_song(db, bob.id, division="Classic")
    await db.execute(
        "INSERT INTO event_song_submissions (id, event_id, song_id,"
        " submitted_by_user_id, created_at, round) VALUES"
        " ('a1', $1, $3, $5, 10, NULL), ('a2', $2, $4, $5, 20, 'finals_only'),"
        " ('b1', $1, $6, $7, 30, NULL)",
        e1,
        e2,
        s1,
        s2,
        alice.id,
        sb,
        bob.id,
    )
    res = await client.get(URL, headers=alice.headers)
    assert res.json()["meta"] == {"version": "v1", "count": 2}
    assert [s["id"] for s in res.json()["data"]] == ["a2", "a1"]
    assert res.json()["data"][0]["round"] == "finals_only"
    filtered = await client.get(URL, params={"event_id": e1}, headers=alice.headers)
    assert [s["id"] for s in filtered.json()["data"]] == ["a1"]
    everything = await client.get(URL, params={"event_id": ""}, headers=alice.headers)
    assert everything.json()["meta"]["count"] == 2


async def test_delete(
    client: httpx.AsyncClient, person: Callable, db: asyncpg.Connection
) -> None:
    alice = await person("alice")
    bob = await person("bob")
    event = await insert_event(db)
    song = await insert_song(db, alice.id)
    plain = await insert_song(db, alice.id, division="Showcase")
    await db.execute(
        "INSERT INTO event_song_submissions (id, event_id, song_id,"
        " submitted_by_user_id, drive_copy_file_id, created_at) VALUES"
        " ('with-copy', $1, $2, $4, 'copy-1', 1), ('no-copy', $1, $3, $4, NULL, 1)",
        event,
        song,
        plain,
        alice.id,
    )
    res = await client.delete(f"{URL}/with-copy", headers=bob.headers)
    assert res.status_code == 404
    assert res.json()["error"] == {
        "code": "NOT_FOUND",
        "message": "Event song submission not found",
    }

    res = await client.delete(f"{URL}/with-copy", headers=alice.headers)
    assert res.status_code == 204
    assert res.content == b""
    res = await client.delete(f"{URL}/no-copy", headers=alice.headers)
    assert res.status_code == 204
    assert await db.fetchval("SELECT count(*) FROM event_song_submissions") == 0
    assert await drive_jobs(db) == [("trash", None, "copy-1", "pending")]


async def test_auth_required(client: httpx.AsyncClient) -> None:
    for method, path in (
        ("GET", f"{URL}?event_id=a&event_id=b"),
        ("POST", URL),
        ("DELETE", f"{URL}/x"),
    ):
        res = await client.request(method, path, json={})
        assert res.status_code == 401, path
        assert res.json()["error"]["code"] == "UNAUTHORIZED"
