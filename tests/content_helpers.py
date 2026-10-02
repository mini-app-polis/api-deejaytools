"""Direct-insert fixtures for the content route tests (partners, songs,
submissions, managed partnerships): rows the routes under test do not
create themselves, such as sessions, check-ins and queue entries."""

from __future__ import annotations

import uuid

import asyncpg


def new_id(prefix: str) -> str:
    """A unique id with a readable prefix."""
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


async def insert_song(
    db: asyncpg.Connection,
    user_id: str,
    *,
    song_id: str | None = None,
    partner_id: str | None = None,
    managed_partnership_id: str | None = None,
    division: str | None = None,
    drive_file_id: str | None = None,
    drive_folder_id: str | None = None,
    processed_filename: str | None = None,
    routine_name: str | None = None,
    season_year: str | None = None,
    created_at: int = 1,
) -> str:
    """A songs row; returns its id."""
    song_id = song_id or new_id("song")
    await db.execute(
        "INSERT INTO songs (id, user_id, partner_id, managed_partnership_id, division,"
        " drive_file_id, drive_folder_id, processed_filename, routine_name,"
        " season_year, created_at, updated_at)"
        " VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $11)",
        song_id,
        user_id,
        partner_id,
        managed_partnership_id,
        division,
        drive_file_id,
        drive_folder_id,
        processed_filename,
        routine_name,
        season_year,
        created_at,
    )
    return song_id


async def insert_event(
    db: asyncpg.Connection,
    *,
    name: str = "Swing Fling",
    start_date: str = "2026-05-01",
    end_date: str = "2026-05-03",
) -> str:
    """An events row; returns its id."""
    event_id = new_id("evt")
    await db.execute(
        "INSERT INTO events (id, name, start_date, end_date, created_at, updated_at)"
        " VALUES ($1, $2, $3, $4, 1, 1)",
        event_id,
        name,
        start_date,
        end_date,
    )
    return event_id


async def insert_session(
    db: asyncpg.Connection, *, status: str = "scheduled", division: str = "Classic"
) -> str:
    """A sessions row with one division; returns its id."""
    session_id = new_id("ses")
    await db.execute(
        "INSERT INTO sessions (id, name, checkin_opens_at, floor_trial_starts_at,"
        " floor_trial_ends_at, status, created_at)"
        " VALUES ($1, 'Session', 1, 2, 3, $2, 1)",
        session_id,
        status,
    )
    await db.execute(
        "INSERT INTO session_divisions (id, session_id, division_name)"
        " VALUES ($1, $2, $3)",
        new_id("sd"),
        session_id,
        division,
    )
    return session_id


async def insert_checkin(
    db: asyncpg.Connection,
    *,
    session_id: str,
    song_id: str,
    submitted_by: str,
    pair_id: str | None = None,
    managed_partnership_id: str | None = None,
    queued: bool = True,
    division: str = "Classic",
) -> str:
    """A check-in for a pair, a managed partnership, or else the submitter
    alone; with ``queued`` it also gets a live queue entry. Returns its id."""
    checkin_id = new_id("chk")
    solo = None if (pair_id or managed_partnership_id) else submitted_by
    await db.execute(
        "INSERT INTO checkins (id, session_id, division_name, entity_pair_id,"
        " entity_solo_user_id, entity_managed_partnership_id, song_id,"
        " submitted_by_user_id, initial_queue, created_at)"
        " VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 'priority', 1)",
        checkin_id,
        session_id,
        division,
        pair_id,
        solo,
        managed_partnership_id,
        song_id,
        submitted_by,
    )
    if queued:
        await db.execute(
            "INSERT INTO queue_entries (id, checkin_id, session_id, queue_type,"
            " position, entered_queue_at, entity_pair_id, entity_solo_user_id,"
            " entity_managed_partnership_id)"
            " VALUES ($1, $2, $3, 'priority', 1, 1, $4, $5, $6)",
            new_id("qe"),
            checkin_id,
            session_id,
            pair_id,
            solo,
            managed_partnership_id,
        )
    return checkin_id


async def drive_jobs(
    db: asyncpg.Connection,
) -> list[tuple[str, str | None, str | None, str]]:
    """Every queued Drive job as (kind, submission_id, file_id, status), sorted."""
    rows = await db.fetch("SELECT kind, submission_id, file_id, status FROM drive_jobs")
    return sorted(
        ((r["kind"], r["submission_id"], r["file_id"], r["status"]) for r in rows),
        key=lambda job: tuple(part or "" for part in job),
    )
