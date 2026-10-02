"""/v1/sessions: optional-caller reads, admin writes, and their rules."""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import date, timedelta
from typing import Any

import asyncpg
import httpx

HOUR = 3_600_000


def _times(offset_hours: float = 0) -> dict[str, int]:
    """Check-in an hour ago, floor trial now for an hour, shifted by offset."""
    now = int(time.time() * 1000) + int(offset_hours * HOUR)
    return {
        "checkin_opens_at": now - HOUR,
        "floor_trial_starts_at": now,
        "floor_trial_ends_at": now + HOUR,
    }


async def _event(client: httpx.AsyncClient, admin: Any) -> str:
    today = date.today()
    res = await client.post(
        "/v1/events",
        json={
            "name": "Event",
            "start_date": (today - timedelta(days=2)).isoformat(),
            "end_date": (today + timedelta(days=2)).isoformat(),
            "timezone": "UTC",
        },
        headers=admin.headers,
    )
    return res.json()["data"]["id"]


async def _session(
    client: httpx.AsyncClient, admin: Any, event_id: str | None = None, **overrides: Any
) -> httpx.Response:
    body = {
        "name": "  Friday  ",
        "divisions": [
            {"division_name": "Classic", "is_priority": True, "priority_run_limit": 2},
            {"division_name": "Showcase"},
            {"division_name": "Other"},
            {"division_name": "  "},
        ],
        **_times(),
        **overrides,
    }
    if event_id:
        body["event_id"] = event_id
    return await client.post("/v1/sessions", json=body, headers=admin.headers)


async def test_create_returns_session_with_divisions(
    client: httpx.AsyncClient, person: Callable
) -> None:
    admin = await person("admin", admin=True)
    res = await _session(client, admin)

    assert res.status_code == 201
    data = res.json()["data"]
    assert data["name"] == "Friday"
    assert data["status"] == "in_progress"  # derived from the clock
    assert (data["active_priority_max"], data["active_non_priority_max"]) == (6, 4)
    assert data["queue_depth"] == {"priority": 0, "non_priority": 0, "active": 0}
    assert [
        (d["division_name"], d["is_priority"], d["sort_order"], d["priority_run_limit"])
        for d in data["divisions"]
    ] == [("Classic", True, 0, 2), ("Showcase", False, 1, 0)]
    assert "event_name" not in data and "has_active_checkin" not in data


async def test_create_rules(client: httpx.AsyncClient, person: Callable) -> None:
    admin = await person("admin", admin=True)
    t = _times()
    cases = [
        ({"checkin_opens_at": 0}, "Missing or invalid required fields"),
        (
            {"floor_trial_starts_at": t["checkin_opens_at"]},
            "floor_trial_starts_at must be after checkin_opens_at",
        ),
        (
            {"floor_trial_ends_at": t["floor_trial_starts_at"]},
            "floor_trial_ends_at must be after floor_trial_starts_at",
        ),
        (
            {"active_priority_max": 2, "active_non_priority_max": 3},
            "active_non_priority_max must be <= active_priority_max",
        ),
    ]
    for overrides, message in cases:
        res = await _session(client, admin, **overrides)
        assert res.status_code == 400
        assert res.json()["error"]["code"] == "BAD_REQUEST"
        assert res.json()["error"]["message"].startswith(message)

    bad = await _session(client, admin, checkin_opens_at="1")  # zod does not coerce
    assert bad.json()["error"]["code"] == "VALIDATION_ERROR"


async def test_event_rules(client: httpx.AsyncClient, person: Callable) -> None:
    admin = await person("admin", admin=True)
    event_id = await _event(client, admin)
    assert (await _session(client, admin, event_id)).status_code == 201

    overlap = await _session(client, admin, event_id)
    assert overlap.json()["error"]["message"] == (
        "Session floor-trial window overlaps another session in this event"
    )
    late = await _session(client, admin, event_id, **_times(offset_hours=24 * 10))
    assert late.json()["error"]["message"].startswith("Session ends (")
    unknown = await _session(client, admin, "no-such-event")
    assert unknown.json()["error"] == {
        "code": "BAD_REQUEST",
        "message": "Event not found",
    }


