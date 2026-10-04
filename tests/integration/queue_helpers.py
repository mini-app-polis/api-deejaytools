"""Direct-insert fixtures for the floor-trial queue tests: sessions with
windows and caps, users, partners, pairs, managed partnerships, waiting and
active entries, and runs."""

from __future__ import annotations

import time
from typing import Any

import asyncpg

from .content_helpers import insert_song, new_id

HOUR = 3_600_000


def now_ms() -> int:
    """Wall-clock epoch milliseconds."""
    return int(time.time() * 1000)


async def insert_user(
    db: asyncpg.Connection,
    *,
    user_id: str | None = None,
    first: str | None = "Ada",
    last: str | None = "Lovelace",
    email: str | None = None,
) -> str:
    """A users row with no principal; returns its id."""
    user_id = user_id or new_id("user")
    await db.execute(
        "INSERT INTO users (id, email, first_name, last_name, created_at, updated_at)"
        " VALUES ($1, $2, $3, $4, 1, 1)",
        user_id,
        email or f"{user_id}@example.test",
        first,
        last,
    )
    return user_id


async def insert_floor_session(
    db: asyncpg.Connection,
    *,
    event_id: str | None = None,
    status: str = "in_progress",
    checkin_opens_at: int | None = None,
    starts_at: int | None = None,
    ends_at: int | None = None,
    priority_max: int = 6,
    non_priority_max: int = 4,
    divisions: tuple[tuple[str, bool, int], ...] = (("Classic", False, 0),),
) -> str:
    """A session whose floor trial is under way unless told otherwise, with
    divisions given as (name, is_priority, priority_run_limit)."""
    now = now_ms()
    session_id = new_id("ses")
    await db.execute(
        "INSERT INTO sessions (id, event_id, name, checkin_opens_at,"
        " floor_trial_starts_at, floor_trial_ends_at, status, created_at,"
        " active_priority_max, active_non_priority_max)"
        " VALUES ($1, $2, 'Floor', $3, $4, $5, $6, 1, $7, $8)",
        session_id,
        event_id,
        checkin_opens_at if checkin_opens_at is not None else now - HOUR,
        starts_at if starts_at is not None else now - 60_000,
        ends_at if ends_at is not None else now + HOUR,
        status,
        priority_max,
        non_priority_max,
    )
    for i, (name, is_priority, limit) in enumerate(divisions):
        await db.execute(
            "INSERT INTO session_divisions (id, session_id, division_name,"
            " is_priority, sort_order, priority_run_limit)"
            " VALUES ($1, $2, $3, $4, $5, $6)",
            new_id("sd"),
            session_id,
            name,
            is_priority,
            i,
            limit,
        )
    return session_id


async def insert_partner(
    db: asyncpg.Connection,
    user_id: str,
    *,
    first: str = "Bob",
    last: str = "Jones",
    kind: str = "partner",
) -> str:
    """A partners row owned by ``user_id``; returns its id."""
    partner_id = new_id("pt")
    await db.execute(
        "INSERT INTO partners (id, user_id, first_name, last_name, kind,"
        " created_at, updated_at) VALUES ($1, $2, $3, $4, $5, 1, 1)",
        partner_id,
        user_id,
        first,
        last,
        kind,
    )
    return partner_id


async def insert_pair(db: asyncpg.Connection, user_id: str, partner_id: str) -> str:
    """A pairs row led by ``user_id``; returns its id."""
    pair_id = new_id("pair")
    await db.execute(
        "INSERT INTO pairs (id, user_a_id, partner_b_id, created_at)"
        " VALUES ($1, $2, $3, 1)",
        pair_id,
        user_id,
        partner_id,
    )
    return pair_id


async def insert_managed(
    db: asyncpg.Connection,
    user_id: str,
    *,
    leader: tuple[str, str] = ("Lea", "Der"),
    follower: tuple[str, str] = ("Fol", "Lower"),
    deleted: bool = False,
) -> str:
    """A managed_partnerships row owned by ``user_id``; returns its id."""
    mp_id = new_id("mp")
    await db.execute(
        "INSERT INTO managed_partnerships (id, user_id, leader_first_name,"
        " leader_last_name, follower_first_name, follower_last_name, created_at,"
        " updated_at, deleted_at) VALUES ($1, $2, $3, $4, $5, $6, 1, 1, $7)",
        mp_id,
        user_id,
        *leader,
        *follower,
        5 if deleted else None,
    )
    return mp_id


