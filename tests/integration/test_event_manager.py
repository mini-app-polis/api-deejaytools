"""The event manager's routes, event-scoped (ADR-0012): the event and its staff.

An event manager of event A edits and deletes A (``deejaytools.events.write``
on ``event:<A>``), lists A's staff and grants and revokes its coordinators
and DJs. It is refused on event B, on the event-manager role anywhere, on
creating an event and on every ``/v1/admin`` route. A DJ or coordinator of A
does not carry ``events.write``; a plain dancer holds none of it; an admin
holds it all through a role. Each decision is audited.
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import asyncpg
import httpx
import pytest
from fastapi.routing import APIRoute

import api_deejaytools.routers as routers_pkg

EVENT = {
    "name": "A",
    "start_date": "2026-05-01",
    "end_date": "2026-05-03",
    "season_year": "2026",
}
FORBIDDEN = {"code": "FORBIDDEN", "message": "Admin access required"}
EVENTS_WRITE = "deejaytools.events.write"
SHARE = "share_event_folder"
UNSHARE = "unshare_event_folder"


async def _event(client: httpx.AsyncClient, admin: Any, name: str) -> str:
    res = await client.post(
        "/v1/events", json={**EVENT, "name": name}, headers=admin.headers
    )
    assert res.status_code == 201, res.text
    event_id: str = res.json()["data"]["id"]
    return event_id


def _staff_url(event_id: str, user_id: str, role: str) -> str:
    return f"/v1/events/{event_id}/staff/{user_id}/{role}"


async def _grant(
    client: httpx.AsyncClient, who: Any, event_id: str, user_id: str, role: str
) -> httpx.Response:
    return await client.put(_staff_url(event_id, user_id, role), headers=who.headers)


async def _revoke(
    client: httpx.AsyncClient, who: Any, event_id: str, user_id: str, role: str
) -> httpx.Response:
    return await client.delete(_staff_url(event_id, user_id, role), headers=who.headers)


@dataclass
class World:
    """Events A and B; an event manager, a DJ and a coordinator of A; a
    dancer; an admin."""

    admin: Any
    manager: Any
    dj: Any
    coordinator: Any
    dancer: Any
    event_a: str
    event_b: str


@pytest.fixture
async def world(client: httpx.AsyncClient, person: Callable[..., Any]) -> World:
    admin = await person("admin", admin=True)
    manager = await person("manager")
    dj = await person("dj")
    coordinator = await person("coordinator")
    dancer = await person("dancer")
    event_a = await _event(client, admin, "A")
    event_b = await _event(client, admin, "B")
    for who, role in (
        (manager, "event-manager"),
        (dj, "dj"),
        (coordinator, "coordinator"),
    ):
        res = await _grant(client, admin, event_a, who.id, role)
        assert res.status_code == 200, res.text
    return World(admin, manager, dj, coordinator, dancer, event_a, event_b)


async def _events_write_audit(
    db: asyncpg.Connection, subject: str
) -> list[tuple[str | None, bool, str]]:
    rows = await db.fetch(
        "SELECT resource, allowed, reason FROM identity_audit_events"
        " WHERE subject = $1 AND scope = $2 ORDER BY occurred_at",
        subject,
        EVENTS_WRITE,
    )
    return [(r["resource"], r["allowed"], r["reason"]) for r in rows]


# --- the event ---------------------------------------------------------------------


async def test_the_event_manager_edits_their_event_only(
    client: httpx.AsyncClient, db: asyncpg.Connection, world: World
) -> None:
    w = world

    def patch(who: Any, event_id: str, name: str) -> Any:
        return client.patch(
            f"/v1/events/{event_id}", json={"name": name}, headers=who.headers
        )

    res = await patch(w.manager, w.event_a, "A by its manager")
    assert res.status_code == 200, res.text
    assert res.json()["data"]["name"] == "A by its manager"

    for who, event_id in (
        (w.manager, w.event_b),
        (w.dj, w.event_a),
        (w.coordinator, w.event_a),
        (w.dancer, w.event_a),
        (w.dancer, w.event_b),
    ):
        res = await patch(who, event_id, "Taken")
        assert res.status_code == 403, (who.id, event_id, res.text)
        assert res.json()["error"] == FORBIDDEN
    names = {
        r["id"]: r["name"]
        for r in await db.fetch(
            "SELECT id, name FROM events WHERE id = ANY($1::text[])",
            [w.event_a, w.event_b],
        )
    }
    assert names == {w.event_a: "A by its manager", w.event_b: "B"}

    # An admin holds the scope through a role: every event.
    assert (await patch(w.admin, w.event_b, "B by an admin")).status_code == 200

    # Audited on the event each request named.
    assert await _events_write_audit(db, w.manager.id) == [
        (f"event:{w.event_a}", True, "granted_by_explicit_grant"),
        (f"event:{w.event_b}", False, "no_matching_scope"),
    ]
    assert await _events_write_audit(db, w.dj.id) == [
        (f"event:{w.event_a}", False, "no_matching_scope"),
    ]
    assert await _events_write_audit(db, w.dancer.id) == [
        (f"event:{w.event_a}", False, "no_matching_scope"),
        (f"event:{w.event_b}", False, "no_matching_scope"),
    ]


async def test_the_event_manager_deletes_their_event_only(
    client: httpx.AsyncClient, db: asyncpg.Connection, world: World
) -> None:
    w = world

    def delete(who: Any, event_id: str) -> Any:
        return client.delete(f"/v1/events/{event_id}", headers=who.headers)

    for who, event_id in (
        (w.manager, w.event_b),
        (w.dj, w.event_a),
        (w.coordinator, w.event_a),
        (w.dancer, w.event_a),
    ):
        res = await delete(who, event_id)
        assert res.status_code == 403, (who.id, event_id, res.text)
        assert res.json()["error"] == FORBIDDEN
    assert await db.fetchval("SELECT count(*) FROM events") == 2

    res = await delete(w.manager, w.event_a)
    assert res.status_code == 200, res.text
    assert res.json()["data"] == {"deleted": True}
    assert await db.fetchval("SELECT array_agg(id) FROM events") == [w.event_b]
    # With A went its staff and every grant on it, the manager's included.
    assert await db.fetchval("SELECT count(*) FROM event_staff_grants") == 0
    assert (
        await db.fetchval(
            "SELECT count(*) FROM identity_explicit_grants WHERE resource = $1",
            f"event:{w.event_a}",
        )
        == 0
    )
    # Its unshares were queued, for its DJ and its event manager.
    unshares = await db.fetch(
        "SELECT user_id FROM drive_jobs WHERE kind = $1 AND event_id = $2",
        UNSHARE,
        w.event_a,
    )
    assert {r["user_id"] for r in unshares} == {w.dj.id, w.manager.id}

    assert (await delete(w.admin, w.event_b)).status_code == 200

    assert await _events_write_audit(db, w.manager.id) == [
        (f"event:{w.event_b}", False, "no_matching_scope"),
        (f"event:{w.event_a}", True, "granted_by_explicit_grant"),
    ]


async def test_a_rename_by_the_event_manager_reshares_the_folder(
    client: httpx.AsyncClient, db: asyncpg.Connection, world: World
) -> None:
    w = world
    before = await db.fetchval("SELECT count(*) FROM drive_jobs WHERE kind = $1", SHARE)
    res = await client.patch(
        f"/v1/events/{w.event_a}", json={"name": "A Renamed"}, headers=w.manager.headers
    )
    assert res.status_code == 200, res.text
    reshared = await db.fetch(
        "SELECT user_id FROM drive_jobs WHERE kind = $1 AND event_id = $2"
        " ORDER BY created_at, ctid OFFSET $3",
        SHARE,
        w.event_a,
        before,
    )
    assert sorted(r["user_id"] for r in reshared) == sorted([w.dj.id, w.manager.id])


async def test_the_event_manager_cannot_create_an_event(
    client: httpx.AsyncClient, db: asyncpg.Connection, world: World
) -> None:
    w = world
    for body in (EVENT, {**EVENT, "event_id": w.event_a, "id": w.event_a}):
        res = await client.post("/v1/events", json=body, headers=w.manager.headers)
        assert res.status_code == 403, res.text
        assert res.json()["error"] == FORBIDDEN
    assert await db.fetchval("SELECT count(*) FROM events") == 2
    # Decided by roles alone: no event on the record.
    assert await _events_write_audit(db, w.manager.id) == [
        (None, False, "no_matching_scope"),
        (None, False, "no_matching_scope"),
    ]


# --- the staff ---------------------------------------------------------------------


async def test_the_event_manager_runs_their_events_staff(
    client: httpx.AsyncClient,
    db: asyncpg.Connection,
    person: Callable[..., Any],
    world: World,
) -> None:
    w = world
    carol = await person("carol")

    listed = await client.get(
        f"/v1/events/{w.event_a}/staff", headers=w.manager.headers
    )
    assert listed.status_code == 200, listed.text
    assert {(g["user_id"], g["role"]) for g in listed.json()["data"]} == {
        (w.manager.id, "event-manager"),
        (w.dj.id, "dj"),
        (w.coordinator.id, "coordinator"),
    }
    other = await client.get(f"/v1/events/{w.event_b}/staff", headers=w.manager.headers)
    assert other.status_code == 403

    # Coordinator and DJ, on A: granted and revoked.
    for role in ("coordinator", "dj"):
        res = await _grant(client, w.manager, w.event_a, carol.id, role)
        assert res.status_code == 200, (role, res.text)
        assert res.json()["data"]["granted_by"] == w.manager.id
    assert {
        r["role"]
        for r in await db.fetch(
            "SELECT role FROM event_staff_grants WHERE user_id = $1", carol.id
        )
    } == {"coordinator", "dj"}
    for role in ("coordinator", "dj"):
        res = await _revoke(client, w.manager, w.event_a, carol.id, role)
        assert res.status_code == 200, (role, res.text)
    assert (
        await _revoke(client, w.manager, w.event_a, w.dj.id, "dj")
    ).status_code == 200

    # On B, nothing; event manager, nowhere.
    refused = [
        await _grant(client, w.manager, w.event_b, carol.id, "dj"),
        await _grant(client, w.manager, w.event_b, carol.id, "coordinator"),
        await _revoke(client, w.manager, w.event_b, carol.id, "dj"),
        await _grant(client, w.manager, w.event_a, carol.id, "event-manager"),
        await _grant(client, w.manager, w.event_b, carol.id, "event-manager"),
        await _revoke(client, w.manager, w.event_a, w.manager.id, "event-manager"),
        await _revoke(client, w.manager, w.event_b, carol.id, "event-manager"),
    ]
    assert [r.status_code for r in refused] == [403] * len(refused)
    assert (
        await db.fetchval(
            "SELECT count(*) FROM event_staff_grants WHERE user_id = $1", carol.id
        )
        == 0
    )
    assert (
        await db.fetchval(
            "SELECT count(*) FROM event_staff_grants WHERE event_id = $1", w.event_b
        )
        == 0
    )
    assert (
        await db.fetchval(
            "SELECT count(*) FROM event_staff_grants"
            " WHERE user_id = $1 AND role = 'event-manager'",
            w.manager.id,
        )
        == 1
    )

    # The staff.read decisions, on the event each request named.
    read = await db.fetch(
        "SELECT resource, allowed FROM identity_audit_events"
        " WHERE subject = $1 AND scope = 'deejaytools.staff.read'"
        " ORDER BY occurred_at",
        w.manager.id,
    )
    assert [(r["resource"], r["allowed"]) for r in read] == [
        (f"event:{w.event_a}", True),
        (f"event:{w.event_b}", False),
    ]


async def test_a_dj_or_coordinator_does_not_run_the_staff(
    client: httpx.AsyncClient,
    person: Callable[..., Any],
    world: World,
) -> None:
    w = world
    carol = await person("carol")
    for who in (w.dj, w.coordinator, w.dancer):
        listed = await client.get(f"/v1/events/{w.event_a}/staff", headers=who.headers)
        assert listed.status_code == 403, who.id
        assert (
            await _grant(client, who, w.event_a, carol.id, "coordinator")
        ).status_code == 403
        assert (
            await _revoke(client, who, w.event_a, w.coordinator.id, "coordinator")
        ).status_code == 403


# --- what stays admin-only ---------------------------------------------------------


def _route_scope(dependant: Any) -> str | None:
    """The scope a route's guard enforces, as the dancer sweep reads it."""
    for dep in dependant.dependencies:
        try:
            scope = inspect.getclosurevars(dep.call).nonlocals.get("scope")
        except TypeError:
            scope = None
        if isinstance(scope, str):
            return scope
        found = _route_scope(dep)
        if found:
            return found
    return None


