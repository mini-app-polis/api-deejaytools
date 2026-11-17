"""Migration 014 on DJs already on an event's staff.

Grants made before it carried the DJ role's old scopes as explicit grants on
the event. Re-running the migration over such grants removes the DJ's
session and registration writes, and keeps each one the person still holds
through another role on the same event.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import asyncpg
import httpx

from .queue_helpers import insert_floor_session

ROOT = Path(__file__).resolve().parents[2]
MIGRATION = ROOT / "migrations" / "014_dj_no_session_or_registration_writes.sql"

SESSIONS = "deejaytools.sessions.write"
REGISTRATIONS = "deejaytools.registrations.write"


async def _event(client: httpx.AsyncClient, admin: Any, name: str) -> str:
    res = await client.post(
        "/v1/events",
        json={"name": name, "start_date": "2026-06-01", "end_date": "2026-06-02"},
        headers=admin.headers,
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


async def _old_dj_grants(db: asyncpg.Connection, user_id: str, event_id: str) -> None:
    """The explicit grants a DJ grant made before migration 014."""
    for scope in (SESSIONS, REGISTRATIONS):
        await db.execute(
            "INSERT INTO identity_explicit_grants"
            " (principal_id, scope, resource, granted_by)"
            " SELECT id, $2, $3, 'deejaytools-event-dj:admin'"
            " FROM identity_principals WHERE subject = $1"
            " ON CONFLICT (principal_id, scope, resource) DO NOTHING",
            user_id,
            scope,
            f"event:{event_id}",
        )


async def _writes(db: asyncpg.Connection, user_id: str, event_id: str) -> set[str]:
    rows = await db.fetch(
        "SELECT g.scope FROM identity_explicit_grants g"
        " JOIN identity_principals p ON p.id = g.principal_id"
        " WHERE p.subject = $1 AND g.resource = $2 AND g.scope = ANY($3::text[])",
        user_id,
        f"event:{event_id}",
        [SESSIONS, REGISTRATIONS],
    )
    return {r["scope"] for r in rows}


async def test_a_djs_old_writes_go_and_other_roles_keep_theirs(
    client: httpx.AsyncClient, db: asyncpg.Connection, person: Callable[..., Any]
) -> None:
    admin = await person("admin", admin=True)
    dj = await person("dj")
    dj_manager = await person("djmanager")
    dj_coordinator = await person("djcoordinator")
    event_a = await _event(client, admin, "A")
    event_b = await _event(client, admin, "B")
    await _grant(client, admin, event_a, dj.id, "dj")
    await _grant(client, admin, event_b, dj.id, "event-manager")
    for who, role in ((dj_manager, "event-manager"), (dj_coordinator, "coordinator")):
        await _grant(client, admin, event_a, who.id, "dj")
        await _grant(client, admin, event_a, who.id, role)
    for who in (dj, dj_manager, dj_coordinator):
        await _old_dj_grants(db, who.id, event_a)

    await db.execute(MIGRATION.read_text())

    # Only a DJ on event A: both writes gone there, untouched on B, which
    # they manage.
    assert await _writes(db, dj.id, event_a) == set()
    assert await _writes(db, dj.id, event_b) == {SESSIONS, REGISTRATIONS}
    # Also its event manager: keeps both.
    assert await _writes(db, dj_manager.id, event_a) == {SESSIONS, REGISTRATIONS}
    # Also its coordinator: keeps the registration list, not the sessions.
    assert await _writes(db, dj_coordinator.id, event_a) == {REGISTRATIONS}

    # And the API agrees: the DJ is refused a session status change on A.
    session_id = await insert_floor_session(db, event_id=event_a)
    res = await client.patch(
        f"/v1/sessions/{session_id}/status",
        json={"status": "cancelled"},
        headers=dj.headers,
    )
    assert res.status_code == 403, res.text
    res = await client.patch(
        f"/v1/sessions/{session_id}/status",
        json={"status": "cancelled"},
        headers=dj_manager.headers,
    )
    assert res.status_code == 200, res.text
