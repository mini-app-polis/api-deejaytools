"""/v1/admin/events/:id/registrations/songs-on-file."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import asyncpg
import httpx

from .content_helpers import insert_event, insert_song, new_id


def _url(event_id: str) -> str:
    return f"/v1/admin/events/{event_id}/registrations/songs-on-file"


async def _named(db: asyncpg.Connection, user_id: str, first: str, last: str) -> None:
    await db.execute(
        "UPDATE users SET first_name = $2, last_name = $3 WHERE id = $1",
        user_id,
        first,
        last,
    )


async def _partner(
    db: asyncpg.Connection, user_id: str, first: str, last: str, role: str = "follower"
) -> str:
    partner_id = new_id("ptn")
    await db.execute(
        "INSERT INTO partners (id, user_id, first_name, last_name, partner_role,"
        " created_at, updated_at) VALUES ($1, $2, $3, $4, $5, 1, 1)",
        partner_id,
        user_id,
        first,
        last,
        role,
    )
    return partner_id


async def _managed(
    db: asyncpg.Connection,
    user_id: str,
    leader: tuple[str, str],
    follower: tuple[str, str],
) -> str:
    mp_id = new_id("mp")
    await db.execute(
        "INSERT INTO managed_partnerships (id, user_id, leader_first_name,"
        " leader_last_name, follower_first_name, follower_last_name, created_at,"
        " updated_at) VALUES ($1, $2, $3, $4, $5, $6, 1, 1)",
        mp_id,
        user_id,
        *leader,
        *follower,
    )
    return mp_id


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


async def _import(
    client: httpx.AsyncClient, admin: Any, event_id: str, entries: list[dict[str, str]]
) -> list[dict[str, Any]]:
    res = await client.put(
        f"/v1/admin/events/{event_id}/registrations",
        json={"entries": entries},
        headers=admin.headers,
    )
    assert res.status_code == 200, res.text
    rows: list[dict[str, Any]] = res.json()["data"]
    return rows


async def _songs_on_file(
    client: httpx.AsyncClient, admin: Any, event_id: str
) -> dict[str, list[dict[str, Any]]]:
    res = await client.get(_url(event_id), headers=admin.headers)
    assert res.status_code == 200, res.text
    return {r["registration_id"]: r["candidates"] for r in res.json()["data"]}


async def test_admin_only(
    client: httpx.AsyncClient, person: Callable, db: asyncpg.Connection
) -> None:
    alice = await person("alice")
    event = await insert_event(db)
    assert (await client.get(_url(event))).status_code == 401
    res = await client.get(_url(event), headers=alice.headers)
    assert res.status_code == 403
    assert res.json()["error"]["code"] == "FORBIDDEN"
    admin = await person("admin", admin=True)
    res = await client.get(_url("nope"), headers=admin.headers)
    assert res.status_code == 404


async def test_exact_match_in_either_order_is_strong(
    client: httpx.AsyncClient, person: Callable, db: asyncpg.Connection
) -> None:
    admin = await person("admin", admin=True)
    dancer = await person("ada")
    await _named(db, dancer.id, "Ada", "Lovelace")
    partner = await _partner(db, dancer.id, "Bob", "Jones")
    song = await insert_song(
        db, dancer.id, partner_id=partner, division="Classic", season_year="2026"
    )
    event = await insert_event(db, start_date="2026-05-01")
    rows = await _import(
        client,
        admin,
        event,
        [
            {
                "division": "Classic (NASDE)",
                "dancer_1": "Ada Lovelace",
                "dancer_2": "Bob Jones",
            },
            {
                "division": "Classic",
                "dancer_1": "BOB  JONES",
                "dancer_2": "Adá Lovelace",
            },
        ],
    )
    found = await _songs_on_file(client, admin, event)
    for row in rows:
        [candidate] = found[row["id"]]
        assert candidate["song_id"] == song
        assert candidate["match"] == "strong"
        assert candidate["same_division"] is True
        assert candidate["partnership_label"] == "Ada Lovelace & Bob Jones"
        assert candidate["submitted_this_season"] == []


async def test_close_name_in_a_matching_division_is_possible_after_strong(
    client: httpx.AsyncClient, person: Callable, db: asyncpg.Connection
) -> None:
    admin = await person("admin", admin=True)
    owner = await person("hugo")
    await _named(db, owner.id, "Hugo", "Pro")

    async def song(follower: tuple[str, str], division: str) -> str:
        mp = await _managed(db, owner.id, ("Hugo", "Pro"), follower)
        return await insert_song(
            db, owner.id, managed_partnership_id=mp, division=division
        )

    exact = await song(("Valerie", "Weeks"), "Showcase")
    misspelt = await song(("Valerie", "Weekes"), "ProAm LeaderAm")
    shortened = await song(("Val", "Weeks"), "ProAm Classic")
    # Only one name shared: another partner, never a candidate.
    await song(("Yubin", "Kim"), "ProAm LeaderAm")
    # Close name, but the divisions share no word.
    await song(("Valery", "Weeks"), "Masters")
    event = await insert_event(db)
    [row] = await _import(
        client,
        admin,
        event,
        [
            {
                "division": "ProAm Spotlight (Intermediate/Advanced)",
                "dancer_1": "Valerie Weeks",
                "dancer_2": "Hugo Pro",
            }
        ],
    )
    found = (await _songs_on_file(client, admin, event))[row["id"]]
    assert found[0]["song_id"] == exact
    assert found[0]["match"] == "strong"
    assert {(c["song_id"], c["match"]) for c in found[1:]} == {
        (misspelt, "possible"),
        (shortened, "possible"),
    }


async def test_one_shared_name_is_not_a_candidate(
    client: httpx.AsyncClient, person: Callable, db: asyncpg.Connection
) -> None:
    admin = await person("admin", admin=True)
    owner = await person("hugo")
    await _named(db, owner.id, "Hugo", "Pro")
    mp = await _managed(db, owner.id, ("Hugo", "Pro"), ("Yubin", "Kim"))
    await insert_song(db, owner.id, managed_partnership_id=mp, division="Classic")
    event = await insert_event(db)
    await _import(
        client,
        admin,
        event,
        [{"division": "Classic", "dancer_1": "Valerie Weeks", "dancer_2": "Hugo Pro"}],
    )
    assert await _songs_on_file(client, admin, event) == {}


async def test_deleted_and_already_submitted_are_excluded(
    client: httpx.AsyncClient, person: Callable, db: asyncpg.Connection
) -> None:
    admin = await person("admin", admin=True)
    dancer = await person("ada")
    await _named(db, dancer.id, "Ada", "Lovelace")
    partner = await _partner(db, dancer.id, "Bob", "Jones")
    gone = await insert_song(db, dancer.id, partner_id=partner)
    await db.execute("UPDATE songs SET deleted_at = 5 WHERE id = $1", gone)
    legacy = await insert_song(
        db, dancer.id, partner_id=partner, processed_filename="[Legacy] x.mp3"
    )
    assert legacy
    event = await insert_event(db)
    [row] = await _import(
        client,
        admin,
        event,
        [{"division": "Classic", "dancer_1": "Ada Lovelace", "dancer_2": "Bob Jones"}],
    )
    assert await _songs_on_file(client, admin, event) == {}

    # A song on file, then submitted: the entry has music in, so it drops out.
    song = await insert_song(db, dancer.id, partner_id=partner)
    found = await _songs_on_file(client, admin, event)
    assert [c["song_id"] for c in found[row["id"]]] == [song]
    await _submit(db, event, song, dancer.id)
    assert await _songs_on_file(client, admin, event) == {}


async def test_ranks_division_then_season_then_submitted_elsewhere(
    client: httpx.AsyncClient, person: Callable, db: asyncpg.Connection
) -> None:
    admin = await person("admin", admin=True)
    dancer = await person("ada")
    await _named(db, dancer.id, "Ada", "Lovelace")
    partner = await _partner(db, dancer.id, "Bob", "Jones")
    old = await insert_song(
        db, dancer.id, partner_id=partner, division="Classic", season_year="2025"
    )
    other_division = await insert_song(
        db, dancer.id, partner_id=partner, division="Showcase", season_year="2026"
    )
    current = await insert_song(
        db, dancer.id, partner_id=partner, division="Classic", season_year="2026"
    )
    sent = await insert_song(
        db, dancer.id, partner_id=partner, division="Classic", season_year="2026"
    )
    # Created first, so newest-first alone would put it last.
    await db.execute("UPDATE songs SET updated_at = 0 WHERE id = $1", sent)
    dcsx = await insert_event(db, name="DCSX", start_date="2026-02-01")
    last_year = await insert_event(db, name="Old Open", start_date="2025-02-01")
    await _submit(db, dcsx, sent, dancer.id)
    await _submit(db, last_year, current, dancer.id)
    event = await insert_event(db, name="The Open", start_date="2026-05-01")
    [row] = await _import(
        client,
        admin,
        event,
        [{"division": "Classic", "dancer_1": "Ada Lovelace", "dancer_2": "Bob Jones"}],
    )
    found = (await _songs_on_file(client, admin, event))[row["id"]]
    assert [c["song_id"] for c in found] == [sent, current, old, other_division]
    assert found[0]["submitted_this_season"] == ["DCSX"]
    assert found[1]["submitted_this_season"] == []


async def test_scratched_handled_and_linked_entries_are_left_out(
    client: httpx.AsyncClient, person: Callable, db: asyncpg.Connection
) -> None:
    admin = await person("admin", admin=True)
    dancer = await person("ada")
    await _named(db, dancer.id, "Ada", "Lovelace")
    await insert_song(
        db,
        dancer.id,
        partner_id=await _partner(db, dancer.id, "Bob", "Jones"),
    )
    event = await insert_event(db)
    entry = {"division": "Classic", "dancer_1": "Ada Lovelace", "dancer_2": "Bob Jones"}
    [row] = await _import(client, admin, event, [entry])
    assert row["id"] in await _songs_on_file(client, admin, event)
    for marks in (
        {"handled": True},
        {"scratched": True},
        {"linked_label": "Ada & Bob"},
    ):
        res = await client.patch(
            f"/v1/admin/event-registrations/{row['id']}",
            json={"handled": False, "scratched": False, "linked_label": None, **marks},
            headers=admin.headers,
        )
        assert res.status_code == 200, res.text
        assert await _songs_on_file(client, admin, event) == {}
