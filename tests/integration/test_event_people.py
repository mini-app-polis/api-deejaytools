"""/v1/events/{id}/people: an event's staff find its people by name (ADR-0012)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import asyncpg
import httpx

from .content_helpers import insert_checkin, insert_song
from .queue_helpers import (
    insert_entry,
    insert_floor_session,
    insert_managed,
    insert_pair,
    insert_user,
)

EVENT = {"name": "Swing Fling", "start_date": "2026-05-01", "end_date": "2026-05-03"}
SCOPE = "deejaytools.entries.read"


def _url(event_id: str) -> str:
    return f"/v1/events/{event_id}/people"


async def _event(client: httpx.AsyncClient, admin: Any, name: str) -> str:
    res = await client.post(
        "/v1/events", json={**EVENT, "name": name}, headers=admin.headers
    )
    assert res.status_code == 201, res.text
    event_id: str = res.json()["data"]["id"]
    return event_id


async def _grant(
    client: httpx.AsyncClient, admin: Any, event_id: str, user_id: str, role: str
) -> None:
    res = await client.put(
        f"/v1/events/{event_id}/staff/{user_id}/{role}", headers=admin.headers
    )
    assert res.status_code == 200, res.text


async def _dancer(db: asyncpg.Connection, first: str, last: str) -> str:
    return await insert_user(
        db, first=first, last=last, email=f"{first}.{last}@example.test".lower()
    )


async def _submit(
    db: asyncpg.Connection,
    event_id: str,
    user_id: str,
    *,
    partner_id: str | None = None,
) -> None:
    song = await insert_song(db, user_id, partner_id=partner_id)
    await db.execute(
        "INSERT INTO event_song_submissions (id, event_id, song_id,"
        " submitted_by_user_id, created_at) VALUES ($1, $2, $3, $4, 1)",
        f"ess_{song}",
        event_id,
        song,
        user_id,
    )


async def _linked_partner(db: asyncpg.Connection, owner: str, linked: str) -> str:
    """A partner of ``owner`` that is ``linked``'s account."""
    partner_id = f"pt_{linked}"
    await db.execute(
        "INSERT INTO partners (id, user_id, first_name, last_name, linked_user_id,"
        " created_at, updated_at) VALUES ($1, $2, 'P', 'Artner', $3, 1, 1)",
        partner_id,
        owner,
        linked,
    )
    return partner_id


async def _world(
    client: httpx.AsyncClient, db: asyncpg.Connection, person: Callable
) -> dict[str, Any]:
    """Events A and B, A's three staff callers, and the Zeldas: people
    connected to A in each way, one connected to B only, one to nothing."""
    admin = await person("admin", admin=True)
    event_a = await _event(client, admin, "A")
    event_b = await _event(client, admin, "B")
    w: dict[str, Any] = {"admin": admin, "a": event_a, "b": event_b}
    for name, role in (
        ("carol", "coordinator"),
        ("dexter", "dj"),
        ("mona", "event-manager"),
    ):
        w[role] = await person(name)
        await _grant(client, admin, event_a, w[role].id, role)
    w["b_staff"] = await person("bea")
    await _grant(client, admin, event_b, w["b_staff"].id, "coordinator")
    w["staff_member"] = await person("zeldo")
    await _grant(client, admin, event_a, w["staff_member"].id, "dj")

    # On a song submitted to A: its submitter and owner, and its partner.
    w["submitter"] = await _dancer(db, "Zelda", "Submitter")
    w["song_partner"] = await _dancer(db, "Zelda", "Songpartner")
    await _submit(
        db,
        event_a,
        w["submitter"],
        partner_id=await _linked_partner(db, w["submitter"], w["song_partner"]),
    )
    # Checked in at A's session, solo, as a pair, or managing a partnership.
    session_a = await insert_floor_session(db, event_id=event_a)
    w["checked_in"] = await _dancer(db, "Zelda", "Checkin")
    await insert_checkin(
        db,
        session_id=session_a,
        song_id=await insert_song(db, w["checked_in"]),
        submitted_by=w["checked_in"],
        queued=False,
    )
    w["leader"] = await _dancer(db, "Zelda", "Leader")
    w["follower"] = await _dancer(db, "Zelda", "Follower")
    pair = await insert_pair(
        db, w["leader"], await _linked_partner(db, w["leader"], w["follower"])
    )
    await insert_entry(
        db,
        session_id=session_a,
        queue_type="priority",
        position=1,
        song_id=await insert_song(db, w["leader"]),
        submitted_by=w["leader"],
        pair_id=pair,
    )
    w["manager_of_couple"] = await _dancer(db, "Zelda", "Manager")
    await insert_checkin(
        db,
        session_id=session_a,
        song_id=await insert_song(db, w["manager_of_couple"]),
        submitted_by=w["manager_of_couple"],
        managed_partnership_id=await insert_managed(db, w["manager_of_couple"]),
        queued=False,
    )
    # Connected to B only, by a submission and a check-in there.
    w["elsewhere"] = await _dancer(db, "Zelda", "Elsewhere")
    await _submit(db, event_b, w["elsewhere"])
    await insert_checkin(
        db,
        session_id=await insert_floor_session(db, event_id=event_b),
        song_id=await insert_song(db, w["elsewhere"]),
        submitted_by=w["elsewhere"],
    )
    # Connected to nothing.
    w["nobody"] = await _dancer(db, "Zelda", "Nobody")
    return w


