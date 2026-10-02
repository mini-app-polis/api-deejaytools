"""services.session_tick: stored status advance and scheduled auto-fill
(deejaytools-api src/services/cron.ts and cron.test.ts)."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any

import asyncpg
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from api_deejaytools.cache import response_cache
from api_deejaytools.database import _get_sessionmaker
from api_deejaytools.services import session_tick

from .conftest import TEST_DATABASE_URL
from .queue_helpers import (
    HOUR,
    dancer_pair,
    insert_entry,
    insert_floor_session,
    now_ms,
    positions,
    queue_events,
)


@pytest.fixture
async def orm(db: asyncpg.Connection) -> AsyncIterator[AsyncSession]:
    async with _get_sessionmaker(TEST_DATABASE_URL)() as session:
        yield session


async def _status(db: asyncpg.Connection, session_id: str) -> str:
    return await db.fetchval("SELECT status FROM sessions WHERE id = $1", session_id)


async def _session(db: asyncpg.Connection, status: str, offset_hours: float) -> str:
    """Check-in opens at now+offset, trial starts an hour later, ends after
    another hour."""
    base = now_ms() + int(offset_hours * HOUR)
    return await insert_floor_session(
        db,
        status=status,
        checkin_opens_at=base,
        starts_at=base + HOUR,
        ends_at=base + 2 * HOUR,
    )


async def test_tick_returns_zero_with_no_sessions(
    db: asyncpg.Connection, orm: AsyncSession
) -> None:
    assert await session_tick.tick_session_statuses(orm) == 0


async def test_tick_advances_each_status_one_step(
    db: asyncpg.Connection, orm: AsyncSession, caplog: pytest.LogCaptureFixture
) -> None:
    scheduled = await _session(db, "scheduled", -0.5)  # check-in open
    checkin_open = await _session(db, "checkin_open", -1.5)  # trial running
    in_progress = await _session(db, "in_progress", -2.5)  # trial over
    not_yet = await _session(db, "scheduled", 1)
    # Far past every boundary: still only one step per tick.
    lagging = await _session(db, "scheduled", -10)
    cancelled = await _session(db, "cancelled", -10)
    completed = await _session(db, "completed", -10)

    with caplog.at_level(logging.INFO):
        assert await session_tick.tick_session_statuses(orm) == 4
    assert await _status(db, scheduled) == "checkin_open"
    assert await _status(db, checkin_open) == "in_progress"
    assert await _status(db, in_progress) == "completed"
    assert await _status(db, not_yet) == "scheduled"
    assert await _status(db, lagging) == "checkin_open"
    assert await _status(db, cancelled) == "cancelled"
    assert await _status(db, completed) == "completed"
    messages = [r.getMessage() for r in caplog.records]
    assert sum("session_status_updated" in m for m in messages) == 4
    assert any(
        "tick_completed sessions_checked=5 sessions_updated=4" in m for m in messages
    )

    assert await session_tick.tick_session_statuses(orm) == 1
    assert await _status(db, lagging) == "in_progress"


async def _waiting(db: asyncpg.Connection, session_id: str, n: int) -> None:
    for i in range(n):
        d = await dancer_pair(db)
        await insert_entry(
            db,
            session_id=session_id,
            queue_type="non_priority",
            position=i + 1,
            song_id=d["song_id"],
            submitted_by=d["user_id"],
            pair_id=d["pair_id"],
        )


async def test_fill_running_sessions(
    db: asyncpg.Connection, orm: AsyncSession, caplog: pytest.LogCaptureFixture
) -> None:
    now = now_ms()
    # Stored status lags (still checkin_open), but the clock says running.
    running = await insert_floor_session(
        db, status="checkin_open", priority_max=2, non_priority_max=2
    )
    full = await insert_floor_session(db, priority_max=1, non_priority_max=0)
    future = await insert_floor_session(
        db, checkin_opens_at=now - HOUR, starts_at=now + HOUR, ends_at=now + 2 * HOUR
    )
    over = await insert_floor_session(
        db,
        checkin_opens_at=now - 3 * HOUR,
        starts_at=now - 2 * HOUR,
        ends_at=now - HOUR,
    )
    cancelled = await insert_floor_session(db, status="cancelled")
    for sid in (running, full, future, over, cancelled):
        await _waiting(db, sid, 3)
        response_cache.set(f"queue:{sid}:active", {"stale": True}, 60)

    with caplog.at_level(logging.INFO):
        assert await session_tick.fill_running_sessions(orm) == 2
    assert [p for _, p in await positions(db, running, "active")] == [1, 2]
    assert [p for _, p in await positions(db, running, "non_priority")] == [1]
    for sid in (full, future, over, cancelled):
        assert await positions(db, sid, "active") == []
    events = await queue_events(db, running)
    assert [(e["action"], e["actor_user_id"]) for e in events] == [
        ("promoted_to_active", None),
        ("promoted_to_active", None),
    ]
    # Only the session that changed loses its cached views.
    assert response_cache.get(f"queue:{running}:active") is None
    assert response_cache.get(f"queue:{full}:active") == {"stale": True}
    assert any(
        "auto_fill_completed sessions_checked=2 entries_promoted=2" in r.getMessage()
        for r in caplog.records
    )
    # Nothing left to promote.
    assert await session_tick.fill_running_sessions(orm) == 0


async def test_fill_running_sessions_continues_after_a_failure(
    db: asyncpg.Connection,
    orm: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    bad = await insert_floor_session(db)
    good = await insert_floor_session(db)
    await _waiting(db, bad, 1)
    await _waiting(db, good, 1)
    real = session_tick.fill_active_queue

    async def flaky(session: AsyncSession, locked: Any, actor: Any, now: int) -> int:
        if locked.id == bad:
            await real(session, locked, actor, now)  # writes, then fails
            raise RuntimeError("boom")
        return await real(session, locked, actor, now)

    monkeypatch.setattr(session_tick, "fill_active_queue", flaky)
    with caplog.at_level(logging.INFO):
        assert await session_tick.fill_running_sessions(orm) == 1
    # The failed session's work was rolled back.
    assert await positions(db, bad, "active") == []
    assert len(await positions(db, bad, "non_priority")) == 1
    assert len(await positions(db, good, "active")) == 1
    assert any(
        f"auto_fill_failed session_id={bad}" in r.getMessage() for r in caplog.records
    )
