"""The DJ's Drive folder (ADR-0012): share and unshare jobs for event staff.

Granting DJ or event manager queues sharing the event's folder with the
person; revoking their last such role, or deleting the event, queues the
unshare; an event change that moves the folder queues a re-share. The jobs
run on the real Drive layer over the in-memory fake Google service: never
real Drive.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable, Iterator
from typing import Any

import asyncpg
import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from api_deejaytools.config import get_settings
from api_deejaytools.database import _get_sessionmaker
from api_deejaytools.services import drive, drive_jobs

from ..drive_fakes import FakeDriveService, fake_facade
from .conftest import TEST_DATABASE_URL

EVENT = {
    "name": "Swing Fling",
    "start_date": "2026-05-01",
    "end_date": "2026-05-03",
    "season_year": "2026",
}
SHARE = "share_event_folder"
UNSHARE = "unshare_event_folder"


@pytest.fixture
def real_drive(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeDriveService]:
    """The real Drive layer over the in-memory fake Google service."""
    settings = get_settings()
    monkeypatch.setattr(
        settings,
        "GOOGLE_CREDENTIALS_JSON",
        json.dumps({"client_email": "sa@x.iam", "private_key": "KEY"}),
    )
    monkeypatch.setattr(settings, "GOOGLE_DRIVE_PARENT_FOLDER_ID", "root")
    facade, svc = fake_facade()
    monkeypatch.setattr(drive, "_build_facade", lambda email, key: facade)
    drive.clear_drive_folder_cache()
    yield svc
    drive.clear_drive_folder_cache()


@pytest.fixture
async def session(db: asyncpg.Connection) -> AsyncIterator[AsyncSession]:
    async with _get_sessionmaker(TEST_DATABASE_URL)() as s:
        yield s


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
    client: httpx.AsyncClient, admin: Any, event_id: str, user_id: str, role: str
) -> None:
    res = await client.put(_url(event_id, user_id, role), headers=admin.headers)
    assert res.status_code == 200, res.text


async def _revoke(
    client: httpx.AsyncClient, admin: Any, event_id: str, user_id: str, role: str
) -> None:
    res = await client.delete(_url(event_id, user_id, role), headers=admin.headers)
    assert res.status_code == 200, res.text


async def _jobs(db: asyncpg.Connection) -> list[tuple[str, str, str, str]]:
    """Every queued folder job, as (kind, event_id, user_id, status), oldest first."""
    rows = await db.fetch(
        "SELECT kind, event_id, user_id, status FROM drive_jobs"
        " ORDER BY created_at, ctid"
    )
    return [(r["kind"], r["event_id"], r["user_id"], r["status"]) for r in rows]


async def _email(db: asyncpg.Connection, user_id: str) -> str:
    email: str = await db.fetchval("SELECT email FROM users WHERE id = $1", user_id)
    return email.lower()


def _readers(svc: FakeDriveService, folder_id: str) -> list[str]:
    return [
        p["emailAddress"]
        for p in svc.permissions_by_file.get(folder_id, [])
        if p["role"] == "reader"
    ]


# --- what is queued ----------------------------------------------------------------


async def test_granting_dj_or_event_manager_queues_one_share(
    client: httpx.AsyncClient, person: Callable[..., Any], db: asyncpg.Connection
) -> None:
    admin = await person("admin", admin=True)
    dj = await person("dj")
    carol = await person("carol")
    event_id = await _event(client, admin)

    await _grant(client, admin, event_id, dj.id, "dj")
    assert await _jobs(db) == [(SHARE, event_id, dj.id, "pending")]
    # Granting it again, or a second folder role, shares nothing new.
    await _grant(client, admin, event_id, dj.id, "dj")
    await _grant(client, admin, event_id, dj.id, "event-manager")
    # A coordinator gets no folder.
    await _grant(client, admin, event_id, carol.id, "coordinator")
    assert await _jobs(db) == [(SHARE, event_id, dj.id, "pending")]

    manager = await person("manager")
    await _grant(client, admin, event_id, manager.id, "event-manager")
    assert (await _jobs(db))[-1] == (SHARE, event_id, manager.id, "pending")


async def test_only_revoking_the_last_folder_role_queues_the_unshare(
    client: httpx.AsyncClient, person: Callable[..., Any], db: asyncpg.Connection
) -> None:
    admin = await person("admin", admin=True)
    dj = await person("dj")
    event_id = await _event(client, admin)
    await _grant(client, admin, event_id, dj.id, "dj")
    await _grant(client, admin, event_id, dj.id, "event-manager")
    await _grant(client, admin, event_id, dj.id, "coordinator")

    # One of two folder roles: they still read the folder.
    await _revoke(client, admin, event_id, dj.id, "dj")
    assert [j[0] for j in await _jobs(db)] == [SHARE]
    await _revoke(client, admin, event_id, dj.id, "event-manager")
    assert (await _jobs(db))[-1] == (UNSHARE, event_id, dj.id, "pending")
    # Revoking what is not held, or a coordinator, queues nothing more.
    await _revoke(client, admin, event_id, dj.id, "dj")
    await _revoke(client, admin, event_id, dj.id, "coordinator")
    assert [j[0] for j in await _jobs(db)] == [SHARE, UNSHARE]


async def test_an_event_change_that_moves_the_folder_queues_reshares(
    client: httpx.AsyncClient, person: Callable[..., Any], db: asyncpg.Connection
) -> None:
    admin = await person("admin", admin=True)
    dj = await person("dj")
    manager = await person("manager")
    carol = await person("carol")
    event_id = await _event(client, admin)
    await _grant(client, admin, event_id, dj.id, "dj")
    await _grant(client, admin, event_id, dj.id, "event-manager")
    await _grant(client, admin, event_id, manager.id, "event-manager")
    await _grant(client, admin, event_id, carol.id, "coordinator")
    before = len(await _jobs(db))

    async def patch(body: dict[str, str]) -> list[tuple[str, str, str, str]]:
        res = await client.patch(
            f"/v1/events/{event_id}", json=body, headers=admin.headers
        )
        assert res.status_code == 200, res.text
        return (await _jobs(db))[before:]

    # Not the folder: the dates, or the same name again.
    assert await patch({"end_date": "2026-05-04"}) == []
    assert await patch({"name": "A"}) == []
    # The name: one share per DJ or event manager, on the new folder.
    renamed = await patch({"name": "A Renamed"})
    assert sorted(renamed) == sorted(
        [
            (SHARE, event_id, dj.id, "pending"),
            (SHARE, event_id, manager.id, "pending"),
        ]
    )
    before += len(renamed)
    # The season moves it too.
    assert len(await patch({"season_year": "2027"})) == 2


async def test_deleting_the_event_queues_the_unshares(
    client: httpx.AsyncClient, person: Callable[..., Any], db: asyncpg.Connection
) -> None:
    admin = await person("admin", admin=True)
    dj = await person("dj")
    carol = await person("carol")
    event_id = await _event(client, admin)
    await _grant(client, admin, event_id, dj.id, "dj")
    await _grant(client, admin, event_id, carol.id, "coordinator")

    res = await client.delete(f"/v1/events/{event_id}", headers=admin.headers)
    assert res.status_code == 200
    assert await _jobs(db) == [
        (SHARE, event_id, dj.id, "pending"),
        (UNSHARE, event_id, dj.id, "pending"),
    ]


async def test_folder_jobs_show_in_the_drive_jobs_view(
    client: httpx.AsyncClient, person: Callable[..., Any], db: asyncpg.Connection
) -> None:
    admin = await person("admin", admin=True)
    dj = await person("dj")
    event_id = await _event(client, admin, "Shown")
    await _grant(client, admin, event_id, dj.id, "dj")

    res = await client.get("/v1/admin/drive-jobs", headers=admin.headers)
    (job,) = res.json()["data"]
    assert {k: job[k] for k in ("kind", "status", "event_id", "user_id")} == {
        "kind": SHARE,
        "status": "pending",
        "event_id": event_id,
        "user_id": dj.id,
    }
    assert job["event_name"] == "Shown"
    summary = await client.get("/v1/admin/drive-jobs/summary", headers=admin.headers)
    assert summary.json()["data"]["by_status"] == {"pending": 1}


# --- what the jobs do --------------------------------------------------------------


async def test_share_creates_the_event_folder_and_shares_it(
    client: httpx.AsyncClient,
    person: Callable[..., Any],
    db: asyncpg.Connection,
    session: AsyncSession,
    real_drive: FakeDriveService,
) -> None:
    admin = await person("admin", admin=True)
    dj = await person("dj")
    event_id = await _event(client, admin, "Swing / Fling")
    await _grant(client, admin, event_id, dj.id, "dj")

    result = await drive_jobs.process_drive_jobs(session)
    assert result == {"claimed": 1, "succeeded": 1, "failed": 0}
    row = await db.fetchrow("SELECT status, file_id FROM drive_jobs")
    assert row["status"] == "done"
    folder_id = row["file_id"]
    assert real_drive.folder_path(folder_id) == ["2026", "Events", "Swing - Fling"]
    assert _readers(real_drive, folder_id) == [await _email(db, dj.id)]
    (perm,) = real_drive.ops("permissions.create")
    assert perm["sendNotificationEmail"] is False


async def test_unshare_takes_access_from_every_folder_the_event_had(
    client: httpx.AsyncClient,
    person: Callable[..., Any],
    db: asyncpg.Connection,
    session: AsyncSession,
    real_drive: FakeDriveService,
) -> None:
    admin = await person("admin", admin=True)
    dj = await person("dj")
    manager = await person("manager")
    event_id = await _event(client, admin)
    await _grant(client, admin, event_id, dj.id, "dj")
    await _grant(client, admin, event_id, manager.id, "event-manager")
    await drive_jobs.process_drive_jobs(session)
    # Renamed: the copies before it stay in the old folder, so both are shared.
    res = await client.patch(
        f"/v1/events/{event_id}", json={"name": "B"}, headers=admin.headers
    )
    assert res.status_code == 200
    await drive_jobs.process_drive_jobs(session)
    folders = sorted(
        await db.fetchval(
            "SELECT array_agg(DISTINCT file_id) FROM drive_jobs WHERE user_id = $1",
            dj.id,
        )
    )
    assert len(folders) == 2
    dj_email = await _email(db, dj.id)
    for folder_id in folders:
        assert dj_email in _readers(real_drive, folder_id)

    await _revoke(client, admin, event_id, dj.id, "dj")
    result = await drive_jobs.process_drive_jobs(session)
    assert result == {"claimed": 1, "succeeded": 1, "failed": 0}
    manager_email = await _email(db, manager.id)
    for folder_id in folders:
        # The DJ is gone from both; the event manager keeps theirs.
        assert _readers(real_drive, folder_id) == [manager_email]


async def test_a_folder_reached_through_another_event_is_kept(
    client: httpx.AsyncClient,
    person: Callable[..., Any],
    db: asyncpg.Connection,
    session: AsyncSession,
    real_drive: FakeDriveService,
) -> None:
    admin = await person("admin", admin=True)
    dj = await person("dj")
    # Two events with one name and season share one folder.
    first = await _event(client, admin, "Same")
    second = await _event(client, admin, "Same")
    await _grant(client, admin, first, dj.id, "dj")
    await _grant(client, admin, second, dj.id, "dj")
    await drive_jobs.process_drive_jobs(session)
    (folder_id,) = set(
        await db.fetchval(
            "SELECT array_agg(file_id) FROM drive_jobs WHERE kind = $1", SHARE
        )
    )

    await _revoke(client, admin, first, dj.id, "dj")
    await drive_jobs.process_drive_jobs(session)
    assert real_drive.ops("permissions.delete") == []
    assert await _email(db, dj.id) in _readers(real_drive, folder_id)


async def test_jobs_follow_the_staff_list_when_they_run(
    client: httpx.AsyncClient,
    person: Callable[..., Any],
    db: asyncpg.Connection,
    session: AsyncSession,
    real_drive: FakeDriveService,
) -> None:
    admin = await person("admin", admin=True)
    dj = await person("dj")
    event_id = await _event(client, admin)
    # Granted and revoked before anything ran: the share finds no role and
    # shares nothing; the unshare finds nothing shared.
    await _grant(client, admin, event_id, dj.id, "dj")
    await _revoke(client, admin, event_id, dj.id, "dj")
    result = await drive_jobs.process_drive_jobs(session)
    assert result == {"claimed": 2, "succeeded": 2, "failed": 0}
    assert real_drive.ops("permissions.create") == []
    assert real_drive.ops("permissions.delete") == []

    # Revoked and granted again before the unshare ran: it leaves access be.
    await _grant(client, admin, event_id, dj.id, "dj")
    await drive_jobs.process_drive_jobs(session)
    await _revoke(client, admin, event_id, dj.id, "dj")
    await _grant(client, admin, event_id, dj.id, "dj")
    await drive_jobs.process_drive_jobs(session)
    assert real_drive.ops("permissions.delete") == []
    statuses = {r["status"] for r in await db.fetch("SELECT status FROM drive_jobs")}
    assert statuses == {"done"}


async def test_a_share_revoked_while_in_flight_queues_another_unshare(
    client: httpx.AsyncClient,
    person: Callable[..., Any],
    db: asyncpg.Connection,
    session: AsyncSession,
    real_drive: FakeDriveService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admin = await person("admin", admin=True)
    dj = await person("dj")
    event_id = await _event(client, admin)
    await _grant(client, admin, event_id, dj.id, "dj")
    share = drive.share_with_reader

    async def share_then_revoke(folder_id: str, email: str) -> Any:
        result = await share(folder_id, email)
        await db.execute("DELETE FROM event_staff_grants WHERE user_id = $1", dj.id)
        return result

    monkeypatch.setattr(drive, "share_with_reader", share_then_revoke)
    await drive_jobs.process_drive_jobs(session)
    assert [j[:3] for j in await _jobs(db)] == [
        (SHARE, event_id, dj.id),
        (UNSHARE, event_id, dj.id),
    ]
    monkeypatch.setattr(drive, "share_with_reader", share)
    await drive_jobs.process_drive_jobs(session)
    (perm,) = real_drive.ops("permissions.create")
    assert _readers(real_drive, perm["fileId"]) == []


async def test_a_person_without_an_email_fails_the_job_at_once(
    client: httpx.AsyncClient,
    person: Callable[..., Any],
    db: asyncpg.Connection,
    session: AsyncSession,
    real_drive: FakeDriveService,
) -> None:
    admin = await person("admin", admin=True)
    dj = await person("dj")
    event_id = await _event(client, admin)
    await _grant(client, admin, event_id, dj.id, "dj")
    await db.execute("UPDATE users SET email = '' WHERE id = $1", dj.id)

    result = await drive_jobs.process_drive_jobs(session)
    assert result == {"claimed": 1, "succeeded": 0, "failed": 1}
    row = await db.fetchrow("SELECT status, attempts, last_error FROM drive_jobs")
    assert (row["status"], row["attempts"]) == ("failed", 1)
    assert row["last_error"] == (
        f"share_event_folder: user {dj.id} has no email address to share the "
        "event folder with"
    )
    assert real_drive.ops("permissions.create") == []

    # An address Drive cannot share with is refused the same way.
    await db.execute("UPDATE users SET email = 'not-an-address' WHERE id = $1", dj.id)
    await db.execute(
        "UPDATE drive_jobs SET status = 'pending', attempts = 0, next_attempt_at = 0"
    )
    await drive_jobs.process_drive_jobs(session)
    row = await db.fetchrow("SELECT status, last_error FROM drive_jobs")
    assert row["status"] == "failed", row["last_error"]
    assert "is not one Drive can share with" in row["last_error"]


async def test_a_drive_failure_is_retried_like_any_job(
    client: httpx.AsyncClient,
    person: Callable[..., Any],
    db: asyncpg.Connection,
    session: AsyncSession,
    real_drive: FakeDriveService,
) -> None:
    admin = await person("admin", admin=True)
    dj = await person("dj")
    event_id = await _event(client, admin)
    await _grant(client, admin, event_id, dj.id, "dj")
    real_drive.fail_list = RuntimeError("drive down")

    await drive_jobs.process_drive_jobs(session)
    row = await db.fetchrow("SELECT status, attempts, last_error FROM drive_jobs")
    assert (row["status"], row["attempts"], row["last_error"]) == (
        "pending",
        1,
        "drive down",
    )


async def test_a_folder_job_missing_its_ids_is_retried(
    db: asyncpg.Connection, session: AsyncSession, real_drive: FakeDriveService
) -> None:
    for kind in (SHARE, UNSHARE):
        await db.execute(
            "INSERT INTO drive_jobs (id, kind, event_id, status, attempts,"
            " next_attempt_at, created_at, updated_at)"
            " VALUES ($1, $2, 'evt_1', 'pending', 0, 0, 0, 0)",
            f"job_{kind}",
            kind,
        )
    result = await drive_jobs.process_drive_jobs(session)
    assert result == {"claimed": 2, "succeeded": 0, "failed": 2}
    errors = {
        r["last_error"] for r in await db.fetch("SELECT last_error FROM drive_jobs")
    }
    assert all("has no user_id" in e for e in errors)