async def test_staff_find_the_events_people_and_only_them(
    client: httpx.AsyncClient, person: Callable, db: asyncpg.Connection
) -> None:
    w = await _world(client, db, person)
    connected = [
        w["checked_in"],
        w["follower"],
        w["leader"],
        w["manager_of_couple"],
        w["song_partner"],
        w["submitter"],
        w["staff_member"].id,
    ]
    for role in ("coordinator", "dj", "event-manager"):
        res = await client.get(
            _url(w["a"]), params={"q": "zel"}, headers=w[role].headers
        )
        assert res.status_code == 200, (role, res.text)
        body = res.json()
        assert body["meta"] == {
            "version": "v1",
            "count": len(connected),
            "total": len(connected),
        }
        # By name: the Zeldas by last name, then Zeldo.
        assert [p["user_id"] for p in body["data"]] == connected, role
        assert w["nobody"] not in res.text
        assert w["elsewhere"] not in res.text

    res = await client.get(
        _url(w["a"]), params={"q": "zel"}, headers=w["coordinator"].headers
    )
    by_id = {p["user_id"]: p for p in res.json()["data"]}
    assert by_id[w["submitter"]] == {
        "user_id": w["submitter"],
        "display_name": "Zelda Submitter",
        "email_masked": "z•••@example.test",
    }
    assert by_id[w["staff_member"].id]["display_name"] == "Zeldo"
    assert by_id[w["staff_member"].id]["email_masked"] == "z•••@example.test"


async def test_the_full_email_is_never_in_the_body(
    client: httpx.AsyncClient, person: Callable, db: asyncpg.Connection
) -> None:
    w = await _world(client, db, person)
    res = await client.get(_url(w["a"]), params={"q": "zel"}, headers=w["dj"].headers)
    assert res.status_code == 200, res.text
    assert res.json()["meta"]["count"] == 7
    emails = {
        r["email"]
        for r in await db.fetch(
            "SELECT email FROM users WHERE id = ANY($1::text[])",
            [p["user_id"] for p in res.json()["data"]],
        )
    }
    assert len(emails) == 7
    for email in emails:
        assert email not in res.text
        assert email.split("@")[0] not in res.text
    # Nor is the email searched: a local part matches no one by name.
    by_email = await client.get(
        _url(w["a"]), params={"q": "zelda.submitter@"}, headers=w["dj"].headers
    )
    assert by_email.status_code == 200
    assert by_email.json()["data"] == []


async def test_name_matching_is_case_insensitive_substring(
    client: httpx.AsyncClient, person: Callable, db: asyncpg.Connection
) -> None:
    w = await _world(client, db, person)
    for q in ("SUBMITTER", "ubmit", "  zelda submitter  ", "zElDa sUb"):
        res = await client.get(
            _url(w["a"]), params={"q": q}, headers=w["coordinator"].headers
        )
        assert res.status_code == 200, (q, res.text)
        assert [p["user_id"] for p in res.json()["data"]] == [w["submitter"]], q
    # LIKE's wildcards are taken literally.
    res = await client.get(
        _url(w["a"]), params={"q": "%_"}, headers=w["coordinator"].headers
    )
    assert res.json()["data"] == []


async def test_display_name_is_preferred_and_searched(
    client: httpx.AsyncClient, person: Callable, db: asyncpg.Connection
) -> None:
    w = await _world(client, db, person)
    await db.execute(
        "UPDATE users SET display_name = 'DJ Zed' WHERE id = $1", w["submitter"]
    )
    res = await client.get(
        _url(w["a"]), params={"q": "dj z"}, headers=w["coordinator"].headers
    )
    assert [(p["user_id"], p["display_name"]) for p in res.json()["data"]] == [
        (w["submitter"], "DJ Zed")
    ]


