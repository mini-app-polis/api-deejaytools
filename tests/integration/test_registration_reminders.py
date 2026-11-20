"""/v1/registration-reminders: a dancer's own registrations with no music in."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import asyncpg
import httpx

from .content_helpers import insert_event, insert_song, new_id

URL = "/v1/registration-reminders"
#: Never completed while these tests matter.
FUTURE = {"start_date": "2099-06-01", "end_date": "2099-06-03"}


async def _named(db: asyncpg.Connection, user_id: str, first: str, last: str) -> None:
    await db.execute(
        "UPDATE users SET first_name = $2, last_name = $3 WHERE id = $1",
        user_id,
        first,
        last,
    )


async def _partner(
    db: asyncpg.Connection,
    user_id: str,
    first: str,
    last: str,
    *,
    linked_user_id: str | None = None,
) -> str:
    partner_id = new_id("ptn")
    await db.execute(
        "INSERT INTO partners (id, user_id, first_name, last_name, linked_user_id,"
        " created_at, updated_at) VALUES ($1, $2, $3, $4, $5, 1, 1)",
        partner_id,
        user_id,
        first,
        last,
        linked_user_id,
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


async def _reminders(client: httpx.AsyncClient, who: Any) -> list[dict[str, Any]]:
    res = await client.get(URL, headers=who.headers)
    assert res.status_code == 200, res.text
    rows: list[dict[str, Any]] = res.json()["data"]
    return rows


async def _ada_and_bob(db: asyncpg.Connection, person: Callable) -> tuple[Any, str]:
    ada = await person("ada")
    await _named(db, ada.id, "Ada", "Lovelace")
    bob = await _partner(db, ada.id, "Bob", "Jones")
    return ada, bob


async def test_needs_sign_in(client: httpx.AsyncClient) -> None:
    assert (await client.get(URL)).status_code == 401
    assert (await client.post(f"{URL}/nope/dismiss")).status_code == 401


async def test_entry_with_a_partner_and_a_song_on_file(
    client: httpx.AsyncClient, person: Callable, db: asyncpg.Connection
) -> None:
    admin = await person("admin", admin=True)
    ada, bob = await _ada_and_bob(db, person)
    song = await insert_song(
        db, ada.id, partner_id=bob, division="Classic", season_year="2099"
    )
    event = await insert_event(db, name="The Open 2099", **FUTURE)
    [row] = await _import(
        client,
        admin,
        event,
        [
            {
                "division": "Classic (NASDE)",
                "dancer_1": "BOB JONES",
                "dancer_2": "Adá Lovelace",
            }
        ],
    )
    [reminder] = await _reminders(client, ada)
    assert reminder["registration_id"] == row["id"]
    assert reminder["event_id"] == event
    assert reminder["event_name"] == "The Open 2099"
    assert reminder["event_status"] == "upcoming"
    assert reminder["division"] == "Classic (NASDE)"
    assert (reminder["dancer_1"], reminder["dancer_2"]) == ("BOB JONES", "Adá Lovelace")
    assert reminder["song"]["song_id"] == song
    assert reminder["song"]["match"] == "strong"
    assert reminder["song"]["same_division"] is True


async def test_entry_with_a_partner_and_nothing_on_file(
    client: httpx.AsyncClient, person: Callable, db: asyncpg.Connection
) -> None:
    admin = await person("admin", admin=True)
    ada, _ = await _ada_and_bob(db, person)
    event = await insert_event(db, **FUTURE)
    await _import(
        client,
        admin,
        event,
        [{"division": "Novice", "dancer_1": "Ada Lovelace", "dancer_2": "Bob Jones"}],
    )
    [reminder] = await _reminders(client, ada)
    assert reminder["song"] is None


async def test_linked_partner_and_managed_partnership_count(
    client: httpx.AsyncClient, person: Callable, db: asyncpg.Connection
) -> None:
    admin = await person("admin", admin=True)
    ada = await person("ada")
    await _named(db, ada.id, "Ada", "Lovelace")
    cleo = await person("cleo")
    await _named(db, cleo.id, "Cleo", "Park")
    # Cleo has Ada in her address book, linked to Ada's account.
    await _partner(db, cleo.id, "Ada", "Lovelace", linked_user_id=ada.id)
    await _managed(db, ada.id, ("Dee", "Ray"), ("Eve", "Moss"))
    event = await insert_event(db, **FUTURE)
    await _import(
        client,
        admin,
        event,
        [
            {
                "division": "Classic",
                "dancer_1": "Cleo Park",
                "dancer_2": "Ada Lovelace",
            },
            {"division": "Showcase", "dancer_1": "Dee Ray", "dancer_2": "Eve Moss"},
        ],
    )
    found = {(r["dancer_1"], r["dancer_2"]) for r in await _reminders(client, ada)}
    assert found == {("Cleo Park", "Ada Lovelace"), ("Dee Ray", "Eve Moss")}


async def test_one_shared_name_and_other_peoples_songs_are_not_shown(
    client: httpx.AsyncClient, person: Callable, db: asyncpg.Connection
) -> None:
    admin = await person("admin", admin=True)
    ada, _ = await _ada_and_bob(db, person)
    zed = await person("zed")
    await _named(db, zed.id, "Zed", "Hart")
    # Someone else's song with Ada's pair names in it is never offered to Ada.
    zeds = await _partner(db, zed.id, "Bob", "Jones")
    await insert_song(db, zed.id, partner_id=zeds, division="Classic")
    event = await insert_event(db, **FUTURE)
    await _import(
        client,
        admin,
        event,
        [
            {
                "division": "Classic",
                "dancer_1": "Ada Lovelace",
                "dancer_2": "Max Steel",
            },
            {
                "division": "Classic",
                "dancer_1": "Ada Lovelace",
                "dancer_2": "Bob Jones",
            },
        ],
    )
    [reminder] = await _reminders(client, ada)
    assert reminder["dancer_2"] == "Bob Jones"
    assert reminder["song"] is None


async def test_left_out_once_music_is_in_or_the_entry_is_settled(
    client: httpx.AsyncClient, person: Callable, db: asyncpg.Connection
) -> None:
    admin = await person("admin", admin=True)
    ada, bob = await _ada_and_bob(db, person)
    song = await insert_song(db, ada.id, partner_id=bob, division="Classic")
    event = await insert_event(db, **FUTURE)
    entry = {"division": "Classic", "dancer_1": "Ada Lovelace", "dancer_2": "Bob Jones"}
    [row] = await _import(client, admin, event, [entry])
    assert len(await _reminders(client, ada)) == 1

    for mark, value in (("handled", True), ("scratched", True)):
        await db.execute(
            f"UPDATE event_registrations SET {mark} = $2 WHERE id = $1",
            row["id"],
            value,
        )
        assert await _reminders(client, ada) == []
        await db.execute(
            f"UPDATE event_registrations SET {mark} = false WHERE id = $1", row["id"]
        )
    await db.execute(
        "UPDATE event_registrations SET linked_label = 'x' WHERE id = $1", row["id"]
    )
    assert await _reminders(client, ada) == []
    await db.execute(
        "UPDATE event_registrations SET linked_label = NULL WHERE id = $1", row["id"]
    )

    await _submit(db, event, song, ada.id)
    assert await _reminders(client, ada) == []


async def test_completed_events_are_left_out(
    client: httpx.AsyncClient, person: Callable, db: asyncpg.Connection
) -> None:
    admin = await person("admin", admin=True)
    ada, _ = await _ada_and_bob(db, person)
    event = await insert_event(db, start_date="2020-01-01", end_date="2020-01-02")
    await _import(
        client,
        admin,
        event,
        [{"division": "Classic", "dancer_1": "Ada Lovelace", "dancer_2": "Bob Jones"}],
    )
    assert await _reminders(client, ada) == []


async def test_song_already_in_the_event_is_not_suggested(
    client: httpx.AsyncClient, person: Callable, db: asyncpg.Connection
) -> None:
    admin = await person("admin", admin=True)
    ada, bob = await _ada_and_bob(db, person)
    other = await insert_event(db, name="DCSX 2099", **FUTURE)
    classic = await insert_song(
        db, ada.id, partner_id=bob, division="Classic", season_year="2099"
    )
    await _submit(db, other, classic, ada.id)
    event = await insert_event(db, name="The Open 2099", **FUTURE)
    await _import(
        client,
        admin,
        event,
        [{"division": "Classic", "dancer_1": "Ada Lovelace", "dancer_2": "Bob Jones"}],
    )
    [reminder] = await _reminders(client, ada)
    assert reminder["song"]["song_id"] == classic
    assert reminder["song"]["submitted_this_season"] == ["DCSX 2099"]


async def test_not_me_dismisses_for_the_caller_only(
    client: httpx.AsyncClient, person: Callable, db: asyncpg.Connection
) -> None:
    admin = await person("admin", admin=True)
    ada, _ = await _ada_and_bob(db, person)
    # Another account with the same names: the dismissal is per person.
    twin = await person("twin")
    await _named(db, twin.id, "Ada", "Lovelace")
    await _partner(db, twin.id, "Bob", "Jones")
    event = await insert_event(db, **FUTURE)
    entry = {"division": "Classic", "dancer_1": "Ada Lovelace", "dancer_2": "Bob Jones"}
    [row] = await _import(client, admin, event, [entry])

    for _ in range(2):  # idempotent
        res = await client.post(f"{URL}/{row['id']}/dismiss", headers=ada.headers)
        assert res.status_code == 204, res.text
    assert await _reminders(client, ada) == []
    assert len(await _reminders(client, twin)) == 1

    # A re-import keeps an unchanged entry, and the dismissal with it.
    await _import(client, admin, event, [entry])
    assert await _reminders(client, ada) == []

    res = await client.post(f"{URL}/nope/dismiss", headers=ada.headers)
    assert res.status_code == 404
