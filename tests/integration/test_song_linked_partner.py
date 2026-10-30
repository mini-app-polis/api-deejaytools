"""A song shared with its partner's account: the linked partner may read it,
submit it to events and check it in; only the owner may change or delete it."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import asyncpg
import httpx

from .content_helpers import insert_event, insert_song
from .queue_helpers import (
    HOUR,
    insert_floor_session,
    insert_pair,
    insert_partner,
    now_ms,
)


async def _linked_partner(
    db: asyncpg.Connection, owner_id: str, linked_user_id: str | None
) -> str:
    """A partners row in ``owner_id``'s address book, linked to an account."""
    partner_id = await insert_partner(db, owner_id, first="Bea", last="Linked")
    await db.execute(
        "UPDATE partners SET linked_user_id = $1 WHERE id = $2",
        linked_user_id,
        partner_id,
    )
    return partner_id


async def _cast(db: asyncpg.Connection, person: Callable[..., Any]) -> dict[str, Any]:
    """Alice owns a song whose partner is Bob's account. Carol is in Alice's
    book unlinked, and Dave is linked only on Erin's partner row."""
    alice, bob, carol, dave, erin = (
        await person("alice"),
        await person("bob"),
        await person("carol"),
        await person("dave"),
        await person("erin"),
    )
    bob_row = await _linked_partner(db, alice.id, bob.id)
    song = await insert_song(
        db, alice.id, partner_id=bob_row, division="Classic", routine_name="Blue"
    )
    carol_row = await _linked_partner(db, alice.id, None)
    carol_song = await insert_song(db, alice.id, partner_id=carol_row)
    # A song pointing at another owner's partner row shares nothing.
    dave_row = await _linked_partner(db, erin.id, dave.id)
    stray = await insert_song(db, alice.id, partner_id=dave_row)
    return {
        "alice": alice,
        "bob": bob,
        "carol": carol,
        "dave": dave,
        "bob_row": bob_row,
        "song": song,
        "carol_song": carol_song,
        "stray": stray,
    }


async def test_a_linked_partner_reads_the_shared_song(
    client: httpx.AsyncClient, person: Callable[..., Any], db: asyncpg.Connection
) -> None:
    c = await _cast(db, person)
    alice, bob = c["alice"], c["bob"]

    mine = (await client.get("/v1/songs", headers=bob.headers)).json()["data"]
    assert [(s["id"], s["shared_with_me"]) for s in mine] == [(c["song"], True)]
    assert mine[0]["user_id"] == alice.id
    assert mine[0]["partner_first_name"] == "Bea"

    one = await client.get(f"/v1/songs/{c['song']}", headers=bob.headers)
    assert one.status_code == 200
    assert one.json()["data"]["shared_with_me"] is True

    owned = (await client.get("/v1/songs", headers=alice.headers)).json()["data"]
    assert {s["id"] for s in owned} == {c["song"], c["carol_song"], c["stray"]}
    assert not any(s["shared_with_me"] for s in owned)
    own_one = await client.get(f"/v1/songs/{c['song']}", headers=alice.headers)
    assert own_one.json()["data"]["shared_with_me"] is False

    # partner_id names one of the caller's own partners: Bob has none here.
    filtered = await client.get(
        "/v1/songs", params={"partner_id": c["bob_row"]}, headers=bob.headers
    )
    assert filtered.json()["data"] == []


async def test_nobody_else_sees_the_song(
    client: httpx.AsyncClient, person: Callable[..., Any], db: asyncpg.Connection
) -> None:
    c = await _cast(db, person)
    for who in (c["carol"], c["dave"]):
        listed = (await client.get("/v1/songs", headers=who.headers)).json()["data"]
        assert listed == []
        for song in (c["song"], c["carol_song"], c["stray"]):
            res = await client.get(f"/v1/songs/{song}", headers=who.headers)
            assert res.status_code == 404
    # Bob is linked on Alice's row for one song only.
    for song in (c["carol_song"], c["stray"]):
        res = await client.get(f"/v1/songs/{song}", headers=c["bob"].headers)
        assert res.status_code == 404


async def test_only_the_owner_changes_or_deletes_it(
    client: httpx.AsyncClient, person: Callable[..., Any], db: asyncpg.Connection
) -> None:
    c = await _cast(db, person)
    bob, song = c["bob"], c["song"]
    before = await db.fetchrow("SELECT * FROM songs WHERE id = $1", song)

    patched = await client.patch(
        f"/v1/songs/{song}", json={"display_name": "Mine now"}, headers=bob.headers
    )
    assert patched.status_code == 404
    deleted = await client.delete(f"/v1/songs/{song}", headers=bob.headers)
    assert deleted.status_code == 404

    assert await db.fetchrow("SELECT * FROM songs WHERE id = $1", song) == before


