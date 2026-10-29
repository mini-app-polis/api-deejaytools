"""A partner set on a song after its Drive file was built is shared the file.

The upload's build shares the file with the partner the song has then. A
partner set later by PATCH /v1/songs/{id} queues a ``share_song`` job in the
same transaction, and the job gives the partner's email reader access when it
runs. The job runs on the real Drive layer over the in-memory fake Google
service: never real Drive.
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
from .content_helpers import drive_jobs as queued
from .content_helpers import insert_song

FILE = "song-file"


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


async def _partner(
    client: httpx.AsyncClient, who: Any, email: str | None = "pat@example.com"
) -> str:
    body: dict[str, Any] = {
        "first_name": "Pat",
        "last_name": "P",
        "partner_role": "follower",
    }
    if email is not None:
        body["email"] = email
    res = await client.post("/v1/partners", json=body, headers=who.headers)
    assert res.status_code == 201, res.text
    partner_id: str = res.json()["data"]["id"]
    return partner_id


async def _set_partner(
    client: httpx.AsyncClient, who: Any, song: str, partner_id: str | None
) -> None:
    res = await client.patch(
        f"/v1/songs/{song}", json={"partner_id": partner_id}, headers=who.headers
    )
    assert res.status_code == 200, res.text


def _readers(svc: FakeDriveService, file_id: str) -> list[str]:
    return [
        p["emailAddress"]
        for p in svc.permissions_by_file.get(file_id, [])
        if p["role"] == "reader"
    ]


async def test_a_partner_set_after_the_build_is_shared_the_file(
    client: httpx.AsyncClient,
    person: Callable[..., Any],
    db: asyncpg.Connection,
    session: AsyncSession,
    real_drive: FakeDriveService,
) -> None:
    alice = await person("alice")
    song = await insert_song(db, alice.id, drive_file_id=FILE, drive_folder_id="f")
    partner = await _partner(client, alice)

    await _set_partner(client, alice, song, partner)
    assert await queued(db) == [("share_song", None, FILE, "pending")]

    result = await drive_jobs.process_drive_jobs(session)
    assert result == {"claimed": 1, "succeeded": 1, "failed": 0}
    assert _readers(real_drive, FILE) == ["pat@example.com"]
    (perm,) = real_drive.ops("permissions.create")
    assert perm["sendNotificationEmail"] is False


async def test_nothing_is_queued_before_the_build_or_for_no_change(
    client: httpx.AsyncClient, person: Callable[..., Any], db: asyncpg.Connection
) -> None:
    alice = await person("alice")
    partner = await _partner(client, alice)
    unbuilt = await insert_song(db, alice.id)
    await _set_partner(client, alice, unbuilt, partner)

    built = await insert_song(
        db, alice.id, partner_id=partner, drive_file_id=FILE, drive_folder_id="f"
    )
    await _set_partner(client, alice, built, partner)
    await _set_partner(client, alice, built, None)
    res = await client.patch(
        f"/v1/songs/{built}", json={"routine_name": "Blue"}, headers=alice.headers
    )
    assert res.status_code == 200, res.text

    assert await queued(db) == []


async def test_the_job_follows_the_partner_the_song_has_when_it_runs(
    client: httpx.AsyncClient,
    person: Callable[..., Any],
    db: asyncpg.Connection,
    session: AsyncSession,
    real_drive: FakeDriveService,
) -> None:
    alice = await person("alice")
    song = await insert_song(db, alice.id, drive_file_id=FILE, drive_folder_id="f")
    first = await _partner(client, alice, "first@example.com")
    second = await _partner(client, alice, "second@example.com")

    await _set_partner(client, alice, song, first)
    await _set_partner(client, alice, song, second)
    await drive_jobs.process_drive_jobs(session)
    # Both jobs read the song's partner when they run: neither shares with
    # the first.
    assert set(_readers(real_drive, FILE)) == {"second@example.com"}


async def test_a_cleared_partner_or_one_with_no_email_shares_nothing(
    client: httpx.AsyncClient,
    person: Callable[..., Any],
    db: asyncpg.Connection,
    session: AsyncSession,
    real_drive: FakeDriveService,
) -> None:
    alice = await person("alice")
    cleared = await insert_song(db, alice.id, drive_file_id=FILE, drive_folder_id="f")
    await _set_partner(client, alice, cleared, await _partner(client, alice))
    await _set_partner(client, alice, cleared, None)

    no_email = await insert_song(
        db, alice.id, drive_file_id="other-file", drive_folder_id="f"
    )
    await _set_partner(client, alice, no_email, await _partner(client, alice, None))

    result = await drive_jobs.process_drive_jobs(session)
    assert result == {"claimed": 2, "succeeded": 2, "failed": 0}
    assert real_drive.ops("permissions.create") == []
    assert [status for *_, status in await queued(db)] == ["done", "done"]


async def test_a_deleted_song_shares_nothing(
    client: httpx.AsyncClient,
    person: Callable[..., Any],
    db: asyncpg.Connection,
    session: AsyncSession,
    real_drive: FakeDriveService,
) -> None:
    alice = await person("alice")
    song = await insert_song(db, alice.id, drive_file_id=FILE, drive_folder_id="f")
    await _set_partner(client, alice, song, await _partner(client, alice))
    await db.execute("UPDATE songs SET deleted_at = 1 WHERE id = $1", song)

    await drive_jobs.process_drive_jobs(session)
    assert real_drive.ops("permissions.create") == []
