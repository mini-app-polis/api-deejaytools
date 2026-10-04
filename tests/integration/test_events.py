"""/v1/events: public reads, the entity roster, and admin writes."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, timedelta
from typing import Any

import asyncpg
import httpx

from api_deejaytools.routers.events import compute_status

EVENT = {"name": "Swing Fling", "start_date": "2026-05-01", "end_date": "2026-05-03"}


async def _create(client: httpx.AsyncClient, admin: Any, **overrides: Any) -> dict:
    res = await client.post(
        "/v1/events", json={**EVENT, **overrides}, headers=admin.headers
    )
    assert res.status_code == 201, res.text
    return res.json()["data"]


async def test_create_and_read_back(
    client: httpx.AsyncClient, person: Callable
) -> None:
    admin = await person("admin", admin=True)
    created = await _create(client, admin)

    assert set(created) == {
        "id",
        "name",
        "start_date",
        "end_date",
        "timezone",
        "season_year",
        "status",
        "created_by",
        "created_at",
        "updated_at",
    }
    assert created["timezone"] == "America/Chicago"
    assert created["season_year"] == "2026"
    assert created["created_by"] == admin.id

    one = await client.get(f"/v1/events/{created['id']}")  # public: no token
    assert one.status_code == 200
    assert one.json() == {"data": created, "meta": {"version": "v1"}}
    listed = (await client.get("/v1/events")).json()
    assert listed["meta"] == {"version": "v1", "count": 1}


async def test_season_year_rolls_over_in_october(
    client: httpx.AsyncClient, person: Callable
) -> None:
    admin = await person("admin", admin=True)
    created = await _create(
        client, admin, start_date="2026-10-01", end_date="2026-10-02"
    )
    assert created["season_year"] == "2027"


async def test_list_is_newest_start_first(
    client: httpx.AsyncClient, person: Callable
) -> None:
    admin = await person("admin", admin=True)
    await _create(
        client, admin, name="Old", start_date="2025-01-01", end_date="2025-01-02"
    )
    await _create(
        client, admin, name="New", start_date="2026-01-01", end_date="2026-01-02"
    )
    names = [e["name"] for e in (await client.get("/v1/events")).json()["data"]]
    assert names == ["New", "Old"]


async def test_writes_need_the_events_scope(
    client: httpx.AsyncClient, person: Callable
) -> None:
    dancer = await person("dancer")
    assert (await client.post("/v1/events", json=EVENT)).status_code == 401
    res = await client.post("/v1/events", json=EVENT, headers=dancer.headers)
    assert res.status_code == 403
    assert res.json()["error"] == {
        "code": "FORBIDDEN",
        "message": "Admin access required",
    }


async def test_validation(client: httpx.AsyncClient, person: Callable) -> None:
    admin = await person("admin", admin=True)
    for body in (
        {**EVENT, "start_date": "May 1"},
        {**EVENT, "timezone": "Mars/Olympus"},
        {**EVENT, "season_year": "26"},
        {**EVENT, "name": ""},
        {**EVENT, "season_year": None},
    ):
        res = await client.post("/v1/events", json=body, headers=admin.headers)
        assert res.status_code == 400, body
        assert res.json()["error"]["code"] == "VALIDATION_ERROR"

    res = await client.post(
        "/v1/events",
        json={**EVENT, "start_date": "2026-05-04"},
        headers=admin.headers,
    )
    assert res.json()["error"] == {
        "code": "BAD_REQUEST",
        "message": "start_date must be on or before end_date",
    }


async def test_timezone_is_matched_like_intl(
    client: httpx.AsyncClient, person: Callable
) -> None:
    """Intl accepts any casing and keeps the value as sent."""
    admin = await person("admin", admin=True)
    created = await _create(client, admin, timezone="america/new_york")
    assert created["timezone"] == "america/new_york"


async def test_patch(client: httpx.AsyncClient, person: Callable) -> None:
    admin = await person("admin", admin=True)
    created = await _create(client, admin)

    res = await client.patch(
        f"/v1/events/{created['id']}",
        json={"name": "Renamed", "start_date": "2026-12-01", "end_date": "2026-12-02"},
        headers=admin.headers,
    )
    data = res.json()["data"]
    assert (data["name"], data["start_date"]) == ("Renamed", "2026-12-01")
    assert data["season_year"] == "2026"  # never recomputed from a moved start

    res = await client.patch(
        f"/v1/events/{created['id']}",
        json={"end_date": "2026-11-01"},
        headers=admin.headers,
    )
    assert res.status_code == 400
    missing = await client.patch("/v1/events/nope", json={}, headers=admin.headers)
    assert missing.json()["error"] == {
        "code": "NOT_FOUND",
        "message": "Event not found",
    }


def test_status_uses_the_event_timezone() -> None:
    today = date.today()
    far = (today + timedelta(days=30)).isoformat()
    past = (today - timedelta(days=30)).isoformat()
    assert compute_status(far, far, "America/Chicago") == "upcoming"
    assert compute_status(past, past, "America/Chicago") == "completed"
    assert compute_status(past, far, "Pacific/Kiritimati") == "active"
    assert compute_status(past, far, "not/a-zone") == "active"  # falls back to UTC


async def _seed_entries(db: asyncpg.Connection, event_id: str, owner: str) -> None:
    """Two Classic songs for one partner, one Showcase song solo, and a managed
    couple with no division anywhere."""
    await db.execute(
        "INSERT INTO partners (id, user_id, first_name, last_name, created_at, updated_at) "
        "VALUES ('p1', $1, 'Bo', 'Partner', 1, 1)",
        owner,
    )
    await db.execute(
        "INSERT INTO managed_partnerships (id, user_id, leader_first_name, leader_last_name,"
        " follower_first_name, follower_last_name, created_at, updated_at) "
        "VALUES ('m1', $1, 'Lee', 'Lead', 'Fay', 'Follow', 1, 1)",
        owner,
    )
    songs = [
        ("s1", "p1", None, "Classic"),
        ("s2", "p1", None, "Classic"),
        ("s3", None, None, "Showcase"),
        ("s4", None, "m1", None),
    ]
    for sid, partner, managed, division in songs:
        await db.execute(
            "INSERT INTO songs (id, user_id, partner_id, managed_partnership_id, division,"
            " created_at, updated_at) VALUES ($1, $2, $3, $4, $5, 1, 1)",
            sid,
            owner,
            partner,
            managed,
            division,
        )
        await db.execute(
            "INSERT INTO event_song_submissions (id, event_id, song_id,"
            " submitted_by_user_id, created_at, drive_copy_file_id)"
            " VALUES ($1, $2, $3, $4, 1, $5)",
            f"sub_{sid}",
            event_id,
            sid,
            owner,
            f"copy_{sid}",
        )


async def test_entities_grouped_by_division(
    client: httpx.AsyncClient, person: Callable, db: asyncpg.Connection
) -> None:
    admin = await person("admin", admin=True)
    dancer = await person("dancer")
    event = await _create(client, admin)
    await _seed_entries(db, event["id"], dancer.id)

    assert (await client.get(f"/v1/events/{event['id']}/entities")).status_code == 401
    res = await client.get(f"/v1/events/{event['id']}/entities", headers=dancer.headers)

    assert res.status_code == 200
    body = res.json()
    assert body["meta"]["count"] == 3
    assert body["data"] == [
        {
            "division": "Classic",
            "entities": [
                {"entity_key": "pt:p1", "label": "Dancer & Bo Partner", "song_count": 2}
            ],
        },
        {
            "division": "Showcase",
            "entities": [
                {"entity_key": f"us:{dancer.id}", "label": "Dancer", "song_count": 1}
            ],
        },
        {
            "division": "Unspecified",
            "entities": [
                {
                    "entity_key": "mp:m1",
                    "label": "Lee Lead & Fay Follow",
                    "song_count": 1,
                }
            ],
        },
    ]


async def test_delete_cascades_and_queues_copies_for_trash(
    client: httpx.AsyncClient, person: Callable, db: asyncpg.Connection
) -> None:
    admin = await person("admin", admin=True)
    event = await _create(client, admin)
    await _seed_entries(db, event["id"], admin.id)
    await db.execute(
        "INSERT INTO sessions (id, event_id, name, checkin_opens_at, floor_trial_starts_at,"
        " floor_trial_ends_at, created_at) VALUES ('sess1', $1, 'S', 1, 2, 3, 1)",
        event["id"],
    )
    await db.execute(
        "INSERT INTO event_division_run_limits (event_id, division_name, priority_run_limit)"
        " VALUES ($1, 'Classic', 2)",
        event["id"],
    )

    res = await client.delete(f"/v1/events/{event['id']}", headers=admin.headers)

    assert res.json() == {"data": {"deleted": True}, "meta": {"version": "v1"}}
    for table in (
        "events",
        "sessions",
        "event_song_submissions",
        "event_division_run_limits",
    ):
        assert await db.fetchval(f"SELECT count(*) FROM {table}") == 0, table
    jobs = await db.fetch(
        "SELECT kind, file_id, status FROM drive_jobs ORDER BY file_id"
    )
    assert [(j["kind"], j["file_id"], j["status"]) for j in jobs] == [
        ("trash", f"copy_s{i}", "pending") for i in range(1, 5)
    ]