async def test_a_deleted_song_is_not_shared(
    client: httpx.AsyncClient, person: Callable[..., Any], db: asyncpg.Connection
) -> None:
    c = await _cast(db, person)
    await db.execute("UPDATE songs SET deleted_at = 9 WHERE id = $1", c["song"])
    bob = c["bob"]
    assert (await client.get("/v1/songs", headers=bob.headers)).json()["data"] == []
    res = await client.get(f"/v1/songs/{c['song']}", headers=bob.headers)
    assert res.status_code == 404
    event_id = await insert_event(db)
    res = await client.post(
        "/v1/event-song-submissions",
        json={"event_id": event_id, "song_id": c["song"]},
        headers=bob.headers,
    )
    assert res.status_code == 404


async def _open_session(db: asyncpg.Connection, event_id: str) -> str:
    now = now_ms()
    return await insert_floor_session(
        db,
        event_id=event_id,
        status="checkin_open",
        checkin_opens_at=now - HOUR,
        starts_at=now + HOUR,
        ends_at=now + 2 * HOUR,
    )


def _checkin(session_id: str, song_id: str, pair_id: str) -> dict[str, Any]:
    return {
        "sessionId": session_id,
        "divisionName": "Classic",
        "entityPairId": pair_id,
        "songId": song_id,
    }


async def test_a_linked_partner_submits_and_checks_in_as_the_pair(
    client: httpx.AsyncClient, person: Callable[..., Any], db: asyncpg.Connection
) -> None:
    c = await _cast(db, person)
    alice, bob, song = c["alice"], c["bob"], c["song"]
    event_id = await insert_event(db)

    for who in (c["carol"], c["dave"]):
        res = await client.post(
            "/v1/event-song-submissions",
            json={"event_id": event_id, "song_id": song},
            headers=who.headers,
        )
        assert res.status_code == 404

    submitted = await client.post(
        "/v1/event-song-submissions",
        json={"event_id": event_id, "song_id": song},
        headers=bob.headers,
    )
    assert submitted.status_code == 201, submitted.text
    assert submitted.json()["data"]["song_id"] == song
    submitter = await db.fetchval(
        "SELECT submitted_by_user_id FROM event_song_submissions WHERE song_id = $1",
        song,
    )
    assert submitter == bob.id
    # Bob made it, so Bob sees it; the owner's list is the owner's own.
    bobs = (await client.get("/v1/event-song-submissions", headers=bob.headers)).json()[
        "data"
    ]
    assert [s["song_id"] for s in bobs] == [song]

    session_id = await _open_session(db, event_id)
    # Bob leads a pair of his own with Alice; the check-in still uses the
    # song's entity, Alice's pair with Bob's row.
    bobs_alice = await insert_partner(db, bob.id, first="Alice", last="Owner")
    bobs_pair = await insert_pair(db, bob.id, bobs_alice)
    for who in (c["carol"], c["dave"]):
        res = await client.post(
            "/v1/checkins",
            json=_checkin(session_id, song, bobs_pair),
            headers=who.headers,
        )
        assert res.status_code == 404

    checked_in = await client.post(
        "/v1/checkins", json=_checkin(session_id, song, bobs_pair), headers=bob.headers
    )
    assert checked_in.status_code == 201, checked_in.text
    entity, submitted_by = await db.fetchrow(
        "SELECT p.user_a_id || '/' || p.partner_b_id, c.submitted_by_user_id"
        " FROM checkins c JOIN pairs p ON p.id = c.entity_pair_id"
        " WHERE c.id = $1",
        checked_in.json()["data"]["id"],
    )
    assert entity == f"{alice.id}/{c['bob_row']}"
    assert submitted_by == bob.id

    # The owner checking in the same song is the same entity: one live entry.
    alices_pair = await db.fetchval(
        "SELECT id FROM pairs WHERE user_a_id = $1 AND partner_b_id = $2",
        alice.id,
        c["bob_row"],
    )
    again = await client.post(
        "/v1/checkins",
        json=_checkin(session_id, song, alices_pair),
        headers=alice.headers,
    )
    assert again.status_code == 409
    assert again.json()["error"]["message"] == (
        "This entity already has a live queue entry in this session"
    )


async def test_a_linked_partner_names_their_pair_or_the_songs(
    client: httpx.AsyncClient, person: Callable[..., Any], db: asyncpg.Connection
) -> None:
    c = await _cast(db, person)
    bob, song = c["bob"], c["song"]
    event_id = await insert_event(db)
    await db.execute(
        "INSERT INTO event_song_submissions (id, event_id, song_id,"
        " submitted_by_user_id, created_at) VALUES ('sub_lp', $1, $2, $3, 1)",
        event_id,
        song,
        c["alice"].id,
    )
    session_id = await _open_session(db, event_id)
    # A pair Bob neither leads nor is the song's.
    strangers = await insert_pair(
        db, c["carol"].id, await insert_partner(db, c["carol"].id)
    )
    res = await client.post(
        "/v1/checkins", json=_checkin(session_id, song, strangers), headers=bob.headers
    )
    assert res.status_code == 400
    assert res.json()["error"]["message"] == "You are not a member of this pair"

    # The song's own pair is accepted, and created when missing.
    songs_pair = await insert_pair(db, c["alice"].id, c["bob_row"])
    res = await client.post(
        "/v1/checkins", json=_checkin(session_id, song, songs_pair), headers=bob.headers
    )
    assert res.status_code == 201, res.text
