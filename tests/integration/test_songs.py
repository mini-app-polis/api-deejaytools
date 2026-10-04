"""/v1/songs (without the chunked upload): a caller's own song library."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import asyncpg
import httpx

from .content_helpers import (
    drive_jobs,
    insert_checkin,
    insert_event,
    insert_session,
    insert_song,
)

NOT_FOUND = {"code": "NOT_FOUND", "message": "Song not found"}
BAD_PARTNER = {
    "code": "BAD_REQUEST",
    "message": "Partner not found or does not belong to you",
}
SONG_KEYS = {
    "id",
    "user_id",
    "partner_id",
    "display_name",
    "original_filename",
    "drive_file_id",
    "drive_folder_id",
    "processed_filename",
    "division",
    "routine_name",
    "personal_descriptor",
    "season_year",
    "is_legacy",
    "created_at",
    "updated_at",
    "partner_first_name",
    "partner_last_name",
    "partner_kind",
    "managed_partnership_id",
    "managed_leader_first_name",
    "managed_leader_last_name",
    "managed_follower_first_name",
    "managed_follower_last_name",
}


async def _partner(client: httpx.AsyncClient, who: Any) -> str:
    res = await client.post(
        "/v1/partners",
        json={"first_name": "Pat", "last_name": "P", "partner_role": "follower"},
        headers=who.headers,
    )
    return res.json()["data"]["id"]


async def _create(client: httpx.AsyncClient, who: Any, **body: Any) -> dict:
    res = await client.post(
        "/v1/songs", json={"division": "Classic", **body}, headers=who.headers
    )
    assert res.status_code == 201, res.text
    return res.json()["data"]


async def test_create_shape(client: httpx.AsyncClient, person: Callable) -> None:
    alice = await person("alice")
    pid = await _partner(client, alice)
    created = await _create(
        client,
        alice,
        partner_id=pid,
        routine_name=" Blue ",
        original_filename=" blue.mp3 ",
        personal_descriptor="  ",
        season_year=" 2026 ",
        division=" Classic ",
    )
    assert set(created) == SONG_KEYS
    assert created == {
        **created,
        "user_id": alice.id,
        "partner_id": pid,
        "display_name": "Blue",
        "original_filename": "blue.mp3",
        "drive_file_id": None,
        "drive_folder_id": None,
        "processed_filename": None,
        "division": "Classic",
        "routine_name": "Blue",
        "personal_descriptor": None,
        "season_year": "2026",
        "is_legacy": False,
        "updated_at": created["created_at"],
        # Not joined on create, as in deejaytools-api.
        "partner_first_name": None,
        "partner_last_name": None,
        "partner_kind": None,
        "managed_partnership_id": None,
        "managed_leader_first_name": None,
    }

    one = await client.get(f"/v1/songs/{created['id']}", headers=alice.headers)
    data = one.json()["data"]
    assert (data["partner_first_name"], data["partner_last_name"]) == ("Pat", "P")
    assert data["partner_kind"] == "partner"


async def test_create_validation_and_partner_check(
    client: httpx.AsyncClient, person: Callable
) -> None:
    alice = await person("alice")
    bob = await person("bob")
    for body in (
        {},
        {"division": ""},
        {"division": "Classic", "season_year": None},
        {"division": "Classic", "display_name": 3},
    ):
        res = await client.post("/v1/songs", json=body, headers=alice.headers)
        assert res.status_code == 400, body
        assert res.json()["error"]["code"] == "VALIDATION_ERROR"
    # routine_name and personal_descriptor are nullable.
    await _create(client, alice, routine_name=None, personal_descriptor=None)

    bobs_partner = await _partner(client, bob)
    res = await client.post(
        "/v1/songs",
        json={"division": "Classic", "partner_id": bobs_partner},
        headers=alice.headers,
    )
    assert (res.status_code, res.json()["error"]) == (400, BAD_PARTNER)
    # "" means no partner.
    assert (await _create(client, alice, partner_id=""))["partner_id"] is None


async def test_list_joins_names_and_filters(
    client: httpx.AsyncClient, person: Callable, db: asyncpg.Connection
) -> None:
    alice = await person("alice")
    bob = await person("bob")
    pid = await _partner(client, alice)
    await db.execute(
        "INSERT INTO managed_partnerships (id, user_id, leader_first_name,"
        " leader_last_name, follower_first_name, follower_last_name, created_at,"
        " updated_at) VALUES ('mp1', $1, 'Lee', 'L', 'Fay', 'F', 1, 1)",
        alice.id,
    )
    old = await insert_song(db, alice.id, partner_id=pid, created_at=1)
    managed = await insert_song(
        db, alice.id, managed_partnership_id="mp1", created_at=2
    )
    deleted = await insert_song(db, alice.id, created_at=3)
    await db.execute("UPDATE songs SET deleted_at = 9 WHERE id = $1", deleted)
    await insert_song(db, bob.id, created_at=4)
    legacy = await insert_song(
        db, alice.id, processed_filename="[Legacy] Old Tune", created_at=5
    )

    res = await client.get("/v1/songs", headers=alice.headers)
    data = res.json()["data"]
    assert res.json()["meta"] == {"version": "v1", "count": 3}
    assert [s["id"] for s in data] == [legacy, managed, old]
    assert data[0]["is_legacy"] is True
    assert data[0]["display_name"] == "[Legacy] Old Tune"
    assert data[1]["managed_partnership_id"] == "mp1"
    assert data[1]["managed_leader_first_name"] == "Lee"
    assert data[1]["managed_follower_last_name"] == "F"
    assert data[2]["partner_first_name"] == "Pat"

    filtered = await client.get(
        "/v1/songs", params={"partner_id": pid}, headers=alice.headers
    )
    assert [s["id"] for s in filtered.json()["data"]] == [old]
    unfiltered = await client.get(
        "/v1/songs", params={"partner_id": ""}, headers=alice.headers
    )
    assert unfiltered.json()["meta"]["count"] == 3
    repeated = await client.get(
        "/v1/songs?partner_id=a&partner_id=b", headers=alice.headers
    )
    assert repeated.status_code == 400
    assert repeated.json()["error"]["code"] == "VALIDATION_ERROR"

    # The single read joins the partner only.
    one = await client.get(f"/v1/songs/{managed}", headers=alice.headers)
    assert one.json()["data"]["managed_partnership_id"] == "mp1"
    assert one.json()["data"]["managed_leader_first_name"] is None


async def test_other_users_and_deleted_songs_are_not_found(
    client: httpx.AsyncClient, person: Callable, db: asyncpg.Connection
) -> None:
    alice = await person("alice")
    bob = await person("bob")
    song = (await _create(client, alice))["id"]
    for res in (
        await client.get(f"/v1/songs/{song}", headers=bob.headers),
        await client.patch(
            f"/v1/songs/{song}", json={"display_name": "x"}, headers=bob.headers
        ),
        await client.delete(f"/v1/songs/{song}", headers=bob.headers),
    ):
        assert (res.status_code, res.json()["error"]) == (404, NOT_FOUND)
    await db.execute("UPDATE songs SET deleted_at = 1 WHERE id = $1", song)
    res = await client.get(f"/v1/songs/{song}", headers=alice.headers)
    assert res.status_code == 404


async def test_patch(client: httpx.AsyncClient, person: Callable) -> None:
    alice = await person("alice")
    bob = await person("bob")
    pid = await _partner(client, alice)
    song = await _create(client, alice, display_name="Blue", routine_name="R")
    url = f"/v1/songs/{song['id']}"

    res = await client.patch(
        url,
        json={
            "partner_id": pid,
            "display_name": "  ",
            "division": " Showcase ",
            "routine_name": None,
            "season_year": "2027",
        },
        headers=alice.headers,
    )
    assert res.status_code == 200
    data = res.json()["data"]
    assert data["partner_id"] == pid
    assert data["partner_first_name"] == "Pat"
    # Stored as sent: only display_name is trimmed (blank -> null).
    assert data["division"] == " Showcase "
    assert data["routine_name"] is None
    assert data["season_year"] == "2027"
    assert data["display_name"] is None

    res = await client.patch(url, json={"partner_id": ""}, headers=alice.headers)
    assert res.json()["data"]["partner_id"] is None
    res = await client.patch(url, json={"display_name": None}, headers=alice.headers)
    assert res.json()["error"]["code"] == "VALIDATION_ERROR"

    bobs_partner = await _partner(client, bob)
    res = await client.patch(
        url, json={"partner_id": bobs_partner}, headers=alice.headers
    )
    assert (res.status_code, res.json()["error"]) == (400, BAD_PARTNER)


async def test_delete_blocked_by_active_checkin(
    client: httpx.AsyncClient, person: Callable, db: asyncpg.Connection
) -> None:
    alice = await person("alice")
    song = (await _create(client, alice))["id"]
    await insert_checkin(
        db,
        session_id=await insert_session(db, status="completed"),
        song_id=song,
        submitted_by=alice.id,
    )
    live = await insert_session(db, status="in_progress")
    await insert_checkin(db, session_id=live, song_id=song, submitted_by=alice.id)
    res = await client.delete(f"/v1/songs/{song}", headers=alice.headers)
    assert res.status_code == 409
    assert res.json()["error"] == {
        "code": "SONG_IN_ACTIVE_CHECKIN",
        "message": "This song is referenced by an active check-in. "
        "Complete or withdraw the check-in first.",
    }
    assert await drive_jobs(db) == []


async def test_delete_soft_deletes_and_queues_trash_after_commit(
    client: httpx.AsyncClient, person: Callable, db: asyncpg.Connection
) -> None:
    alice = await person("alice")
    song = await insert_song(
        db, alice.id, drive_file_id="own-file", drive_folder_id="folder"
    )
    event_a = await insert_event(db, name="A")
    event_b = await insert_event(db, name="B")
    await db.execute(
        "INSERT INTO event_song_submissions (id, event_id, song_id,"
        " submitted_by_user_id, drive_copy_file_id, created_at) VALUES"
        " ('s1', $1, $3, $4, 'copy-a', 1), ('s2', $2, $3, $4, NULL, 1)",
        event_a,
        event_b,
        song,
        alice.id,
    )

    res = await client.delete(f"/v1/songs/{song}", headers=alice.headers)
    assert res.status_code == 204
    assert res.content == b""
    assert await db.fetchval("SELECT deleted_at FROM songs WHERE id = $1", song)
    assert await db.fetchval("SELECT count(*) FROM event_song_submissions") == 0
    assert await drive_jobs(db) == [
        ("trash", None, "copy-a", "pending"),
        ("trash", None, "own-file", "pending"),
    ]
    assert (await client.get("/v1/songs", headers=alice.headers)).json()["data"] == []


async def test_delete_without_drive_folder_queues_no_own_file(
    client: httpx.AsyncClient, person: Callable, db: asyncpg.Connection
) -> None:
    alice = await person("alice")
    song = await insert_song(db, alice.id, drive_file_id="own-file")
    res = await client.delete(f"/v1/songs/{song}", headers=alice.headers)
    assert res.status_code == 204
    assert await drive_jobs(db) == []


async def test_auth_required(client: httpx.AsyncClient) -> None:
    for method, path in (
        ("GET", "/v1/songs?partner_id=a&partner_id=b"),
        ("POST", "/v1/songs"),
        ("GET", "/v1/songs/x"),
        ("PATCH", "/v1/songs/x"),
        ("DELETE", "/v1/songs/x"),
    ):
        res = await client.request(method, path, json={})
        assert res.status_code == 401, path
        assert res.json()["error"]["code"] == "UNAUTHORIZED"