async def test_reads_are_public_and_add_caller_fields_when_synced(
    client: httpx.AsyncClient, person: Callable, db: asyncpg.Connection
) -> None:
    admin = await person("admin", admin=True)
    dancer = await person("dancer")
    event_id = await _event(client, admin)
    session = (await _session(client, admin, event_id)).json()["data"]

    anon = (await client.get(f"/v1/sessions/{session['id']}")).json()["data"]
    assert "has_active_checkin" not in anon
    assert anon["event_name"] == "Event" and anon["event_timezone"] == "UTC"
    listed = (await client.get("/v1/sessions", params={"event_id": event_id})).json()
    assert listed["meta"]["count"] == 1
    assert "has_active_checkin" not in listed["data"][0]
    assert listed["data"][0]["event_timezone"] == "UTC"

    # An invalid token is anonymous, not an error.
    bad = await client.get("/v1/sessions", headers={"Authorization": "Bearer junk"})
    assert bad.status_code == 200 and "has_active_checkin" not in bad.json()["data"][0]

    mine = (
        await client.get(f"/v1/sessions/{session['id']}", headers=dancer.headers)
    ).json()
    assert mine["data"]["has_active_checkin"] is False
    assert "active_checkin_division" not in mine["data"]

    await db.execute(
        "INSERT INTO songs (id, user_id, created_at, updated_at) VALUES ('s1', $1, 1, 1)",
        dancer.id,
    )
    await db.execute(
        "INSERT INTO checkins (id, session_id, division_name, entity_solo_user_id, song_id,"
        " submitted_by_user_id, initial_queue, created_at)"
        " VALUES ('c1', $1, 'Classic', $2, 's1', $2, 'priority', 1)",
        session["id"],
        dancer.id,
    )
    await db.execute(
        "INSERT INTO queue_entries (id, checkin_id, session_id, queue_type, position,"
        " entered_queue_at, entity_solo_user_id) VALUES ('q1', 'c1', $1, 'priority', 1, 1, $2)",
        session["id"],
        dancer.id,
    )

    mine = (
        await client.get(f"/v1/sessions/{session['id']}", headers=dancer.headers)
    ).json()
    assert mine["data"]["has_active_checkin"] is True
    assert mine["data"]["active_checkin_division"] == "Classic"
    listed = (await client.get("/v1/sessions", headers=dancer.headers)).json()
    assert listed["data"][0]["has_active_checkin"] is True


async def test_writes_need_the_sessions_scope(
    client: httpx.AsyncClient, person: Callable
) -> None:
    dancer = await person("dancer")
    res = await client.post("/v1/sessions", json={}, headers=dancer.headers)
    assert res.status_code == 403


async def test_put_divisions_upserts_by_name(
    client: httpx.AsyncClient, person: Callable
) -> None:
    admin = await person("admin", admin=True)
    session = (await _session(client, admin)).json()["data"]
    classic_id = session["divisions"][0]["id"]

    res = await client.put(
        f"/v1/sessions/{session['id']}/divisions",
        json={
            "divisions": [
                {"division_name": "Classic", "is_priority": False, "sort_order": 5},
                {"division_name": "Masters", "is_priority": True},
            ]
        },
        headers=admin.headers,
    )

    divisions = {d["division_name"]: d for d in res.json()["data"]["divisions"]}
    assert divisions["Classic"]["id"] == classic_id  # kept its row
    assert divisions["Classic"]["is_priority"] is False
    assert set(divisions) == {"Classic", "Showcase", "Masters"}


async def test_status_patch_and_cache_invalidation(
    client: httpx.AsyncClient, person: Callable
) -> None:
    admin = await person("admin", admin=True)
    session = (await _session(client, admin)).json()["data"]
    url = f"/v1/sessions/{session['id']}"
    assert (await client.get(url)).json()["data"][
        "status"
    ] == "in_progress"  # cached now

    res = await client.patch(
        f"{url}/status", json={"status": "cancelled"}, headers=admin.headers
    )
    assert res.json()["data"]["status"] == "cancelled"
    assert (await client.get(url)).json()["data"]["status"] == "cancelled"

    bad = await client.patch(
        f"{url}/status", json={"status": "paused"}, headers=admin.headers
    )
    assert bad.json()["error"]["code"] == "VALIDATION_ERROR"


async def test_patch_checks_the_result_as_a_whole(
    client: httpx.AsyncClient, person: Callable
) -> None:
    admin = await person("admin", admin=True)
    session = (await _session(client, admin, date="2026-05-01")).json()["data"]
    url = f"/v1/sessions/{session['id']}"

    res = await client.patch(
        url, json={"name": " Saturday ", "date": None}, headers=admin.headers
    )
    assert (res.json()["data"]["name"], res.json()["data"]["date"]) == (
        "Saturday",
        None,
    )

    res = await client.patch(
        url,
        json={"floor_trial_ends_at": session["floor_trial_starts_at"]},
        headers=admin.headers,
    )
    assert res.status_code == 400
    res = await client.patch(
        url, json={"active_non_priority_max": 9}, headers=admin.headers
    )
    assert res.json()["error"]["message"] == (
        "active_non_priority_max must be <= active_priority_max"
    )
    res = await client.patch(url, json={"name": None}, headers=admin.headers)
    assert (
        res.json()["error"]["code"] == "VALIDATION_ERROR"
    )  # only date and event_id are nullable


async def test_delete_cascades(
    client: httpx.AsyncClient, person: Callable, db: asyncpg.Connection
) -> None:
    admin = await person("admin", admin=True)
    session = (await _session(client, admin)).json()["data"]

    res = await client.delete(f"/v1/sessions/{session['id']}", headers=admin.headers)

    assert res.json()["data"] == {"deleted": True}
    assert await db.fetchval("SELECT count(*) FROM session_divisions") == 0
    missing = await client.get(f"/v1/sessions/{session['id']}")
    assert missing.json()["error"] == {
        "code": "NOT_FOUND",
        "message": "Session not found",
    }
