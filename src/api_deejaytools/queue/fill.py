"""The session lock and auto-fill of the active queue (deejaytools-api lib/queue/fill.ts).

Every queue transaction starts with ``lock_session_for_fill``: the session
row is the first lock taken, before any queue_entries row, so all mutations
of a session serialize in one lock order and cannot deadlock (ADR-005,
"Concurrency — pessimistic").
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import delete, func, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import QueueEntry, Session
from .admission import PromotionGate, can_promote_non_priority, can_promote_priority
from .audit import record_queue_event
from .compaction import compact_after_removal, next_bottom_position

# Each pass promotes exactly one entry, so this bound is never reached; it
# only guards against a loop that cannot make progress.
_FILL_GUARD = 10_000


@dataclass(frozen=True, slots=True)
class LockedSession:
    """The session fields auto-fill needs, read under the FOR UPDATE lock."""

    id: str
    status: str
    active_priority_max: int
    active_non_priority_max: int
    floor_trial_starts_at: int
    floor_trial_ends_at: int


async def lock_session_for_fill(
    db: AsyncSession, session_id: str
) -> LockedSession | None:
    """``SELECT … FOR UPDATE`` the session row; None if it does not exist.

    Must be the first statement of any transaction that will fill.
    """
    row = (
        await db.execute(
            select(
                Session.id,
                Session.status,
                Session.active_priority_max,
                Session.active_non_priority_max,
                Session.floor_trial_starts_at,
                Session.floor_trial_ends_at,
            )
            .where(Session.id == session_id)
            .with_for_update()
        )
    ).first()
    if row is None:
        return None
    return LockedSession(
        id=row.id,
        status=row.status,
        active_priority_max=row.active_priority_max,
        active_non_priority_max=row.active_non_priority_max,
        floor_trial_starts_at=row.floor_trial_starts_at,
        floor_trial_ends_at=row.floor_trial_ends_at,
    )


async def count_queue(db: AsyncSession, session_id: str, queue_type: str) -> int:
    """How many entries a session's queue holds."""
    n = (
        await db.execute(
            select(func.count())
            .select_from(QueueEntry)
            .where(
                QueueEntry.session_id == session_id, QueueEntry.queue_type == queue_type
            )
        )
    ).scalar()
    return int(n or 0)


async def fill_active_queue(
    db: AsyncSession,
    session: LockedSession,
    actor_user_id: str | None,
    now: int,
) -> int:
    """Promote waiting entries into active until the caps are reached.

    The caller must hold ``session``'s lock in this transaction. Priority
    drains first; a standard entry goes only when priority is empty and the
    standard cap has room — the same gates as a manual promote. Inert unless
    ``now`` is inside [floor_trial_starts_at, floor_trial_ends_at) and the
    session is not cancelled: the stored status may lag the clock.
    ``actor_user_id`` is None for the scheduler. Returns how many were
    promoted.
    """
    if session.status == "cancelled":
        return 0
    if now < session.floor_trial_starts_at or now >= session.floor_trial_ends_at:
        return 0

    session_id = session.id
    promoted = 0
    for _ in range(_FILL_GUARD):
        gate = PromotionGate(
            active_count=await count_queue(db, session_id, "active"),
            priority_count=await count_queue(db, session_id, "priority"),
            active_priority_max=session.active_priority_max,
            active_non_priority_max=session.active_non_priority_max,
        )

        from_queue: Literal["priority", "non_priority"] | None = None
        if gate.priority_count > 0 and can_promote_priority(gate):
            from_queue = "priority"
        elif can_promote_non_priority(gate):
            from_queue = "non_priority"
        if from_queue is None:
            break

        nxt = (
            await db.execute(
                select(
                    QueueEntry.id,
                    QueueEntry.checkin_id,
                    QueueEntry.entity_pair_id,
                    QueueEntry.entity_solo_user_id,
                    QueueEntry.entity_managed_partnership_id,
                    QueueEntry.position,
                )
                .where(
                    QueueEntry.session_id == session_id,
                    QueueEntry.queue_type == from_queue,
                )
                .order_by(QueueEntry.position.asc())
                .limit(1)
            )
        ).first()
        # The standard gate can open with an empty standard queue.
        if nxt is None:
            break

        await db.execute(delete(QueueEntry).where(QueueEntry.id == nxt.id))
        await compact_after_removal(db, session_id, from_queue, nxt.position)

        new_position = await next_bottom_position(db, session_id, "active")
        await db.execute(
            insert(QueueEntry).values(
                id=str(uuid.uuid4()),
                checkin_id=nxt.checkin_id,
                session_id=session_id,
                entity_pair_id=nxt.entity_pair_id,
                entity_solo_user_id=nxt.entity_solo_user_id,
                entity_managed_partnership_id=nxt.entity_managed_partnership_id,
                queue_type="active",
                position=new_position,
                entered_queue_at=now,
            )
        )
        await record_queue_event(
            db,
            session_id=session_id,
            checkin_id=nxt.checkin_id,
            action="promoted_to_active",
            from_queue=from_queue,
            from_position=nxt.position,
            to_queue="active",
            to_position=new_position,
            actor_user_id=actor_user_id,
            reason=None,
            created_at=now,
        )
        promoted += 1

    return promoted
