"""A couple's check-ins across both partners' accounts: the linked partner sees
the leader's live check-ins in /v1/checkins/mine but cannot withdraw them,
and a couple holds one live entry per session whichever partner's pair it is."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import asyncpg
import httpx
import pytest

from .content_helpers import insert_song
from .queue_helpers import (
    HOUR,
    insert_entry,
    insert_floor_session,
    insert_pair,
    insert_partner,
    insert_run,
    now_ms,
    positions,
)

LIVE_ENTRY = "This entity already has a live queue entry in this session"


async def _row(
    db: asyncpg.Connection,
    owner_id: str,
    *,
    linked_user_id: str | None = None,
    email: str | None = None,
    first: str = "Pat",
) -> str:
    """A partners row in ``owner_id``'s address book, with a link or email."""
    partner_id = await insert_partner(db, owner_id, first=first, last="Partner")
    await db.execute(
        "UPDATE partners SET linked_user_id = $1, email = $2 WHERE id = $3",
        linked_user_id,
        email,
        partner_id,
    )
    return partner_id


async def _email(db: asyncpg.Connection, user_id: str) -> str:
    email: str = await db.fetchval("SELECT email FROM users WHERE id = $1", user_id)
    return email


async def _session(db: asyncpg.Connection) -> str:
    """A session in its check-in window, with no event and no auto-fill."""
    now = now_ms()
    return await insert_floor_session(
        db,
        status="checkin_open",
        checkin_opens_at=now - HOUR,
        starts_at=now + HOUR,
        ends_at=now + 2 * HOUR,
    )


async def _check_in(
    client: httpx.AsyncClient, who: Any, session_id: str, pair_id: str, song_id: str
) -> httpx.Response:
    return await client.post(
        "/v1/checkins",
        json={
            "sessionId": session_id,
            "divisionName": "Classic",
            "entityPairId": pair_id,
            "songId": song_id,
        },
        headers=who.headers,
    )


async def _mine(client: httpx.AsyncClient, who: Any) -> list[dict[str, Any]]:
    res = await client.get("/v1/checkins/mine", headers=who.headers)
    assert res.status_code == 200, res.text
    rows: list[dict[str, Any]] = res.json()["data"]
    return rows


# --- GET /v1/checkins/mine and DELETE /v1/checkins/{id} --------------------------


async def test_a_linked_partner_sees_the_couples_checkin_but_cannot_withdraw(
    client: httpx.AsyncClient, person: Callable[..., Any], db: asyncpg.Connection
) -> None:
    alice, bob, carol, dave, erin = (
        await person("alice"),
        await person("bob"),
        await person("carol"),
        await person("dave"),
        await person("erin"),
    )
    bob_row = await _row(db, alice.id, linked_user_id=bob.id, first="Bob")
    pair = await insert_pair(db, alice.id, bob_row)
    song = await insert_song(db, alice.id, partner_id=bob_row)
    # Dave is linked only on Erin's row: a pair Alice leads with it is not his.
    dave_row = await _row(db, erin.id, linked_user_id=dave.id)
    stray = await insert_pair(db, alice.id, dave_row)
    session_id = await _session(db)
    ahead = await insert_entry(
        db,
        session_id=session_id,
        queue_type="non_priority",
        position=1,
        song_id=song,
        submitted_by=alice.id,
        pair_id=stray,
        created_at=5,
    )
    entry = await insert_entry(
        db,
        session_id=session_id,
        queue_type="non_priority",
        position=2,
        song_id=song,
        submitted_by=alice.id,
        pair_id=pair,
        created_at=10,
    )
    await insert_run(
        db, session_id=session_id, song_id=song, completed_by=alice.id, pair_id=pair
    )

    alices = await _mine(client, alice)
    assert [(r["id"], r["canWithdraw"]) for r in alices] == [
        (entry["checkin_id"], True),
        (ahead["checkin_id"], True),
    ]
    bobs = await _mine(client, bob)
    assert [r["id"] for r in bobs] == [entry["checkin_id"]]
    assert bobs[0] == {**alices[0], "canWithdraw": False}
    assert bobs[0]["queuePosition"] == 2
    assert bobs[0]["overallPosition"] == 2
    assert bobs[0]["runCount"] == 1
    for who in (carol, dave, erin):
        assert await _mine(client, who) == []

    res = await client.delete(
        f"/v1/checkins/{entry['checkin_id']}", headers=bob.headers
    )
    assert res.status_code == 403
    assert await positions(db, session_id, "non_priority") == [
        (ahead["entry_id"], 1),
        (entry["entry_id"], 2),
    ]


# --- POST /v1/checkins: one entry per couple -------------------------------------


async def _couple(
    db: asyncpg.Connection,
    alice: Any,
    bob: Any,
    *,
    alices_row: dict[str, str | None],
    bobs_row: dict[str, str | None],
) -> dict[str, str]:
    """Each leads a pair with a row for the other, and has a song with it."""
    a_row = await _row(db, alice.id, **alices_row)  # type: ignore[arg-type]
    b_row = await _row(db, bob.id, **bobs_row)  # type: ignore[arg-type]
    return {
        "a_pair": await insert_pair(db, alice.id, a_row),
        "a_song": await insert_song(db, alice.id, partner_id=a_row),
        "b_pair": await insert_pair(db, bob.id, b_row),
        "b_song": await insert_song(db, bob.id, partner_id=b_row),
    }


