"""The event folder sweep: a deleted event's folder goes to the Drive trash
once nothing live is left in it, with the folders inside it. Runs on the real
Drive layer over the in-memory fake Google service: never real Drive."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable, Iterator
from typing import Any

import asyncpg
import httpx
import pytest
from mini_app_polis.google.drive import FOLDER_CACHE
from sqlalchemy.ext.asyncio import AsyncSession

from api_deejaytools.config import get_settings
from api_deejaytools.database import _get_sessionmaker
from api_deejaytools.services import drive, drive_jobs

from ..drive_fakes import FakeDriveService, fake_facade
from .conftest import TEST_DATABASE_URL
from .content_helpers import insert_event, insert_song, new_id

ROOT = "root"
CONTRACT = "Contract Suite 2026-10-01T12:00:00.000Z (updated)"


@pytest.fixture
def real_drive(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeDriveService]:
    """The real Drive layer over the in-memory fake Google service."""
    settings = get_settings()
    monkeypatch.setattr(
        settings,
        "GOOGLE_CREDENTIALS_JSON",
        json.dumps({"client_email": "sa@x.iam", "private_key": "KEY"}),
    )
    monkeypatch.setattr(settings, "GOOGLE_DRIVE_PARENT_FOLDER_ID", ROOT)
    facade, svc = fake_facade()
    monkeypatch.setattr(drive, "_build_facade", lambda email, key: facade)
    drive.clear_drive_folder_cache()
    yield svc
    drive.clear_drive_folder_cache()


@pytest.fixture
async def session(db: asyncpg.Connection) -> AsyncIterator[AsyncSession]:
    async with _get_sessionmaker(TEST_DATABASE_URL)() as s:
        yield s


def _swept(result: list[drive_jobs.TrashedEventFolder]) -> set[tuple[str, str, int]]:
    return {(t.season, t.name, t.subfolders) for t in result}


async def test_sweep_trashes_only_empty_folders_no_event_maps_to(
    real_drive: FakeDriveService, session: AsyncSession, db: asyncpg.Connection
) -> None:
    svc = real_drive
    # An event's own folder (sanitized name, its season) stays, empty or not.
    await insert_event(db, name="Swing / Fling", start_date="2026-05-01")
    kept_live_event = svc.add_path(ROOT, "2026", "Events", "Swing - Fling")
    # The same name in another season maps to no event.
    other_season = svc.add_path(ROOT, "2025", "Events", "Swing - Fling")
    # A contract run's leftovers: division and round folders, only trashed or
    # moved-away copies left.
    contract = svc.add_path(ROOT, "2027", "Events", CONTRACT)
    classic = svc.add_path(contract, "Classic")
    prelims = svc.add_path(classic, "Prelims")
    finals = svc.add_path(classic, "Finals")
    svc.add_file("old.mp3", [finals], trashed=True)
    # A deleted event's folder with a live file deep inside it stays.
    with_file = svc.add_path(ROOT, "2027", "Events", "Gone But Full")
    svc.add_file("copy.mp3", [svc.add_path(with_file, "Classic", "Finals")])
    # A live file of any kind, put there by anyone, keeps the folder.
    with_doc = svc.add_path(ROOT, "2027", "Events", "Notes Left")
    doc = svc.add_file("notes", [with_doc])
    svc.files_by_id[doc]["mimeType"] = "application/vnd.google-apps.document"
    # Upload folders are shared by every song and are not event folders.
    upload = svc.add_path(ROOT, "2027", "Classic")
    deprecated = svc.add_path(ROOT, "_deprecated")

    # Cached by an earlier lookup: trashing must drop it.
    cached = await drive.ensure_event_folder(season_year="2027", event_name=CONTRACT)
    assert cached == contract
    assert contract in FOLDER_CACHE.values()

    result = await drive_jobs.sweep_event_folders(session)

    assert _swept(result) == {("2025", "Swing - Fling", 0), ("2027", CONTRACT, 3)}
    for folder in (other_season, contract, classic, prelims, finals):
        assert svc.trashed(folder)
    for folder in (
        kept_live_event,
        with_file,
        with_doc,
        upload,
        deprecated,
        svc.folder_id(ROOT, "2027"),
        svc.folder_id(svc.add_path(ROOT, "2027"), "Events"),
    ):
        assert folder is not None and not svc.trashed(folder)
    assert not svc.ops("files.delete")
    assert contract not in FOLDER_CACHE.values()
    again = await drive.ensure_event_folder(season_year="2027", event_name=CONTRACT)
    assert again != contract

    # Nothing left to trash; the folder just made again is empty and orphaned,
    # so the next sweep takes it.
    assert _swept(await drive_jobs.sweep_event_folders(session)) == {
        ("2027", CONTRACT, 0)
    }
    assert await drive_jobs.sweep_event_folders(session) == []


async def test_a_deleted_events_folder_is_trashed_once_its_copies_are_gone(
    real_drive: FakeDriveService,
    session: AsyncSession,
    client: httpx.AsyncClient,
    person: Callable[..., Any],
    db: asyncpg.Connection,
) -> None:
    svc = real_drive
    admin = await person("admin", admin=True)
    event_id = await insert_event(db, name="Contract Suite X")
    folder = svc.add_path(ROOT, "2026", "Events", "Contract Suite X")
    copy = svc.add_file("song.mp3", [svc.add_path(folder, "Classic")])
    song_id = await insert_song(db, admin.id)
    await db.execute(
        "INSERT INTO event_song_submissions (id, event_id, song_id,"
        " submitted_by_user_id, drive_copy_file_id, created_at)"
        " VALUES ($1, $2, $3, $4, $5, 1)",
        new_id("sub"),
        event_id,
        song_id,
        admin.id,
        copy,
    )

    # The event exists: its folder stays.
    assert await drive_jobs.sweep_event_folders(session) == []

    res = await client.delete(f"/v1/events/{event_id}", headers=admin.headers)
    assert res.status_code == 200, res.text
    # Its copy is still there until the trash job runs.
    assert await drive_jobs.sweep_event_folders(session) == []
    assert not svc.trashed(folder)

    await drive_jobs.process_drive_jobs(session)
    assert svc.files_by_id[copy]["parents"] == [svc.folder_id(ROOT, "_deprecated")]

    result = await drive_jobs.sweep_event_folders(session)
    assert _swept(result) == {("2026", "Contract Suite X", 1)}
    assert result[0].folder_id == folder
    assert svc.trashed(folder)
    assert not svc.trashed(copy)


async def test_a_limited_sweep_stops_and_the_next_one_goes_on(
    real_drive: FakeDriveService, session: AsyncSession
) -> None:
    svc = real_drive
    names = [f"Contract Suite {i}" for i in range(5)]
    folders = [svc.add_path(ROOT, "2027", "Events", name) for name in names]

    first = await drive_jobs.sweep_event_folders(session, limit=2)
    assert len(first) == 2
    assert sum(svc.trashed(f) for f in folders) == 2

    rest = await drive_jobs.sweep_event_folders(session, limit=10)
    assert len(rest) == 3
    assert all(svc.trashed(f) for f in folders)
