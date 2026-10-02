"""The queue model (api_deejaytools.queue), against Postgres.

Ports deejaytools-api src/lib/queue/{admission,compaction,fill,runCounts,
singleEntry}.test.ts. Node mocked the database; these run the same cases
against real rows, because the model's correctness rests on Postgres
(FOR UPDATE, the unique position index).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import asyncpg
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from api_deejaytools.database import _get_sessionmaker
from api_deejaytools.queue import (
    AdmissionContext,
    AdmissionError,
    EntityRef,
    LockedSession,
    PromotionGate,
    can_promote_non_priority,
    can_promote_priority,
    compact_after_removal,
    determine_initial_queue,
    entity_has_live_entry,
    fill_active_queue,
    load_admission_context,
    lock_session_for_fill,
    next_bottom_position,
    runs_for_entity_in_event,
    runs_for_entity_in_session,
)

from .conftest import TEST_DATABASE_URL
from .content_helpers import insert_event, insert_song
from .queue_helpers import (
    dancer_pair,
    insert_entry,
    insert_floor_session,
    insert_managed,
    insert_run,
    insert_user,
    now_ms,
    positions,
    queue_events,
)


@pytest.fixture
async def orm(db: asyncpg.Connection) -> AsyncIterator[AsyncSession]:
    """An ORM session on the emptied test database, as the routes use."""
    async with _get_sessionmaker(TEST_DATABASE_URL)() as session:
        yield session


def gate(active: int, priority: int, pmax: int = 6, npmax: int = 4) -> PromotionGate:
    return PromotionGate(
        active_count=active,
        priority_count=priority,
        active_priority_max=pmax,
        active_non_priority_max=npmax,
    )


# --- canPromotePriority / canPromoteNonPriority --------------------------------


def test_can_promote_priority_below_cap() -> None:
    assert can_promote_priority(gate(5, 2)) is True


def test_can_promote_priority_false_at_cap() -> None:
    assert can_promote_priority(gate(6, 0)) is False


def test_can_promote_non_priority_below_cap_and_priority_empty() -> None:
    assert can_promote_non_priority(gate(3, 0)) is True


def test_can_promote_non_priority_false_when_priority_waiting() -> None:
    assert can_promote_non_priority(gate(0, 1)) is False


def test_can_promote_non_priority_false_at_cap() -> None:
    assert can_promote_non_priority(gate(4, 0)) is False


# --- determineInitialQueue / loadAdmissionContext -------------------------------


async def _admission_world(db: asyncpg.Connection) -> dict[str, str]:
    event_id = await insert_event(db)
    session_id = await insert_floor_session(
        db, event_id=event_id, divisions=(("Classic", True, 3), ("Showcase", False, 0))
    )
    other_session = await insert_floor_session(
        db, event_id=event_id, starts_at=1, ends_at=2, divisions=(("Classic", True, 3),)
    )
    d = await dancer_pair(db)
    return {"event_id": event_id, "session_id": session_id, "other": other_session, **d}


def _ctx(w: dict[str, str], **overrides: object) -> AdmissionContext:
    base: dict[str, object] = {
        "session_id": w["session_id"],
        "event_id": w["event_id"],
        "division_name": "Classic",
        "is_division_priority": True,
        "session_priority_run_limit": 3,
        "event_priority_run_limit": 2,
    }
    base.update(overrides)
    return AdmissionContext(**base)  # type: ignore[arg-type]


async def test_non_priority_when_division_not_priority(
    db: asyncpg.Connection, orm: AsyncSession
) -> None:
    w = await _admission_world(db)
    ctx = _ctx(w, is_division_priority=False)
    assert await determine_initial_queue(orm, EntityRef(pair_id=w["pair_id"]), ctx) == (
        "non_priority"
    )


async def test_non_priority_when_session_run_limit_reached(
    db: asyncpg.Connection, orm: AsyncSession
) -> None:
    w = await _admission_world(db)
    for _ in range(3):
        await insert_run(
            db,
            session_id=w["session_id"],
            song_id=w["song_id"],
            completed_by=w["user_id"],
            pair_id=w["pair_id"],
        )
    ctx = _ctx(w, event_priority_run_limit=None)
    assert await determine_initial_queue(orm, EntityRef(pair_id=w["pair_id"]), ctx) == (
        "non_priority"
    )


async def test_non_priority_when_event_limit_reached(
    db: asyncpg.Connection, orm: AsyncSession
) -> None:
    w = await _admission_world(db)
    # Two runs in another session of the same event: under the session
    # limit here, at the event limit.
    for _ in range(2):
        await insert_run(
            db,
            session_id=w["other"],
            event_id=w["event_id"],
            song_id=w["song_id"],
            completed_by=w["user_id"],
            pair_id=w["pair_id"],
        )
    assert await determine_initial_queue(
        orm, EntityRef(pair_id=w["pair_id"]), _ctx(w)
    ) == ("non_priority")


async def test_priority_when_under_both_limits(
    db: asyncpg.Connection, orm: AsyncSession
) -> None:
    w = await _admission_world(db)
    await insert_run(
        db,
        session_id=w["session_id"],
        event_id=w["event_id"],
        song_id=w["song_id"],
        completed_by=w["user_id"],
        pair_id=w["pair_id"],
    )
    assert await determine_initial_queue(
        orm, EntityRef(pair_id=w["pair_id"]), _ctx(w)
    ) == ("priority")


async def test_event_limit_ignored_when_null(
    db: asyncpg.Connection, orm: AsyncSession
) -> None:
    w = await _admission_world(db)
    for _ in range(5):
        await insert_run(
            db,
            session_id=w["other"],
            event_id=w["event_id"],
            song_id=w["song_id"],
            completed_by=w["user_id"],
            pair_id=w["pair_id"],
        )
    ctx = _ctx(w, event_priority_run_limit=None)
    assert await determine_initial_queue(orm, EntityRef(pair_id=w["pair_id"]), ctx) == (
        "priority"
    )


async def test_runs_in_another_division_do_not_count(
    db: asyncpg.Connection, orm: AsyncSession
) -> None:
    w = await _admission_world(db)
    for _ in range(3):
        await insert_run(
            db,
            session_id=w["session_id"],
            song_id=w["song_id"],
            completed_by=w["user_id"],
            pair_id=w["pair_id"],
            division="Showcase",
        )
    ctx = _ctx(w, event_priority_run_limit=None)
    assert await determine_initial_queue(orm, EntityRef(pair_id=w["pair_id"]), ctx) == (
        "priority"
    )


async def test_load_admission_context_reads_session_division_and_event_limit(
    db: asyncpg.Connection, orm: AsyncSession
) -> None:
    w = await _admission_world(db)
    await db.execute(
        "INSERT INTO event_division_run_limits (event_id, division_name,"
        " priority_run_limit) VALUES ($1, 'Classic', 2)",
        w["event_id"],
    )
    ctx = await load_admission_context(orm, w["session_id"], "Classic")
    assert ctx == _ctx(w)
    no_limit = await load_admission_context(orm, w["session_id"], "Showcase")
    assert no_limit.event_priority_run_limit is None
    assert no_limit.is_division_priority is False


async def test_load_admission_context_errors(
    db: asyncpg.Connection, orm: AsyncSession
) -> None:
    w = await _admission_world(db)
    with pytest.raises(AdmissionError, match="^Session not found$"):
        await load_admission_context(orm, "nope", "Classic")
    with pytest.raises(
        AdmissionError, match="^Division not configured for this session$"
    ):
        await load_admission_context(orm, w["session_id"], "Masters")


# --- runsForEntityInSession / runsForEntityInEvent -------------------------------


async def test_run_counts_per_entity_kind(
    db: asyncpg.Connection, orm: AsyncSession
) -> None:
    event_id = await insert_event(db)
    session_id = await insert_floor_session(db, event_id=event_id)
    d = await dancer_pair(db)
    mp = await insert_managed(db, d["user_id"])
    solo = await insert_user(db)
    song = await insert_song(db, solo)
    for _ in range(3):
        await insert_run(
            db,
            session_id=session_id,
            event_id=event_id,
            song_id=d["song_id"],
            completed_by=d["user_id"],
            pair_id=d["pair_id"],
        )
    await insert_run(
        db, session_id=session_id, song_id=song, completed_by=solo, solo_user_id=solo
    )
    for _ in range(2):
        await insert_run(
            db,
            session_id=session_id,
            event_id=event_id,
            song_id=d["song_id"],
            completed_by=d["user_id"],
            managed_partnership_id=mp,
        )

    pair = EntityRef(pair_id=d["pair_id"])
    assert await runs_for_entity_in_session(orm, pair, session_id, "Classic") == 3
    assert (
        await runs_for_entity_in_session(
            orm, EntityRef(solo_user_id=solo), session_id, "Classic"
        )
        == 1
    )
    assert (
        await runs_for_entity_in_session(
            orm, EntityRef(managed_partnership_id=mp), session_id, "Classic"
        )
        == 2
    )
    assert await runs_for_entity_in_session(orm, pair, session_id, "Teams") == 0
    assert (
        await runs_for_entity_in_session(
            orm, EntityRef(pair_id="x"), session_id, "Classic"
        )
        == 0
    )

    assert await runs_for_entity_in_event(orm, pair, event_id, "Classic") == 3
    assert (
        await runs_for_entity_in_event(
            orm, EntityRef(managed_partnership_id=mp), event_id, "Classic"
        )
        == 2
    )
    # The solo run has no event.
    assert (
        await runs_for_entity_in_event(
            orm, EntityRef(solo_user_id=solo), event_id, "Classic"
        )
        == 0
    )


# --- entityHasLiveEntry -----------------------------------------------------------


async def test_entity_has_live_entry(db: asyncpg.Connection, orm: AsyncSession) -> None:
    session_id = await insert_floor_session(db)
    d = await dancer_pair(db)
    mp = await insert_managed(db, d["user_id"])
    solo = await insert_user(db)
    solo_song = await insert_song(db, solo)

    pair = EntityRef(pair_id=d["pair_id"])
    managed = EntityRef(managed_partnership_id=mp)
    solo_ref = EntityRef(solo_user_id=solo)
    for ref in (pair, managed, solo_ref):
        assert await entity_has_live_entry(orm, ref, session_id) is False

    await insert_entry(
        db,
        session_id=session_id,
        queue_type="priority",
        position=1,
        song_id=d["song_id"],
        submitted_by=d["user_id"],
        pair_id=d["pair_id"],
    )
    await insert_entry(
        db,
        session_id=session_id,
        queue_type="priority",
        position=2,
        song_id=d["song_id"],
        submitted_by=d["user_id"],
        managed_partnership_id=mp,
    )
    await insert_entry(
        db,
        session_id=session_id,
        queue_type="active",
        position=1,
        song_id=solo_song,
        submitted_by=solo,
        solo_user_id=solo,
    )
    for ref in (pair, managed, solo_ref):
        assert await entity_has_live_entry(orm, ref, session_id) is True
    other = await insert_floor_session(db)
    assert await entity_has_live_entry(orm, pair, other) is False


# --- nextBottomPosition / compactAfterRemoval -------------------------------------


async def _queue(
    db: asyncpg.Connection, session_id: str, queue_type: str, n: int
) -> list[str]:
    ids = []
    for i in range(1, n + 1):
        d = await dancer_pair(db)
        ids.append(
            (
                await insert_entry(
                    db,
                    session_id=session_id,
                    queue_type=queue_type,
                    position=i,
                    song_id=d["song_id"],
                    submitted_by=d["user_id"],
                    pair_id=d["pair_id"],
                )
            )["entry_id"]
        )
    return ids


async def test_next_bottom_position(db: asyncpg.Connection, orm: AsyncSession) -> None:
    session_id = await insert_floor_session(db)
    assert await next_bottom_position(orm, session_id, "active") == 1
    await _queue(db, session_id, "non_priority", 5)
    assert await next_bottom_position(orm, session_id, "non_priority") == 6
    assert await next_bottom_position(orm, session_id, "priority") == 1


@pytest.mark.parametrize("queue_type", ["active", "priority", "non_priority"])
async def test_compact_after_removal_closes_the_gap(
    db: asyncpg.Connection, orm: AsyncSession, queue_type: str
) -> None:
    session_id = await insert_floor_session(db)
    ids = await _queue(db, session_id, queue_type, 4)
    await db.execute("DELETE FROM queue_entries WHERE id = $1", ids[1])
    await compact_after_removal(orm, session_id, queue_type, 2)  # type: ignore[arg-type]
    await orm.commit()
    assert await positions(db, session_id, queue_type) == [
        (ids[0], 1),
        (ids[2], 2),
        (ids[3], 3),
    ]


async def test_compact_after_removal_noop_below_bottom(
    db: asyncpg.Connection, orm: AsyncSession
) -> None:
    session_id = await insert_floor_session(db)
    ids = await _queue(db, session_id, "non_priority", 2)
    await compact_after_removal(orm, session_id, "non_priority", 100)
    await orm.commit()
    assert await positions(db, session_id, "non_priority") == [(ids[0], 1), (ids[1], 2)]


# --- lockSessionForFill / fillActiveQueue -----------------------------------------


def _locked(session_id: str, **overrides: object) -> LockedSession:
    now = now_ms()
    base: dict[str, object] = {
        "id": session_id,
        "status": "in_progress",
        "active_priority_max": 2,
        "active_non_priority_max": 4,
        "floor_trial_starts_at": now - 10_000,
        "floor_trial_ends_at": now + 10_000,
    }
    base.update(overrides)
    return LockedSession(**base)  # type: ignore[arg-type]


async def test_lock_session_for_fill_reads_the_row(
    db: asyncpg.Connection, orm: AsyncSession
) -> None:
    session_id = await insert_floor_session(db, priority_max=5, non_priority_max=3)
    locked = await lock_session_for_fill(orm, session_id)
    assert locked is not None
    assert (locked.id, locked.status) == (session_id, "in_progress")
    assert (locked.active_priority_max, locked.active_non_priority_max) == (5, 3)
    assert await lock_session_for_fill(orm, "missing") is None
    await orm.rollback()


async def test_lock_session_for_fill_blocks_a_second_locker(
    db: asyncpg.Connection, orm: AsyncSession
) -> None:
    session_id = await insert_floor_session(db)
    await lock_session_for_fill(orm, session_id)

    async with _get_sessionmaker(TEST_DATABASE_URL)() as other:
        second = asyncio.create_task(lock_session_for_fill(other, session_id))
        await asyncio.sleep(0.3)
        assert not second.done(), "the second FOR UPDATE must wait for the first"
        await orm.commit()
        assert (await asyncio.wait_for(second, 5)) is not None
        await other.rollback()


async def test_fill_promotes_priority_up_to_cap_then_stops(
    db: asyncpg.Connection, orm: AsyncSession
) -> None:
    admin = await insert_user(db)
    session_id = await insert_floor_session(db)
    ids = await _queue(db, session_id, "priority", 3)
    now = now_ms()
    promoted = await fill_active_queue(orm, _locked(session_id), admin, now)
    await orm.commit()
    assert promoted == 2
    active = await positions(db, session_id, "active")
    assert [p for _, p in active] == [1, 2]
    # The third priority entry moved up to position 1.
    assert await positions(db, session_id, "priority") == [(ids[2], 1)]
    events = await queue_events(db, session_id)
    assert [
        (
            e["action"],
            e["from_queue"],
            e["from_position"],
            e["to_queue"],
            e["to_position"],
        )
        for e in events
    ] == [
        ("promoted_to_active", "priority", 1, "active", 1),
        ("promoted_to_active", "priority", 1, "active", 2),
    ]
    assert {e["actor_user_id"] for e in events} == {admin}
    entered = await db.fetchval(
        "SELECT MIN(entered_queue_at) FROM queue_entries WHERE session_id = $1"
        " AND queue_type = 'active'",
        session_id,
    )
    assert entered == now


async def test_fill_promotes_standard_only_when_priority_empty(
    db: asyncpg.Connection, orm: AsyncSession
) -> None:
    session_id = await insert_floor_session(db)
    await _queue(db, session_id, "non_priority", 1)
    promoted = await fill_active_queue(
        orm, _locked(session_id, active_priority_max=6), None, now_ms()
    )
    await orm.commit()
    assert promoted == 1
    events = await queue_events(db, session_id)
    assert events[0]["actor_user_id"] is None
    assert events[0]["from_queue"] == "non_priority"


async def test_fill_standard_waits_behind_priority(
    db: asyncpg.Connection, orm: AsyncSession
) -> None:
    session_id = await insert_floor_session(db)
    await _queue(db, session_id, "non_priority", 2)
    priority = await _queue(db, session_id, "priority", 1)
    # Caps 2/1: the priority entry goes on; then active (1) is at the
    # standard cap, so the standard entries wait.
    promoted = await fill_active_queue(
        orm,
        _locked(session_id, active_priority_max=2, active_non_priority_max=1),
        None,
        now_ms(),
    )
    await orm.commit()
    assert promoted == 1
    assert len(await positions(db, session_id, "non_priority")) == 2
    checkin = await db.fetchval(
        "SELECT checkin_id FROM queue_entries WHERE session_id = $1 AND queue_type = 'active'",
        session_id,
    )
    assert checkin == await db.fetchval(
        "SELECT checkin_id FROM queue_events WHERE session_id = $1", session_id
    )
    assert priority  # promoted from the priority queue


async def test_fill_nothing_when_priority_waiting_and_cap_full(
    db: asyncpg.Connection, orm: AsyncSession
) -> None:
    session_id = await insert_floor_session(db)
    await _queue(db, session_id, "active", 2)
    await _queue(db, session_id, "priority", 1)
    promoted = await fill_active_queue(
        orm, _locked(session_id, active_priority_max=2), None, now_ms()
    )
    assert promoted == 0
    await orm.rollback()


async def test_fill_inert_outside_the_window(
    db: asyncpg.Connection, orm: AsyncSession
) -> None:
    session_id = await insert_floor_session(db)
    await _queue(db, session_id, "priority", 1)
    now = now_ms()
    before = _locked(
        session_id,
        floor_trial_starts_at=now + 60_000,
        floor_trial_ends_at=now + 120_000,
    )
    assert await fill_active_queue(orm, before, None, now) == 0
    # The end of the window is exclusive.
    at_end = _locked(session_id, floor_trial_starts_at=now - 1, floor_trial_ends_at=now)
    assert await fill_active_queue(orm, at_end, None, now) == 0
    await orm.rollback()
    assert await positions(db, session_id, "active") == []


async def test_fill_inert_when_cancelled(
    db: asyncpg.Connection, orm: AsyncSession
) -> None:
    session_id = await insert_floor_session(db)
    await _queue(db, session_id, "priority", 1)
    cancelled = _locked(session_id, status="cancelled")
    assert await fill_active_queue(orm, cancelled, None, now_ms()) == 0
    await orm.rollback()
