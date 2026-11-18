"""/v1/events/{id}/submission-timeline, and the events.series it compares by."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import asyncpg
import httpx

from .content_helpers import insert_song

CHICAGO = ZoneInfo("America/Chicago")


def _ms(local: str, zone: ZoneInfo = CHICAGO) -> int:
    """Epoch ms of a local YYYY-MM-DDTHH:MM in ``zone``."""
    return int(datetime.fromisoformat(local).replace(tzinfo=zone).timestamp() * 1000)


def _url(event_id: str) -> str:
    return f"/v1/events/{event_id}/submission-timeline"


async def _event(
    client: httpx.AsyncClient, admin: Any, name: str, start: str, **extra: Any
) -> str:
    res = await client.post(
        "/v1/events",
        json={"name": name, "start_date": start, "end_date": start, **extra},
        headers=admin.headers,
    )
    assert res.status_code == 201, res.text
    event_id: str = res.json()["data"]["id"]
    return event_id


async def _submit(
    db: asyncpg.Connection,
    event_id: str,
    user_id: str,
    created_at: int,
    *,
    song_added_at: int | None = None,
) -> None:
    """A submission made at ``created_at`` of a song added at ``song_added_at``
    (the same moment when not given)."""
    song = await insert_song(
        db,
        user_id,
        division="Classic",
        season_year="2026",
        created_at=song_added_at if song_added_at is not None else created_at,
    )
    await db.execute(
        "INSERT INTO event_song_submissions (id, event_id, song_id,"
        " submitted_by_user_id, created_at) VALUES ($1, $2, $3, $4, $5)",
        f"sub_{song}",
        event_id,
        song,
        user_id,
        created_at,
    )


async def test_series_is_set_trimmed_changed_and_cleared(
    client: httpx.AsyncClient, person: Callable
) -> None:
    admin = await person("admin", admin=True)
    plain = await _event(client, admin, "Plain", "2026-05-01")
    res = await client.get(f"/v1/events/{plain}")
    assert res.json()["data"]["series"] is None

    event_id = await _event(client, admin, "Open", "2026-11-25", series="  The Open ")
    res = await client.get(f"/v1/events/{event_id}")
    assert res.json()["data"]["series"] == "The Open"

    res = await client.patch(
        f"/v1/events/{event_id}", json={"name": "Renamed"}, headers=admin.headers
    )
    assert res.json()["data"]["series"] == "The Open"
    res = await client.patch(
        f"/v1/events/{event_id}", json={"series": "Open"}, headers=admin.headers
    )
    assert res.json()["data"]["series"] == "Open"
    res = await client.patch(
        f"/v1/events/{event_id}", json={"series": ""}, headers=admin.headers
    )
    assert res.json()["data"]["series"] is None

    res = await client.patch(
        f"/v1/events/{event_id}", json={"series": "x" * 101}, headers=admin.headers
    )
    assert res.status_code == 400


async def test_counts_by_day_from_start_beside_earlier_editions(
    client: httpx.AsyncClient, person: Callable, db: asyncpg.Connection
) -> None:
    admin = await person("admin", admin=True)
    alice = await person("alice")
    this_year = await _event(
        client, admin, "Open 2026", "2026-11-25", series="The Open"
    )
    last_year = await _event(
        client,
        admin,
        "Open 2025",
        "2025-11-26",
        series="the open",
        timezone="America/Los_Angeles",
    )
    older = await _event(client, admin, "Open 2024", "2024-11-27", series="The Open")
    # Later editions, other series and events in none are left out.
    await _event(client, admin, "Open 2027", "2027-11-24", series="The Open")
    await _event(client, admin, "Other", "2025-11-01", series="Swing Fling")
    await _event(client, admin, "Loose", "2025-11-02")

    await _submit(db, this_year, alice.id, _ms("2026-11-17T09:00"))
    # Uploaded weeks before it was submitted.
    await _submit(
        db,
        this_year,
        alice.id,
        _ms("2026-11-17T23:30"),
        song_added_at=_ms("2026-10-01T12:00"),
    )
    # Uploaded the day before, submitted at the event.
    await _submit(
        db,
        this_year,
        alice.id,
        _ms("2026-11-25T08:00"),
        song_added_at=_ms("2026-11-24T20:00"),
    )
    # 23:30 in Los Angeles is the next day in Chicago: the event's own zone counts.
    la = ZoneInfo("America/Los_Angeles")
    await _submit(db, last_year, alice.id, _ms("2025-11-18T23:30", la))
    await _submit(db, last_year, alice.id, _ms("2025-11-27T10:00", la))
    res = await client.put(
        f"/v1/admin/events/{last_year}/registrations",
        json={
            "entries": [
                {"division": "Classic", "dancer_1": "Ada A", "dancer_2": "Bob B"},
                {"division": "Classic", "dancer_1": "Cy C", "dancer_2": "Di D"},
                {"division": "Classic", "dancer_1": "Ed E", "dancer_2": "Flo F"},
            ]
        },
        headers=admin.headers,
    )
    assert res.status_code == 200, res.text
    scratched = res.json()["data"][2]["id"]
    res = await client.patch(
        f"/v1/admin/event-registrations/{scratched}",
        json={"scratched": True},
        headers=admin.headers,
    )
    assert res.status_code == 200, res.text

    res = await client.get(_url(this_year), headers=admin.headers)
    assert res.status_code == 200, res.text
    data = res.json()["data"]
    assert data["series"] == "The Open"
    assert isinstance(data["today"], int)
    assert [e["id"] for e in data["events"]] == [this_year, last_year, older]
    this, last, old = data["events"]
    assert this["days"] == [{"day": -8, "count": 2}, {"day": 0, "count": 1}]
    assert this["song_added_days"] == [
        {"day": -55, "count": 1},
        {"day": -8, "count": 1},
        {"day": -1, "count": 1},
    ]
    assert (this["submissions"], this["registrations"]) == (3, 0)
    assert last["days"] == [{"day": -8, "count": 1}, {"day": 1, "count": 1}]
    # Added the moment they were submitted, in the event's own zone too.
    assert last["song_added_days"] == last["days"]
    assert (last["submissions"], last["registrations"]) == (2, 2)
    assert old == {
        "id": older,
        "name": "Open 2024",
        "start_date": "2024-11-27",
        "end_date": "2024-11-27",
        "registrations": 0,
        "submissions": 0,
        "days": [],
        "song_added_days": [],
    }


async def test_an_event_in_no_series_is_alone(
    client: httpx.AsyncClient, person: Callable
) -> None:
    admin = await person("admin", admin=True)
    event_id = await _event(client, admin, "Loose", "2026-05-01")
    await _event(client, admin, "Earlier", "2025-05-01")

    res = await client.get(_url(event_id), headers=admin.headers)
    data = res.json()["data"]
    assert data["series"] is None
    assert [e["id"] for e in data["events"]] == [event_id]


async def test_the_events_staff_see_it_and_others_do_not(
    client: httpx.AsyncClient, person: Callable
) -> None:
    admin = await person("admin", admin=True)
    alice = await person("alice")
    dj = await person("dj")
    event_id = await _event(client, admin, "Open", "2026-11-25", series="The Open")
    other = await _event(client, admin, "Other", "2026-12-01")
    res = await client.put(
        f"/v1/events/{event_id}/staff/{dj.id}/dj", headers=admin.headers
    )
    assert res.status_code == 200, res.text

    assert (await client.get(_url(event_id), headers=dj.headers)).status_code == 200
    assert (await client.get(_url(other), headers=dj.headers)).status_code == 403
    assert (await client.get(_url(event_id), headers=alice.headers)).status_code == 403
    assert (await client.get(_url(event_id))).status_code == 401
    res = await client.get(_url("evt_none"), headers=admin.headers)
    assert res.status_code == 404
