"""/v1/events/{id}/staff: per-event staff roles and their explicit grants (ADR-0012)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import asyncpg
import httpx

EVENT = {"name": "Swing Fling", "start_date": "2026-05-01", "end_date": "2026-05-03"}


async def _event(client: httpx.AsyncClient, admin: Any, name: str = "A") -> str:
    res = await client.post(
        "/v1/events", json={**EVENT, "name": name}, headers=admin.headers
    )
    assert res.status_code == 201, res.text
    event_id: str = res.json()["data"]["id"]
    return event_id


def _url(event_id: str, user_id: str, role: str) -> str:
    return f"/v1/events/{event_id}/staff/{user_id}/{role}"


async def _grant(
    client: httpx.AsyncClient, who: Any, event_id: str, user_id: str, role: str
) -> httpx.Response:
    return await client.put(_url(event_id, user_id, role), headers=who.headers)


async def _explicit(db: asyncpg.Connection, user_id: str) -> set[tuple[str, str]]:
    rows = await db.fetch(
        "SELECT g.scope, g.resource FROM identity_explicit_grants g"
        " JOIN identity_principals p ON p.id = g.principal_id"
        " WHERE p.subject = $1",
        user_id,
    )
    return {(r["scope"], r["resource"]) for r in rows}


async def _role_scopes(db: asyncpg.Connection, role_name: str) -> set[str]:
    rows = await db.fetch(
        "SELECT scope FROM identity_role_scopes WHERE role_name = $1", role_name
    )
    return {r["scope"] for r in rows}


async def test_admin_grants_and_lists(
    client: httpx.AsyncClient, person: Callable, db: asyncpg.Connection
) -> None:
    admin = await person("admin", admin=True)
    bob = await person("bob")
    event_id = await _event(client, admin)

    res = await _grant(client, admin, event_id, bob.id, "dj")
    assert res.status_code == 200, res.text
    grant = res.json()["data"]
    assert {k: grant[k] for k in ("event_id", "user_id", "role", "granted_by")} == {
        "event_id": event_id,
        "user_id": bob.id,
        "role": "dj",
        "granted_by": admin.id,
    }
    assert grant["first_name"] == "Bob"

    listed = await client.get(f"/v1/events/{event_id}/staff", headers=admin.headers)
    assert listed.status_code == 200
    assert listed.json() == {"data": [grant], "meta": {"version": "v1", "count": 1}}

    # One explicit grant per scope of the role, on this event only, never a
    # principal role.
    dj_scopes = await _role_scopes(db, "deejaytools-event-dj")
    assert dj_scopes
    assert await _explicit(db, bob.id) == {(s, f"event:{event_id}") for s in dj_scopes}
    sources = await db.fetch(
        "SELECT DISTINCT g.granted_by FROM identity_explicit_grants g"
        " JOIN identity_principals p ON p.id = g.principal_id WHERE p.subject = $1",
        bob.id,
    )
    assert [r["granted_by"] for r in sources] == [f"deejaytools-event-dj:{admin.id}"]
    roles = await db.fetch(
        "SELECT r.role_name FROM identity_principal_roles r"
        " JOIN identity_principals p ON p.id = r.principal_id WHERE p.subject = $1",
        bob.id,
    )
    assert [r["role_name"] for r in roles] == ["deejaytools-dancer"]


async def test_grant_is_idempotent(
    client: httpx.AsyncClient, person: Callable, db: asyncpg.Connection
) -> None:
    admin = await person("admin", admin=True)
    bob = await person("bob")
    event_id = await _event(client, admin)

    first = await _grant(client, admin, event_id, bob.id, "coordinator")
    again = await _grant(client, admin, event_id, bob.id, "coordinator")
    assert first.status_code == again.status_code == 200
    assert again.json() == first.json()
    assert (
        await db.fetchval(
            "SELECT count(*) FROM event_staff_grants WHERE user_id = $1", bob.id
        )
        == 1
    )


async def test_a_dancer_cannot_grant_or_list(
    client: httpx.AsyncClient, person: Callable, db: asyncpg.Connection
) -> None:
    admin = await person("admin", admin=True)
    alice = await person("alice")
    event_id = await _event(client, admin)

    res = await _grant(client, alice, event_id, alice.id, "coordinator")
    assert res.status_code == 403
    assert res.json()["error"]["code"] == "FORBIDDEN"
    revoke = await client.delete(
        _url(event_id, alice.id, "coordinator"), headers=alice.headers
    )
    assert revoke.status_code == 403
    listed = await client.get(f"/v1/events/{event_id}/staff", headers=alice.headers)
    assert listed.status_code == 403
    assert await db.fetchval("SELECT count(*) FROM event_staff_grants") == 0
    assert await _explicit(db, alice.id) == set()


async def test_a_grant_on_event_a_does_not_reach_event_b(
    client: httpx.AsyncClient, person: Callable, db: asyncpg.Connection
) -> None:
    admin = await person("admin", admin=True)
    manager = await person("manager")
    carol = await person("carol")
    event_a = await _event(client, admin, "A")
    event_b = await _event(client, admin, "B")
    assert (
        await _grant(client, admin, event_a, manager.id, "event-manager")
    ).status_code == 200

    # The event manager's staff.write is held on A only.
    on_a = await _grant(client, manager, event_a, carol.id, "dj")
    assert on_a.status_code == 200, on_a.text
    assert on_a.json()["data"]["granted_by"] == manager.id
    on_b = await _grant(client, manager, event_b, carol.id, "dj")
    assert on_b.status_code == 403
    assert (
        await db.fetchval(
            "SELECT count(*) FROM event_staff_grants WHERE event_id = $1", event_b
        )
        == 0
    )

    # Both decisions are audited, on the event each request named.
    audited = await db.fetch(
        "SELECT resource, allowed, reason FROM identity_audit_events"
        " WHERE subject = $1 AND scope = 'deejaytools.staff.write'"
        " AND resource IN ($2, $3)",
        manager.id,
        f"event:{event_a}",
        f"event:{event_b}",
    )
    assert {(r["resource"], r["allowed"], r["reason"]) for r in audited} == {
        (f"event:{event_a}", True, "granted_by_explicit_grant"),
        (f"event:{event_b}", False, "no_matching_scope"),
    }


async def test_only_an_admin_grants_or_revokes_event_manager(
    client: httpx.AsyncClient, person: Callable, db: asyncpg.Connection
) -> None:
    admin = await person("admin", admin=True)
    manager = await person("manager")
    other = await person("other")
    event_id = await _event(client, admin)
    await _grant(client, admin, event_id, manager.id, "event-manager")

    res = await _grant(client, manager, event_id, other.id, "event-manager")
    assert res.status_code == 403
    revoke = await client.delete(
        _url(event_id, manager.id, "event-manager"), headers=manager.headers
    )
    assert revoke.status_code == 403
    assert {
        r["user_id"]
        for r in await db.fetch(
            "SELECT user_id FROM event_staff_grants WHERE role = 'event-manager'"
        )
    } == {manager.id}
    denied = await db.fetchval(
        "SELECT count(*) FROM identity_audit_events WHERE subject = $1"
        " AND resource = $2 AND NOT allowed",
        manager.id,
        f"event:{event_id}/staff/{other.id}/event-manager",
    )
    assert denied == 1


async def test_revoke_removes_access(
    client: httpx.AsyncClient, person: Callable, db: asyncpg.Connection
) -> None:
    admin = await person("admin", admin=True)
    manager = await person("manager")
    carol = await person("carol")
    event_id = await _event(client, admin)
    await _grant(client, admin, event_id, manager.id, "event-manager")
    assert (await _grant(client, manager, event_id, carol.id, "dj")).status_code == 200

    res = await client.delete(
        _url(event_id, manager.id, "event-manager"), headers=admin.headers
    )
    assert res.status_code == 200
    assert res.json() == {"data": {"deleted": True}, "meta": {"version": "v1"}}
    assert await _explicit(db, manager.id) == set()
    assert (
        await _grant(client, manager, event_id, carol.id, "coordinator")
    ).status_code == 403

    # Idempotent: revoking again still answers 200.
    again = await client.delete(
        _url(event_id, manager.id, "event-manager"), headers=admin.headers
    )
    assert again.status_code == 200


async def test_revoke_keeps_a_scope_another_role_still_carries(
    client: httpx.AsyncClient, person: Callable, db: asyncpg.Connection
) -> None:
    admin = await person("admin", admin=True)
    carol = await person("carol")
    event_id = await _event(client, admin)
    resource = f"event:{event_id}"
    await _grant(client, admin, event_id, carol.id, "coordinator")
    await _grant(client, admin, event_id, carol.id, "dj")
    dj_scopes = await _role_scopes(db, "deejaytools-event-dj")
    assert "deejaytools.entries.read" in dj_scopes

    await client.delete(_url(event_id, carol.id, "coordinator"), headers=admin.headers)
    assert await _explicit(db, carol.id) == {(s, resource) for s in dj_scopes}

    await client.delete(_url(event_id, carol.id, "dj"), headers=admin.headers)
    assert await _explicit(db, carol.id) == set()


async def test_me_lists_event_roles(
    client: httpx.AsyncClient, person: Callable
) -> None:
    admin = await person("admin", admin=True)
    carol = await person("carol")
    event_id = await _event(client, admin)

    before = (await client.get("/v1/auth/me", headers=carol.headers)).json()["data"]
    assert before["event_roles"] == []
    assert before["role"] == "user"

    await _grant(client, admin, event_id, carol.id, "dj")
    await _grant(client, admin, event_id, carol.id, "coordinator")
    me = (await client.get("/v1/auth/me", headers=carol.headers)).json()["data"]
    assert me["event_roles"] == [
        {"event_id": event_id, "role": "coordinator"},
        {"event_id": event_id, "role": "dj"},
    ]
    # A staff role is not admin on the wire.
    assert me["role"] == "user"
    assert {k: v for k, v in me.items() if k != "event_roles"} == {
        k: v for k, v in before.items() if k != "event_roles"
    }


async def test_deleting_the_event_removes_its_grants(
    client: httpx.AsyncClient, person: Callable, db: asyncpg.Connection
) -> None:
    admin = await person("admin", admin=True)
    carol = await person("carol")
    event_a = await _event(client, admin, "A")
    event_b = await _event(client, admin, "B")
    await _grant(client, admin, event_a, carol.id, "dj")
    await _grant(client, admin, event_b, carol.id, "coordinator")

    res = await client.delete(f"/v1/events/{event_a}", headers=admin.headers)
    assert res.status_code == 200, res.text
    assert [
        r["event_id"]
        for r in await db.fetch(
            "SELECT event_id FROM event_staff_grants WHERE user_id = $1", carol.id
        )
    ] == [event_b]
    assert {resource for _, resource in await _explicit(db, carol.id)} == {
        f"event:{event_b}"
    }


async def test_unknown_role_event_or_user(
    client: httpx.AsyncClient, person: Callable, db: asyncpg.Connection
) -> None:
    admin = await person("admin", admin=True)
    carol = await person("carol")
    event_id = await _event(client, admin)

    bad_role = await _grant(client, admin, event_id, carol.id, "admin")
    assert bad_role.status_code == 400
    assert bad_role.json()["error"]["code"] == "VALIDATION_ERROR"

    no_event = await _grant(client, admin, "nope", carol.id, "dj")
    assert no_event.status_code == 404
    assert no_event.json()["error"] == {
        "code": "NOT_FOUND",
        "message": "Event not found",
    }
    assert (
        await client.get("/v1/events/nope/staff", headers=admin.headers)
    ).status_code == 404

    no_user = await _grant(client, admin, event_id, "user_nobody", "dj")
    assert no_user.status_code == 404
    assert no_user.json()["error"] == {"code": "NOT_FOUND", "message": "User not found"}
    assert await db.fetchval("SELECT count(*) FROM event_staff_grants") == 0
