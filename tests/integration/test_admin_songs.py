"""/v1/admin/songs: every user's songs, for admins."""

from __future__ import annotations

from collections.abc import Callable

import asyncpg
import httpx

from .content_helpers import insert_song

URL = "/v1/admin/songs"


async def _partner(
    db: asyncpg.Connection,
    user_id: str,
    pid: str,
    first: str,
    last: str,
    *,
    kind: str = "partner",
    linked_user_id: str | None = None,
) -> str:
    await db.execute(
        "INSERT INTO partners (id, user_id, first_name, last_name, kind,"
        " linked_user_id, created_at, updated_at) VALUES ($1, $2, $3, $4, $5, $6, 1, 1)",
        pid,
        user_id,
        first,
        last,
        kind,
        linked_user_id,
    )
    return pid


async def test_requires_a_token_and_the_scope(
    client: httpx.AsyncClient, person: Callable
) -> None:
    alice = await person("alice")
    res = await client.get(URL)
    assert res.status_code == 401
    assert res.json()["error"] == {
        "code": "UNAUTHORIZED",
        "message": "Authentication required",
    }
    res = await client.get(URL, headers=alice.headers)
    assert res.status_code == 403
    assert res.json()["error"] == {
        "code": "FORBIDDEN",
        "message": "Admin access required",
    }
    # Auth before query validation.
    res = await client.get(f"{URL}?include_deleted=yes", headers=alice.headers)
    assert res.status_code == 403


async def test_shape_order_and_partners(
    client: httpx.AsyncClient, person: Callable, db: asyncpg.Connection
) -> None:
    admin = await person("admin", admin=True)
    alice = await person("alice")
    bob = await person("bob")
    pid = await _partner(db, alice.id, "p_bob", "Bob", "Jones", linked_user_id=bob.id)
    solo = await _partner(db, alice.id, "p_solo", "Solo", "Act", kind="solo")

    with_partner = await insert_song(
        db,
        alice.id,
        partner_id=pid,
        division="Classic",
        season_year="2026",
        routine_name="Routine",
        processed_filename="Alice_BobJones_Classic_2026_Routine_v01.mp3",
        created_at=30,
    )
    legacy = await insert_song(
        db, alice.id, processed_filename="[Legacy] old.mp3", created_at=20
    )
    placeholder = await insert_song(
        db, alice.id, partner_id=solo, division="Showcase", created_at=10
    )
    await db.execute(
        "UPDATE songs SET display_name = 'Routine', personal_descriptor = 'mine'"
        " WHERE id = $1",
        with_partner,
    )

    res = await client.get(URL, headers=admin.headers)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["meta"] == {"version": "v1", "count": 3}
    assert [s["id"] for s in body["data"]] == [with_partner, legacy, placeholder]
    first, second, third = body["data"]
    assert first == {
        "id": with_partner,
        "song_label": "Alice & Bob Jones Classic 2026 Routine v01",
        "display_name": "Routine",
        "division": "Classic",
        "routine_name": "Routine",
        "personal_descriptor": "mine",
        "season_year": "2026",
        "is_legacy": False,
        "created_at": 30,
        "deleted_at": None,
        "owner": {
            "id": alice.id,
            "email": f"alice.{alice.id}@example.test",
            "full_name": "Alice",
        },
        "partner": {
            "id": pid,
            "full_name": "Bob Jones",
            "linked_user_email": f"bob.{bob.id}@example.test",
        },
    }
    assert second["is_legacy"] is True
    assert second["partner"] is None
    assert second["song_label"] == "[Legacy] old.mp3"
    # A placeholder partner is the whole entity.
    assert third["song_label"] == "Solo Act Showcase"
    assert third["partner"] == {
        "id": solo,
        "full_name": "Solo Act",
        "linked_user_email": None,
    }


async def test_nameless_owner_and_unstructured_song(
    client: httpx.AsyncClient, person: Callable, db: asyncpg.Connection
) -> None:
    admin = await person("admin", admin=True)
    await db.execute(
        "INSERT INTO users (id, email, created_at, updated_at)"
        " VALUES ('user_nameless', 'nameless@example.test', 1, 1)"
    )
    song = await insert_song(db, "user_nameless")
    res = await client.get(URL, headers=admin.headers)
    row = res.json()["data"][0]
    assert row["id"] == song
    assert row["owner"] == {
        "id": "user_nameless",
        "email": "nameless@example.test",
        "full_name": None,
    }
    assert row["song_label"] == song


async def test_deleted_songs_and_search(
    client: httpx.AsyncClient, person: Callable, db: asyncpg.Connection
) -> None:
    admin = await person("admin", admin=True)
    alice = await person("alice")
    pid = await _partner(db, alice.id, "p_zed", "Zed", "Zebra")
    live = await insert_song(db, alice.id, partner_id=pid, routine_name="Waltz")
    gone = await insert_song(db, alice.id, routine_name="Tango")
    await db.execute("UPDATE songs SET deleted_at = 99 WHERE id = $1", gone)

    async def ids(params: dict[str, str]) -> list[str]:
        res = await client.get(URL, params=params, headers=admin.headers)
        assert res.status_code == 200, res.text
        return sorted(s["id"] for s in res.json()["data"])

    assert await ids({}) == [live]
    assert await ids({"include_deleted": "false"}) == [live]
    assert await ids({"include_deleted": "true"}) == sorted([live, gone])
    assert await ids({"q": " zebra "}) == [live]
    assert await ids({"q": "tango"}) == []
    assert await ids({"q": "tango", "include_deleted": "true"}) == [gone]
    assert await ids({"q": "ALICE"}) == [live]

    res = await client.get(
        URL, params={"include_deleted": "true"}, headers=admin.headers
    )
    deleted_row = next(s for s in res.json()["data"] if s["id"] == gone)
    assert deleted_row["deleted_at"] == 99


async def test_validates_the_query(client: httpx.AsyncClient, person: Callable) -> None:
    admin = await person("admin", admin=True)
    for qs in ("include_deleted=yes", "include_deleted=TRUE", "q=a&q=b"):
        res = await client.get(f"{URL}?{qs}", headers=admin.headers)
        assert res.status_code == 400, qs
        assert res.json()["error"]["code"] == "VALIDATION_ERROR"
