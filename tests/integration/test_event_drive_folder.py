"""/v1/events/{id}/drive-folder: the event's shared Drive folder, for the
people it is shared with (its DJs and event managers) and admins."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import asyncpg
import httpx

EVENT = {"name": "Swing Fling", "start_date": "2026-05-01", "end_date": "2026-05-03"}


def _url(event_id: str) -> str:
    return f"/v1/events/{event_id}/drive-folder"


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


async def _share_job(
    db: asyncpg.Connection,
    job_id: str,
    event_id: str,
    *,
    file_id: str | None,
    status: str = "done",
    updated_at: int = 1,
    kind: str = "share_event_folder",
) -> None:
    await db.execute(
        "INSERT INTO drive_jobs (id, kind, file_id, event_id, user_id, status,"
        " next_attempt_at, created_at, updated_at)"
        " VALUES ($1, $2, $3, $4, 'u_someone', $5, 0, 1, $6)",
        job_id,
        kind,
        file_id,
        event_id,
        status,
        updated_at,
    )


async def test_a_dj_gets_the_newest_completed_share(
    client: httpx.AsyncClient, person: Callable, db: asyncpg.Connection
) -> None:
    admin = await person("root", admin=True)
    dj = await person("dj")
    event = await _event(client, admin, "Fling")
    await _grant(client, admin, event, dj.id, "dj")
    # The grant's own share is still pending: it has recorded no folder.
    await _share_job(db, "j_old", event, file_id="folder_old", updated_at=10)
    await _share_job(db, "j_new", event, file_id="folder_new", updated_at=20)
    await _share_job(
        db, "j_failed", event, file_id="folder_x", status="failed", updated_at=30
    )
    await _share_job(
        db,
        "j_unshare",
        event,
        file_id="folder_y",
        kind="unshare_event_folder",
        updated_at=40,
    )

    res = await client.get(_url(event), headers=dj.headers)

    assert res.status_code == 200, res.text
    assert res.json() == {
        "data": {
            "folder_id": "folder_new",
            "url": "https://drive.google.com/drive/folders/folder_new",
        },
        "meta": {"version": "v1"},
    }


async def test_no_recorded_share_answers_nulls(
    client: httpx.AsyncClient, person: Callable
) -> None:
    admin = await person("root", admin=True)
    dj = await person("dj")
    event = await _event(client, admin, "Fling")
    await _grant(client, admin, event, dj.id, "dj")

    res = await client.get(_url(event), headers=dj.headers)

    assert res.status_code == 200, res.text
    assert res.json()["data"] == {"folder_id": None, "url": None}


async def test_an_event_manager_and_an_admin_read_it(
    client: httpx.AsyncClient, person: Callable, db: asyncpg.Connection
) -> None:
    admin = await person("root", admin=True)
    manager = await person("em")
    event = await _event(client, admin, "Fling")
    await _grant(client, admin, event, manager.id, "event-manager")
    await _share_job(db, "j1", event, file_id="folder_1")

    for who in (manager, admin):
        res = await client.get(_url(event), headers=who.headers)
        assert res.status_code == 200, res.text
        assert res.json()["data"]["folder_id"] == "folder_1"


async def test_only_the_events_djs_and_managers_are_allowed(
    client: httpx.AsyncClient, person: Callable, db: asyncpg.Connection
) -> None:
    admin = await person("root", admin=True)
    coordinator = await person("coord")
    other_dj = await person("otherdj")
    dancer = await person("dancer")
    event = await _event(client, admin, "Fling")
    other = await _event(client, admin, "Other")
    await _grant(client, admin, event, coordinator.id, "coordinator")
    await _grant(client, admin, other, other_dj.id, "dj")
    await _share_job(db, "j1", event, file_id="folder_1")

    for who in (coordinator, other_dj, dancer):
        res = await client.get(_url(event), headers=who.headers)
        assert res.status_code == 403, res.text
        assert "folder_1" not in res.text
    assert (await client.get(_url(event))).status_code == 401


async def test_an_unknown_event_is_404(
    client: httpx.AsyncClient, person: Callable
) -> None:
    admin = await person("root", admin=True)

    res = await client.get(_url("evt_missing"), headers=admin.headers)

    assert res.status_code == 404, res.text
    assert res.json()["error"]["code"] == "NOT_FOUND"
