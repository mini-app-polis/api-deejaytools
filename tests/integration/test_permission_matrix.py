"""Every scope-guarded route, against every kind of caller (ADR-0007, ADR-0012).

The routes and the scope each requires are read from the app itself (each
route's ``require_scope`` / ``require_event_scope`` guard), so a new route is
covered the day it is added. What each caller may do is read from the
identity store (role scopes, and the explicit grants an event's staff roles
write), never from a list kept here. The test checks the two agree over HTTP:

- an allowed caller gets past authorization (any answer but 401 or 403;
  a 404 for a made-up id is still "allowed");
- a refused caller gets exactly 403, before any validation or lookup.

The callers: someone with no roles, a dancer, the event's coordinator, DJ and
event manager, someone holding all three staff roles on another event, and
an admin. An event-scoped route is called on event A, naming it the way the
route does (path, query or body; the event itself, one of its sessions, or a
queue entry in one), so a grant on B must never reach A.

That check alone would follow a wrong guard (a staff action made admin-only
by mistake still agrees with itself), so who may call each route is also
pinned in docs/permissions.json, and any change to it is a reviewed diff.
After a deliberate change, regenerate it with the suite against Postgres:

    UPDATE_PERMISSIONS=1 uv run pytest tests/integration/test_permission_matrix.py
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import asyncpg
import httpx
import pytest
from fastapi.routing import APIRoute

from api_deejaytools.auth import EventRef, event_resource
from api_deejaytools.main import app

from .content_helpers import insert_event, new_id
from .queue_helpers import dancer_pair, insert_entry, insert_floor_session

STAFF_ROLES = ("coordinator", "dj", "event-manager")

#: Who may call each route: committed, regenerated on purpose (see above).
TABLE = Path(__file__).resolve().parents[2] / "docs" / "permissions.json"
UPDATE = os.environ.get("UPDATE_PERMISSIONS") == "1"
OBSERVED: dict[str, list[str]] = {}


def _table() -> dict[str, list[str]]:
    routes: dict[str, list[str]] = json.loads(TABLE.read_text())["routes"]
    return routes


@pytest.fixture(scope="module", autouse=True)
def _write_table_when_updating() -> Iterator[None]:
    yield
    if UPDATE and OBSERVED:
        about = (
            "Who may call each scope-guarded route, as "
            "tests/integration/test_permission_matrix.py observes it over HTTP. "
            "Callers: no-roles, dancer, the event's coordinator, dj and "
            "event-manager, other-event-staff (all three roles on another "
            "event), admin. Regenerate after a deliberate change: "
            "UPDATE_PERMISSIONS=1."
        )
        doc = {"about": about, "routes": dict(sorted(OBSERVED.items()))}
        TABLE.write_text(json.dumps(doc, indent=2) + "\n")


@dataclass(frozen=True)
class Guarded:
    method: str
    path: str
    scope: str
    refs: tuple[EventRef, ...]  # empty: decided on roles alone

    def __str__(self) -> str:
        return f"{self.method} {self.path}"


def _routes(routes: Any) -> Iterator[Any]:
    for r in routes:
        if isinstance(r, APIRoute) or type(r).__name__ == "_EffectiveRouteContext":
            yield r
        elif type(r).__name__ == "_IncludedRouter":
            found = r.effective_candidates
            yield from _routes(found() if callable(found) else found)
        elif hasattr(r, "routes"):
            yield from _routes(r.routes)


def _guard(dependant: Any) -> dict[str, Any] | None:
    """The scope guard among a route's dependencies, as its closure holds it."""
    for dep in dependant.dependencies:
        name = getattr(dep.call, "__qualname__", "")
        if name.startswith(("require_scope.", "require_event_scope.")):
            cells = dep.call.__closure__ or ()
            return dict(
                zip(dep.call.__code__.co_freevars, (c.cell_contents for c in cells))
            )
        found = _guard(dep)
        if found:
            return found
    return None


def _guarded() -> list[Guarded]:
    out = []
    for route in _routes(app.routes):
        guard = _guard(route.dependant)
        if guard is None:
            continue
        for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
            out.append(
                Guarded(
                    method, route.path, guard["scope"], tuple(guard.get("refs", ()))
                )
            )
    return sorted(out, key=str)


GUARDED = _guarded()


def test_the_matrix_finds_the_guarded_routes() -> None:
    # A guard the walk stopped recognising would quietly empty the matrix.
    assert len(GUARDED) > 50
    assert any(g.refs for g in GUARDED)


@dataclass
class Event:
    id: str
    session_id: str
    queue_entry_id: str


async def _event_with_a_queue(db: asyncpg.Connection, name: str) -> Event:
    event_id = await insert_event(db, name=name)
    session_id = await insert_floor_session(db, event_id=event_id)
    pair = await dancer_pair(db)
    entry = await insert_entry(
        db,
        session_id=session_id,
        queue_type="priority",
        position=1,
        song_id=pair["song_id"],
        submitted_by=pair["user_id"],
        pair_id=pair["pair_id"],
    )
    return Event(event_id, session_id, entry["entry_id"])


