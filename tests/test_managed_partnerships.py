"""/v1/managed-partnerships: couples a caller manages by name."""

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

BODY = {
    "leader_first_name": " lee  ann ",
    "leader_last_name": "leader",
    "follower_first_name": "fay",
    "follower_last_name": "mcFollower",
}
NOT_FOUND = {"code": "NOT_FOUND", "message": "Managed partnership not found"}


async def _create(client: httpx.AsyncClient, who: Any, **overrides: Any) -> dict:
    res = await client.post(
        "/v1/managed-partnerships", json={**BODY, **overrides}, headers=who.headers
    )
    assert res.status_code == 201, res.text
    return res.json()["data"]


async def test_create_shape_and_casing(
    client: httpx.AsyncClient, person: Callable
) -> None:
    alice = await person("alice")
    created = await _create(client, alice)
    assert created == {
        "id": created["id"],
        "user_id": alice.id,
        "leader_first_name": "Lee Ann",
        "leader_last_name": "Leader",
        "follower_first_name": "Fay",
        "follower_last_name": "McFollower",
        "created_at": created["created_at"],
        "updated_at": created["created_at"],
    }


async def test_validation(client: httpx.AsyncClient, person: Callable) -> None:
    alice = await person("alice")
    for override in (
        {"leader_first_name": "  "},
        {"follower_last_name": "x" * 101},
        {"leader_last_name": None},
    ):
        res = await client.post(
            "/v1/managed-partnerships",
            json={**BODY, **override},
            headers=alice.headers,
        )
        assert res.status_code == 400, override
        assert res.json()["error"]["code"] == "VALIDATION_ERROR"
    res = await client.post(
        "/v1/managed-partnerships",
        json={k: v for k, v in BODY.items() if k != "follower_first_name"},
        headers=alice.headers,
    )
    assert res.json()["error"]["code"] == "VALIDATION_ERROR"


async def test_list_patch_and_ownership(
    client: httpx.AsyncClient, person: Callable
) -> None:
    alice = await person("alice")
    bob = await person("bob")
    mp = await _create(client, alice)
    await _create(client, bob)

    res = await client.patch(
        f"/v1/managed-partnerships/{mp['id']}",
        json={**BODY, "follower_last_name": "follows"},
        headers=alice.headers,
    )
    assert res.status_code == 200
    assert res.json()["data"]["follower_last_name"] == "Follows"
    assert res.json()["data"]["created_at"] == mp["created_at"]

    listed = await client.get("/v1/managed-partnerships", headers=alice.headers)
    assert listed.json()["meta"] == {"version": "v1", "count": 1}
    assert listed.json()["data"][0]["follower_last_name"] == "Follows"

    res = await client.patch(
        f"/v1/managed-partnerships/{mp['id']}", json=BODY, headers=bob.headers
    )
    assert (res.status_code, res.json()["error"]) == (404, NOT_FOUND)
    res = await client.delete(
        f"/v1/managed-partnerships/{mp['id']}", headers=bob.headers
    )
    assert (res.status_code, res.json()["error"]) == (404, NOT_FOUND)


async def test_delete_blocked_by_active_checkin(
    client: httpx.AsyncClient, person: Callable, db: asyncpg.Connection
) -> None:
    alice = await person("alice")
    mp = await _create(client, alice)
    song = await insert_song(db, alice.id, managed_partnership_id=mp["id"])
    # A queue entry in a finished session does not block.
    await insert_checkin(
        db,
        session_id=await insert_session(db, status="completed"),
        song_id=song,
        submitted_by=alice.id,
        managed_partnership_id=mp["id"],
    )
    live = await insert_session(db)
    await insert_checkin(
        db,
        session_id=live,
        song_id=song,
        submitted_by=alice.id,
        managed_partnership_id=mp["id"],
    )
    res = await client.delete(
        f"/v1/managed-partnerships/{mp['id']}", headers=alice.headers
    )
    assert res.status_code == 409
    assert res.json()["error"] == {
        "code": "MANAGED_PARTNERSHIP_IN_ACTIVE_CHECKIN",
        "message": "This partnership has an active check-in. "
        "Complete or withdraw it first.",
    }

    await db.execute("UPDATE sessions SET status = 'cancelled' WHERE id = $1", live)
    res = await client.delete(
        f"/v1/managed-partnerships/{mp['id']}", headers=alice.headers
    )
    assert res.status_code == 204


async def test_delete_soft_deletes_songs_and_queues_trash(
    client: httpx.AsyncClient, person: Callable, db: asyncpg.Connection
) -> None:
    alice = await person("alice")
    mp = await _create(client, alice)
    event = await insert_event(db)
    with_file = await insert_song(
        db,
        alice.id,
        managed_partnership_id=mp["id"],
        drive_file_id="file-own",
        drive_folder_id="folder",
    )
    # A file id without a folder id is not trashed (the song-delete condition).
    no_folder = await insert_song(
        db, alice.id, managed_partnership_id=mp["id"], drive_file_id="file-nofolder"
    )
    already_gone = await insert_song(
        db,
        alice.id,
        managed_partnership_id=mp["id"],
        drive_file_id="file-old",
        drive_folder_id="folder",
    )
    await db.execute("UPDATE songs SET deleted_at = 7 WHERE id = $1", already_gone)
    unrelated = await insert_song(db, alice.id)
    for i, song in enumerate((with_file, no_folder)):
        await db.execute(
            "INSERT INTO event_song_submissions (id, event_id, song_id,"
            " submitted_by_user_id, drive_copy_file_id, created_at)"
            " VALUES ($1, $2, $3, $4, $5, 1)",
            f"sub{i}",
            event,
            song,
            alice.id,
            "copy-1" if i == 0 else None,
        )

    res = await client.delete(
        f"/v1/managed-partnerships/{mp['id']}", headers=alice.headers
    )
    assert res.status_code == 204
    assert res.content == b""

    assert await db.fetchval("SELECT count(*) FROM event_song_submissions") == 0
    deleted = {
        r["id"]: r["deleted_at"]
        for r in await db.fetch("SELECT id, deleted_at FROM songs")
    }
    assert deleted[with_file] and deleted[no_folder]
    assert deleted[already_gone] == 7
    assert deleted[unrelated] is None
    assert await db.fetchval(
        "SELECT deleted_at FROM managed_partnerships WHERE id = $1", mp["id"]
    )
    assert await drive_jobs(db) == [
        ("trash", None, "copy-1", "pending"),
        ("trash", None, "file-own", "pending"),
    ]
    listed = await client.get("/v1/managed-partnerships", headers=alice.headers)
    assert listed.json()["data"] == []
    again = await client.delete(
        f"/v1/managed-partnerships/{mp['id']}", headers=alice.headers
    )
    assert again.status_code == 404


async def test_auth_required(client: httpx.AsyncClient) -> None:
    for method, path in (
        ("GET", "/v1/managed-partnerships"),
        ("POST", "/v1/managed-partnerships"),
        ("PATCH", "/v1/managed-partnerships/x"),
        ("DELETE", "/v1/managed-partnerships/x"),
    ):
        res = await client.request(method, path, json={})
        assert res.status_code == 401, path
        assert res.json()["error"]["code"] == "UNAUTHORIZED"