async def test_the_event_manager_gets_no_admin_route(
    client: httpx.AsyncClient, world: World
) -> None:
    """Every /v1/admin route refuses an event manager, even with its own
    event in every path parameter, in the query and in the body: those
    routes decide by roles alone, and the event manager's scopes are grants
    on one event."""
    w = world
    admin_routes = [
        route
        for info in pkgutil.iter_modules(routers_pkg.__path__)
        for route in importlib.import_module(
            f"api_deejaytools.routers.{info.name}"
        ).router.routes
        if isinstance(route, APIRoute)
        and route.path.startswith("/v1/admin")
        and _route_scope(route.dependant) is not None
    ]
    assert len(admin_routes) > 10  # not vacuous

    headers = {"Content-Type": "application/json", **w.manager.headers}
    wrong = []
    for route in admin_routes:
        path = route.path
        for name in route.param_convertors:
            path = path.replace(f"{{{name}}}", w.event_a)
        for method in route.methods:
            res = await client.request(
                method,
                f"{path}?event_id={w.event_a}",
                content=(
                    f'{{"event_id": "{w.event_a}", "eventId": "{w.event_a}",'
                    f' "role": "admin", "user_id": "{w.manager.id}"}}'
                ).encode(),
                headers=headers,
            )
            if res.status_code != 403 or res.json()["error"] != FORBIDDEN:
                wrong.append(f"{method} {route.path}: {res.status_code}")
    assert wrong == []