async def test_results_are_capped_and_deduplicated(
    client: httpx.AsyncClient, person: Callable, db: asyncpg.Connection
) -> None:
    admin = await person("admin", admin=True)
    event_a = await _event(client, admin, "A")
    coordinator = await person("carol")
    await _grant(client, admin, event_a, coordinator.id, "coordinator")
    session_a = await insert_floor_session(db, event_id=event_a)

    # Connected four ways: two submissions, a queued check-in, and staff.
    twice = await _dancer(db, "Aaron", "Often")
    await _submit(db, event_a, twice)
    await _submit(db, event_a, twice)
    await insert_checkin(
        db,
        session_id=session_a,
        song_id=await insert_song(db, twice),
        submitted_by=twice,
    )
    await db.execute(
        "INSERT INTO event_staff_grants (event_id, user_id, role, granted_by,"
        " granted_at) VALUES ($1, $2, 'coordinator', 'test', 1)",
        event_a,
        twice,
    )
    many = [await _dancer(db, "Many", f"Dancer{i:02d}") for i in range(25)]
    for user_id in many:
        await _submit(db, event_a, user_id)

    res = await client.get(
        _url(event_a), params={"q": "aaron"}, headers=coordinator.headers
    )
    assert [p["user_id"] for p in res.json()["data"]] == [twice]

    res = await client.get(
        _url(event_a), params={"q": "many"}, headers=coordinator.headers
    )
    body = res.json()
    assert body["meta"]["count"] == 20
    assert [p["user_id"] for p in body["data"]] == many[:20]


async def test_others_are_refused_and_an_admin_is_not(
    client: httpx.AsyncClient, person: Callable, db: asyncpg.Connection
) -> None:
    w = await _world(client, db, person)
    dancer = await person("zoe")

    for who in (w["b_staff"], dancer):
        res = await client.get(_url(w["a"]), params={"q": "zel"}, headers=who.headers)
        assert res.status_code == 403
        assert res.json()["error"] == {
            "code": "FORBIDDEN",
            "message": "Admin access required",
        }
    res = await client.get(
        _url(w["a"]), params={"q": "zel"}, headers=w["admin"].headers
    )
    assert res.status_code == 200
    assert res.json()["meta"]["count"] == 7
    # B's staff find B's people on B.
    res = await client.get(
        _url(w["b"]), params={"q": "zel"}, headers=w["b_staff"].headers
    )
    assert [p["user_id"] for p in res.json()["data"]] == [w["elsewhere"]]

    # Each decision is audited, on the event in the path.
    audited = await db.fetch(
        "SELECT subject, resource, allowed FROM identity_audit_events"
        " WHERE scope = $1 AND subject = ANY($2::text[])",
        SCOPE,
        [w["b_staff"].id, dancer.id, w["coordinator"].id],
    )
    assert {(r["subject"], r["resource"], r["allowed"]) for r in audited} == {
        (w["b_staff"].id, f"event:{w['a']}", False),
        (w["b_staff"].id, f"event:{w['b']}", True),
        (dancer.id, f"event:{w['a']}", False),
    }


async def test_q_is_required_and_at_least_two_characters(
    client: httpx.AsyncClient, person: Callable, db: asyncpg.Connection
) -> None:
    w = await _world(client, db, person)
    headers = w["coordinator"].headers
    for params in ({}, {"q": ""}, {"q": "z"}, {"q": "  z  "}, {"q": ["zel", "zel"]}):
        res = await client.get(_url(w["a"]), params=params, headers=headers)
        assert res.status_code == 400, (params, res.text)
        assert res.json()["error"]["code"] == "VALIDATION_ERROR"
        assert res.json()["error"]["message"].startswith("q: ")
    # Two characters is enough.
    res = await client.get(_url(w["a"]), params={"q": "ze"}, headers=headers)
    assert res.status_code == 200


async def test_unknown_event_is_404_for_an_admin_and_403_otherwise(
    client: httpx.AsyncClient, person: Callable
) -> None:
    admin = await person("admin", admin=True)
    alice = await person("alice")

    res = await client.get(_url("evt_none"), params={"q": "al"}, headers=admin.headers)
    assert res.status_code == 404
    assert res.json()["error"] == {"code": "NOT_FOUND", "message": "Event not found"}
    res = await client.get(_url("evt_none"), params={"q": "al"}, headers=alice.headers)
    assert res.status_code == 403


async def test_requires_a_token(client: httpx.AsyncClient) -> None:
    res = await client.get(_url("evt_any"), params={"q": "al"})
    assert res.status_code == 401
    assert res.json()["error"]["code"] == "UNAUTHORIZED"