async def dancer_pair(
    db: asyncpg.Connection,
    *,
    user_id: str | None = None,
    first: str = "Ada",
    last: str = "Lovelace",
    partner: tuple[str, str] = ("Bob", "Jones"),
    kind: str = "partner",
) -> dict[str, str]:
    """A user (inserted unless ``user_id`` names an existing one), a partner,
    their pair and a song attached to the partner."""
    if user_id is None:
        user_id = await insert_user(db, first=first, last=last)
    partner_id = await insert_partner(
        db, user_id, first=partner[0], last=partner[1], kind=kind
    )
    pair_id = await insert_pair(db, user_id, partner_id)
    song_id = await insert_song(db, user_id, partner_id=partner_id)
    return {
        "user_id": user_id,
        "partner_id": partner_id,
        "pair_id": pair_id,
        "song_id": song_id,
    }


async def insert_entry(
    db: asyncpg.Connection,
    *,
    session_id: str,
    queue_type: str,
    position: int,
    song_id: str,
    submitted_by: str,
    pair_id: str | None = None,
    managed_partnership_id: str | None = None,
    solo_user_id: str | None = None,
    division: str = "Classic",
    notes: str | None = None,
    created_at: int = 1,
) -> dict[str, str]:
    """A check-in with a live queue entry at ``position``; returns both ids."""
    checkin_id = new_id("chk")
    entry_id = new_id("qe")
    initial = "priority" if queue_type == "priority" else "non_priority"
    await db.execute(
        "INSERT INTO checkins (id, session_id, division_name, entity_pair_id,"
        " entity_solo_user_id, entity_managed_partnership_id, song_id,"
        " submitted_by_user_id, initial_queue, notes, created_at)"
        " VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)",
        checkin_id,
        session_id,
        division,
        pair_id,
        solo_user_id,
        managed_partnership_id,
        song_id,
        submitted_by,
        initial,
        notes,
        created_at,
    )
    await db.execute(
        "INSERT INTO queue_entries (id, checkin_id, session_id, queue_type,"
        " position, entered_queue_at, entity_pair_id, entity_solo_user_id,"
        " entity_managed_partnership_id) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)",
        entry_id,
        checkin_id,
        session_id,
        queue_type,
        position,
        created_at,
        pair_id,
        solo_user_id,
        managed_partnership_id,
    )
    return {"checkin_id": checkin_id, "entry_id": entry_id}


async def insert_run(
    db: asyncpg.Connection,
    *,
    session_id: str,
    song_id: str,
    completed_by: str,
    pair_id: str | None = None,
    managed_partnership_id: str | None = None,
    solo_user_id: str | None = None,
    event_id: str | None = None,
    division: str = "Classic",
    completed_at: int = 1,
) -> str:
    """A completed run (with its own off-queue check-in); returns its id."""
    checkin_id = new_id("chk")
    await db.execute(
        "INSERT INTO checkins (id, session_id, division_name, entity_pair_id,"
        " entity_solo_user_id, entity_managed_partnership_id, song_id,"
        " submitted_by_user_id, initial_queue, created_at)"
        " VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 'priority', 1)",
        checkin_id,
        session_id,
        division,
        pair_id,
        solo_user_id,
        managed_partnership_id,
        song_id,
        completed_by,
    )
    run_id = new_id("run")
    await db.execute(
        "INSERT INTO runs (id, checkin_id, session_id, event_id, division_name,"
        " entity_pair_id, entity_solo_user_id, entity_managed_partnership_id,"
        " song_id, completed_at, completed_by_user_id)"
        " VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)",
        run_id,
        checkin_id,
        session_id,
        event_id,
        division,
        pair_id,
        solo_user_id,
        managed_partnership_id,
        song_id,
        completed_at,
        completed_by,
    )
    return run_id


async def positions(
    db: asyncpg.Connection, session_id: str, queue_type: str
) -> list[tuple[str, int]]:
    """(entry id, position) for a queue, front first."""
    rows = await db.fetch(
        "SELECT id, position FROM queue_entries WHERE session_id = $1"
        " AND queue_type = $2 ORDER BY position",
        session_id,
        queue_type,
    )
    return [(r["id"], r["position"]) for r in rows]


async def queue_events(db: asyncpg.Connection, session_id: str) -> list[dict[str, Any]]:
    """Every queue_events row for a session, oldest first."""
    rows = await db.fetch(
        "SELECT action, from_queue, from_position, to_queue, to_position,"
        " actor_user_id, reason, checkin_id FROM queue_events WHERE session_id = $1"
        " ORDER BY created_at, ctid",
        session_id,
    )
    return [dict(r) for r in rows]