async def _assert_one_entry_per_couple(
    client: httpx.AsyncClient,
    db: asyncpg.Connection,
    alice: Any,
    bob: Any,
    c: dict[str, str],
) -> None:
    """Whichever partner checks in first, the other's pair is refused."""
    first = await _session(db)
    res = await _check_in(client, alice, first, c["a_pair"], c["a_song"])
    assert res.status_code == 201, res.text
    res = await _check_in(client, bob, first, c["b_pair"], c["b_song"])
    assert res.status_code == 409
    assert res.json()["error"]["message"] == LIVE_ENTRY

    second = await _session(db)
    res = await _check_in(client, bob, second, c["b_pair"], c["b_song"])
    assert res.status_code == 201, res.text
    res = await _check_in(client, alice, second, c["a_pair"], c["a_song"])
    assert res.status_code == 409
    assert res.json()["error"]["message"] == LIVE_ENTRY

    entries = await db.fetch(
        "SELECT session_id FROM queue_entries WHERE session_id = ANY($1::text[])",
        [first, second],
    )
    assert sorted(e["session_id"] for e in entries) == sorted([first, second])


async def test_two_way_links_hold_one_entry(
    client: httpx.AsyncClient, person: Callable[..., Any], db: asyncpg.Connection
) -> None:
    alice, bob = await person("alice"), await person("bob")
    c = await _couple(
        db,
        alice,
        bob,
        alices_row={"linked_user_id": bob.id},
        bobs_row={"linked_user_id": alice.id},
    )
    await _assert_one_entry_per_couple(client, db, alice, bob, c)

    # Bob checking in Alice's song, shared with him, is Alice's pair: refused
    # while his own pair holds the couple's entry.
    third = await _session(db)
    res = await _check_in(client, bob, third, c["b_pair"], c["b_song"])
    assert res.status_code == 201, res.text
    res = await _check_in(client, bob, third, c["b_pair"], c["a_song"])
    assert res.status_code == 409
    assert res.json()["error"]["message"] == LIVE_ENTRY


async def test_a_one_way_link_with_a_matching_email(
    client: httpx.AsyncClient, person: Callable[..., Any], db: asyncpg.Connection
) -> None:
    alice, bob = await person("alice"), await person("bob")
    alices_email = await _email(db, alice.id)
    c = await _couple(
        db,
        alice,
        bob,
        alices_row={"linked_user_id": bob.id},
        bobs_row={"email": f"  {alices_email.upper()} "},
    )
    await _assert_one_entry_per_couple(client, db, alice, bob, c)


async def test_email_alone_matches(
    client: httpx.AsyncClient, person: Callable[..., Any], db: asyncpg.Connection
) -> None:
    alice, bob = await person("alice"), await person("bob")
    c = await _couple(
        db,
        alice,
        bob,
        alices_row={"email": await _email(db, bob.id)},
        bobs_row={"email": await _email(db, alice.id)},
    )
    await _assert_one_entry_per_couple(client, db, alice, bob, c)


async def test_a_pair_standing_for_nobody_elses_account_is_unaffected(
    client: httpx.AsyncClient, person: Callable[..., Any], db: asyncpg.Connection
) -> None:
    alice, bob, carol = (
        await person("alice"),
        await person("bob"),
        await person("carol"),
    )
    a_row = await _row(db, alice.id, linked_user_id=bob.id)
    a_pair = await insert_pair(db, alice.id, a_row)
    a_song = await insert_song(db, alice.id, partner_id=a_row)
    session_id = await _session(db)
    res = await _check_in(client, alice, session_id, a_pair, a_song)
    assert res.status_code == 201, res.text

    # Bob's rows: a placeholder, Carol's email, and one linked to Carol but
    # carrying Alice's email (the link decides).
    for row in (
        {},
        {"email": await _email(db, carol.id)},
        {"linked_user_id": carol.id, "email": await _email(db, alice.id)},
    ):
        b_row = await _row(db, bob.id, **row)
        b_pair = await insert_pair(db, bob.id, b_row)
        b_song = await insert_song(db, bob.id, partner_id=b_row)
        res = await _check_in(client, bob, session_id, b_pair, b_song)
        assert res.status_code == 201, res.text

    # Alice's own second pair, with a placeholder, checks in too.
    other_row = await _row(db, alice.id)
    other_pair = await insert_pair(db, alice.id, other_row)
    other_song = await insert_song(db, alice.id, partner_id=other_row)
    res = await _check_in(client, alice, session_id, other_pair, other_song)
    assert res.status_code == 201, res.text


async def test_the_couple_rule_holds_when_both_partners_check_in_at_once(
    client: httpx.AsyncClient,
    person: Callable[..., Any],
    db: asyncpg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bob's check-in lands after Alice's request has passed its first check
    but before it takes the session lock: Alice's is refused under the lock."""
    from api_deejaytools.routers import checkins as checkin_routes

    alice, bob = await person("alice"), await person("bob")
    c = await _couple(
        db,
        alice,
        bob,
        alices_row={"linked_user_id": bob.id},
        bobs_row={"linked_user_id": alice.id},
    )
    session_id = await _session(db)
    real_lock = checkin_routes.lock_session_for_fill
    raced = False

    async def bob_first(session: Any, sid: str) -> Any:
        nonlocal raced
        if not raced:
            raced = True
            res = await _check_in(client, bob, session_id, c["b_pair"], c["b_song"])
            assert res.status_code == 201, res.text
        return await real_lock(session, sid)

    monkeypatch.setattr(checkin_routes, "lock_session_for_fill", bob_first)
    res = await _check_in(client, alice, session_id, c["a_pair"], c["a_song"])
    assert res.status_code == 409
    assert res.json()["error"]["message"] == LIVE_ENTRY
    entries = await db.fetch(
        "SELECT entity_pair_id FROM queue_entries WHERE session_id = $1", session_id
    )
    assert [e["entity_pair_id"] for e in entries] == [c["b_pair"]]
