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
    assert body["meta"] == {"version": "v1", "count": 2, "total": 2}
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


def _registrations_url(event_id: str) -> str:
    return f"/v1/events/{event_id}/registrations"


async def _import_one(client: httpx.AsyncClient, admin: Any, event_id: str) -> None:
    res = await client.put(
        f"/v1/admin/events/{event_id}/registrations",
        json={
            "entries": [
                {
                    "division": "Classic",
                    "dancer_1": "Ada Lovelace",
                    "dancer_2": "Bob Jones",
                }
            ]
        },
        headers=admin.headers,
    )
    assert res.status_code == 200, res.text
    entry_id: str = res.json()["data"][0]["id"]
    res = await client.patch(
        f"/v1/admin/event-registrations/{entry_id}",
        json={"note": "called her Tuesday"},
        headers=admin.headers,
    )
    assert res.status_code == 200, res.text


async def test_staff_see_their_events_registrations_without_admin_notes(
    client: httpx.AsyncClient, person: Callable, db: asyncpg.Connection
) -> None:
    admin = await person("admin", admin=True)
    alice = await person("alice")
    dj = await person("dave")
    event_a, event_b = await _two_events(client, db, admin, alice)
    await _import_one(client, admin, event_a)
    await _import_one(client, admin, event_b)
    await _grant(client, admin, event_a, dj.id, "dj")

    res = await client.get(_registrations_url(event_a), headers=dj.headers)
    assert res.status_code == 200, res.text
    data = res.json()["data"]
    assert [(e["dancer_1"], e["note"]) for e in data] == [("Ada Lovelace", None)]

    res = await client.get(_registrations_url(event_a), headers=admin.headers)
    assert res.json()["data"][0]["note"] == "called her Tuesday"

    res = await client.get(_registrations_url(event_b), headers=dj.headers)
    assert res.status_code == 403
    res = await client.get(_registrations_url(event_a), headers=alice.headers)
    assert res.status_code == 403
    assert (await client.get(_registrations_url(event_a))).status_code == 401


async def test_registrations_for_an_unknown_event_is_404(
    client: httpx.AsyncClient, person: Callable
) -> None:
    admin = await person("admin", admin=True)
    res = await client.get(_registrations_url("nope"), headers=admin.headers)
    assert res.status_code == 404


async def test_a_coordinator_manages_their_events_registration_list(
    client: httpx.AsyncClient, person: Callable, db: asyncpg.Connection
) -> None:
    admin = await person("admin", admin=True)
    alice = await person("alice")
    coordinator = await person("carol")
    event_a, event_b = await _two_events(client, db, admin, alice)
    await _grant(client, admin, event_a, coordinator.id, "coordinator")
    url = _registrations_url(event_a)

    # Import: the admin's note survives, but the coordinator never sees it.
    await _import_one(client, admin, event_a)
    res = await client.put(
        url,
        json={
            "entries": [
                {
                    "division": "Classic",
                    "dancer_1": "Ada Lovelace",
                    "dancer_2": "Bob Jones",
                },
                {
                    "division": "Masters",
                    "dancer_1": "Cara Smith",
                    "dancer_2": "Dan Brown",
                },
            ]
        },
        headers=coordinator.headers,
    )
    assert res.status_code == 200, res.text
    data = res.json()["data"]
    assert [(e["dancer_1"], e["note"]) for e in data] == [
        ("Ada Lovelace", None),
        ("Cara Smith", None),
    ]
    ada, cara = data[0]["id"], data[1]["id"]
    kept = await client.get(url, headers=admin.headers)
    assert kept.json()["data"][0]["note"] == "called her Tuesday"

    # Add one at the end.
    res = await client.post(
        url,
        json={"division": "Teams", "dancer_1": "Future Wow", "dancer_2": ""},
        headers=coordinator.headers,
    )
    assert res.status_code == 201, res.text
    added = res.json()["data"]
    assert (added["position"], added["dancer_1"], added["dancer_2"]) == (
        2,
        "Future Wow",
        None,
    )

    # Mark: handled and linked, but not the admin's note.
    res = await client.patch(
        f"{url}/{cara}", json={"handled": True}, headers=coordinator.headers
    )
    assert res.status_code == 200, res.text
    assert res.json()["data"]["handled"] is True
    res = await client.patch(
        f"{url}/{ada}", json={"note": "mine now"}, headers=coordinator.headers
    )
    assert res.status_code == 403

    # Remove.
    res = await client.delete(f"{url}/{added['id']}", headers=coordinator.headers)
    assert res.status_code == 200, res.text
    assert res.json()["data"] == {"deleted": True}
    listed = await client.get(url, headers=coordinator.headers)
    assert [e["id"] for e in listed.json()["data"]] == [ada, cara]

    # Another event's list, and another event's entry through this event, are out of reach.
    other = _registrations_url(event_b)
    assert (
        await client.post(
            other,
            json={"division": "Classic", "dancer_1": "X"},
            headers=coordinator.headers,
        )
    ).status_code == 403
    assert (
        await client.put(other, json={"entries": []}, headers=coordinator.headers)
    ).status_code == 403
    await _import_one(client, admin, event_b)
    b_entry = (await client.get(other, headers=admin.headers)).json()["data"][0]["id"]
    assert (
        await client.delete(f"{url}/{b_entry}", headers=coordinator.headers)
    ).status_code == 404
    assert (
        await client.patch(
            f"{url}/{b_entry}", json={"handled": True}, headers=coordinator.headers
        )
    ).status_code == 404

    # A dancer may not write it.
    assert (
        await client.post(
            url, json={"division": "Classic", "dancer_1": "X"}, headers=alice.headers
        )
    ).status_code == 403


async def test_removing_a_scratched_entry_unhides_its_submissions(
    client: httpx.AsyncClient, person: Callable, db: asyncpg.Connection
) -> None:
    admin = await person("admin", admin=True)
    alice = await person("alice")
    event_a, _ = await _two_events(client, db, admin, alice)
    url = _registrations_url(event_a)
    res = await client.post(
        url,
        json={"division": "Classic", "dancer_1": "Alice", "dancer_2": "Bob"},
        headers=admin.headers,
    )
    entry = res.json()["data"]["id"]
    res = await client.patch(
        f"{url}/{entry}",
        json={"scratched": True, "hide_submission_ids": ["s_a1"]},
        headers=admin.headers,
    )
    assert res.status_code == 200, res.text
    assert res.json()["data"]["hidden_submission_ids"] == ["s_a1"]

    assert (
        await client.delete(f"{url}/{entry}", headers=admin.headers)
    ).status_code == 200
    hidden_by = await db.fetchval(
        "SELECT hidden_by_registration_id FROM event_song_submissions WHERE id = 's_a1'"
    )
    assert hidden_by is None
