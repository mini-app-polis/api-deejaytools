"""/v1/events/{id}/song-submissions: an event's entries, for its staff (ADR-0012)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import asyncpg
import httpx

from api_deejaytools.event_connections import mask_email

from .content_helpers import insert_song

EVENT = {"name": "Swing Fling", "start_date": "2026-05-01", "end_date": "2026-05-03"}
SCOPE = "deejaytools.entries.read"


def _url(event_id: str) -> str:
    return f"/v1/events/{event_id}/song-submissions"


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


async def _submit(
    db: asyncpg.Connection, sid: str, event_id: str, user_id: str, created_at: int
) -> None:
    song = await insert_song(db, user_id, division="Classic", season_year="2026")
    await db.execute(
        "INSERT INTO event_song_submissions (id, event_id, song_id,"
        " submitted_by_user_id, created_at) VALUES ($1, $2, $3, $4, $5)",
        sid,
        event_id,
        song,
        user_id,
        created_at,
    )


async def _two_events(
    client: httpx.AsyncClient, db: asyncpg.Connection, admin: Any, dancer: Any
) -> tuple[str, str]:
    """Events A and B, each with submissions from ``dancer``."""
    event_a = await _event(client, admin, "A")
    event_b = await _event(client, admin, "B")
    await _submit(db, "s_a1", event_a, dancer.id, 10)
    await _submit(db, "s_a2", event_a, dancer.id, 20)
    await _submit(db, "s_b1", event_b, dancer.id, 30)
    return event_a, event_b


async def test_a_coordinator_sees_their_event_and_only_theirs(
    client: httpx.AsyncClient, person: Callable, db: asyncpg.Connection
) -> None:
    admin = await person("admin", admin=True)
    alice = await person("alice")
    coordinator = await person("carol")
    event_a, event_b = await _two_events(client, db, admin, alice)
    await _grant(client, admin, event_a, coordinator.id, "coordinator")

    res = await client.get(_url(event_a), headers=coordinator.headers)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["meta"] == {"version": "v1", "count": 2}
    assert [r["id"] for r in body["data"]] == ["s_a2", "s_a1"]
    assert {(r["event_id"], r["event_name"], r["division"]) for r in body["data"]} == {
        (event_a, "A", "Classic")
    }
    assert all(r["song_label"] for r in body["data"])

    # The same answer the admin list gives for the event, but with the
    # submitter's email masked: staff never see an email whole.
    admin_list = await client.get(
        "/v1/admin/event-song-submissions",
        params={"event_id": event_a},
        headers=admin.headers,
    )
    whole = admin_list.json()
    assert [r["submitter_email"] for r in body["data"]] == [
        mask_email(r["submitter_email"]) for r in whole["data"]
    ]
    assert all("@" in r["submitter_email"] for r in whole["data"])
    assert all("•••@" in r["submitter_email"] for r in body["data"])
    for row in (*body["data"], *whole["data"]):
        row.pop("submitter_email")
    assert whole == body

    # An admin on the event-scoped route sees emails whole.
    as_admin = await client.get(_url(event_a), headers=admin.headers)
    assert all("•••" not in r["submitter_email"] for r in as_admin.json()["data"])

    other = await client.get(_url(event_b), headers=coordinator.headers)
    assert other.status_code == 403
    assert other.json()["error"] == {
        "code": "FORBIDDEN",
        "message": "Admin access required",
    }

    # Both decisions are audited, on the event each request named.
    audited = await db.fetch(
        "SELECT resource, allowed, reason FROM identity_audit_events"
        " WHERE subject = $1 AND scope = $2",
        coordinator.id,
        SCOPE,
    )
    assert {(r["resource"], r["allowed"], r["reason"]) for r in audited} == {
        (f"event:{event_a}", True, "granted_by_explicit_grant"),
        (f"event:{event_b}", False, "no_matching_scope"),
    }


async def test_a_coordinator_gets_no_admin_list(
    client: httpx.AsyncClient, person: Callable, db: asyncpg.Connection
) -> None:
    admin = await person("admin", admin=True)
    alice = await person("alice")
    coordinator = await person("carol")
    event_a, _ = await _two_events(client, db, admin, alice)
    await _grant(client, admin, event_a, coordinator.id, "coordinator")

    res = await client.get(
        "/v1/admin/event-song-submissions",
        params={"event_id": event_a},
        headers=coordinator.headers,
    )
    assert res.status_code == 403
    assert res.json()["error"]["code"] == "FORBIDDEN"


async def test_a_dancer_is_refused(
    client: httpx.AsyncClient, person: Callable, db: asyncpg.Connection
) -> None:
    admin = await person("admin", admin=True)
    alice = await person("alice")
    event_a, _ = await _two_events(client, db, admin, alice)

    # Not even their own event's entries: submitting is not staff.
    res = await client.get(_url(event_a), headers=alice.headers)
    assert res.status_code == 403
    assert res.json()["error"]["code"] == "FORBIDDEN"
    denied = await db.fetchval(
        "SELECT count(*) FROM identity_audit_events WHERE subject = $1"
        " AND scope = $2 AND resource = $3 AND NOT allowed",
        alice.id,
        SCOPE,
        f"event:{event_a}",
    )
    assert denied == 1


async def test_a_dj_an_event_manager_and_an_admin_see_it(
    client: httpx.AsyncClient, person: Callable, db: asyncpg.Connection
) -> None:
    admin = await person("admin", admin=True)
    alice = await person("alice")
    dj = await person("dj")
    manager = await person("manager")
    event_a, event_b = await _two_events(client, db, admin, alice)
    await _grant(client, admin, event_a, dj.id, "dj")
    await _grant(client, admin, event_a, manager.id, "event-manager")

    for who in (dj, manager, admin):
        res = await client.get(_url(event_a), headers=who.headers)
        assert res.status_code == 200, (who.id, res.text)
        assert [r["id"] for r in res.json()["data"]] == ["s_a2", "s_a1"]
    for who in (dj, manager):
        assert (await client.get(_url(event_b), headers=who.headers)).status_code == 403
    # An admin holds the scope through a role, so every event.
    assert (await client.get(_url(event_b), headers=admin.headers)).status_code == 200


async def test_unknown_event_is_404_after_authorization(
    client: httpx.AsyncClient, person: Callable
) -> None:
    admin = await person("admin", admin=True)
    alice = await person("alice")

    res = await client.get(_url("evt_none"), headers=admin.headers)
    assert res.status_code == 404
    assert res.json()["error"] == {"code": "NOT_FOUND", "message": "Event not found"}
    # Without the scope, the caller learns nothing about the id.
    assert (await client.get(_url("evt_none"), headers=alice.headers)).status_code == (
        403
    )


async def test_requires_a_token(client: httpx.AsyncClient) -> None:
    res = await client.get(_url("evt_any"))
    assert res.status_code == 401
    assert res.json()["error"]["code"] == "UNAUTHORIZED"