def _ref_value(ref: EventRef, event: Event) -> str:
    return {
        "event": event.id,
        "session": event.session_id,
        "queue_entry": event.queue_entry_id,
    }[ref.kind]


def _request(route: Guarded, event: Event) -> dict[str, Any]:
    """The route's method, URL and body, naming ``event`` where the guard looks."""
    path_refs = {r.name: r for r in route.refs if r.source == "path"}
    query = {
        r.name: _ref_value(r, event) for r in route.refs[:1] if r.source == "query"
    }
    body = {r.name: _ref_value(r, event) for r in route.refs if r.source == "body"}

    def fill(m: re.Match[str]) -> str:
        name = m.group(1)
        if name in path_refs:
            return _ref_value(path_refs[name], event)
        return "coordinator" if name == "role" else "probe"

    url = re.sub(r"\{([^}]+)\}", fill, route.path)
    kwargs: dict[str, Any] = {"params": query}
    if route.method in ("POST", "PUT", "PATCH"):
        kwargs["json"] = body
    return {"method": route.method, "url": url, **kwargs}


async def _scopes(
    db: asyncpg.Connection, user_id: str, event_id: str
) -> tuple[set[str], set[str]]:
    """What the store gives the person: through their roles (anywhere), and
    through explicit grants on ``event_id``."""
    principal = await db.fetchval(
        "SELECT id FROM identity_principals WHERE subject = $1", user_id
    )
    by_role = await db.fetch(
        "SELECT rs.scope FROM identity_principal_roles pr"
        " JOIN identity_role_scopes rs ON rs.role_name = pr.role_name"
        " WHERE pr.principal_id = $1",
        principal,
    )
    granted = await db.fetch(
        "SELECT scope FROM identity_explicit_grants WHERE principal_id = $1 AND resource = $2",
        principal,
        event_resource(event_id),
    )
    return {r["scope"] for r in by_role}, {r["scope"] for r in granted}


@pytest.mark.parametrize("route", GUARDED, ids=str)
async def test_every_caller_gets_what_the_store_gives_them(
    route: Guarded,
    client: httpx.AsyncClient,
    person: Callable[..., Any],
    db: asyncpg.Connection,
) -> None:
    admin = await person("admin", admin=True)
    event_a = await _event_with_a_queue(db, "A")
    event_b = await _event_with_a_queue(db, "B")

    async def grant(event_id: str, user_id: str, role: str) -> None:
        res = await client.put(
            f"/v1/events/{event_id}/staff/{user_id}/{role}", headers=admin.headers
        )
        assert res.status_code == 200, res.text

    no_role = await person("nora")
    await db.execute(
        "DELETE FROM identity_principal_roles WHERE principal_id ="
        " (SELECT id FROM identity_principals WHERE subject = $1)",
        no_role.id,
    )
    callers: dict[str, Any] = {"no-roles": no_role, "dancer": await person("dana")}
    for role in STAFF_ROLES:
        callers[role] = staff = await person(role.replace("-", ""))
        await grant(event_a.id, staff.id, role)
    callers["other-event-staff"] = other = await person("otto")
    for role in STAFF_ROLES:
        await grant(event_b.id, other.id, role)
    callers["admin"] = admin

    expected: dict[str, bool] = {}
    for label, caller in callers.items():
        by_role, on_a = await _scopes(db, caller.id, event_a.id)
        expected[label] = route.scope in by_role or (
            bool(route.refs) and route.scope in on_a
        )

    # Refused callers first, then the allowed ones from least to most: an
    # allowed call may change things (withdraw the queue entry, delete a
    # session), so each caller gets fresh targets on A to name.
    wrong = []
    allowed: list[str] = []
    for label in sorted(callers, key=lambda k: (expected[k], list(callers).index(k))):
        target = event_a
        if route.refs and any(r.kind != "event" for r in route.refs):
            fresh = await insert_floor_session(db, event_id=event_a.id)
            pair = await dancer_pair(db, first=new_id("f"))
            entry = await insert_entry(
                db,
                session_id=fresh,
                queue_type="priority",
                position=1,
                song_id=pair["song_id"],
                submitted_by=pair["user_id"],
                pair_id=pair["pair_id"],
            )
            target = Event(event_a.id, fresh, entry["entry_id"])
        res = await client.request(
            **_request(route, target), headers=callers[label].headers
        )
        if res.status_code not in (401, 403):
            allowed.append(label)
        if expected[label] and res.status_code in (401, 403):
            wrong.append(
                f"{label}: should be allowed ({route.scope}), got {res.status_code}"
            )
        if not expected[label] and res.status_code != 403:
            wrong.append(
                f"{label}: should be refused ({route.scope}), got {res.status_code}"
            )
    assert not wrong, f"{route}:\n  " + "\n  ".join(wrong)

    observed = [label for label in callers if label in allowed]
    OBSERVED[str(route)] = observed
    if not UPDATE:
        pinned = _table().get(str(route))
        assert pinned == observed, (
            f"{route}: who may call it changed. Pinned in docs/permissions.json: "
            f"{pinned}; now: {observed}. If deliberate, regenerate the table "
            "(UPDATE_PERMISSIONS=1) and commit it with the change."
        )
