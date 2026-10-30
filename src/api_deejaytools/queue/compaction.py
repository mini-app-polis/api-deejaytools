"""Closing gaps and appending to a queue.

Positions are 1-based and dense per (session, queue). Callers run these in
the same transaction as the delete or insert they accompany.
"""

from __future__ import annotations

from typing import Literal

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import QueueEntry

QueueType = Literal["priority", "non_priority", "active"]

# Far above any real position (queues grow by one per entry), so a row parked
# here cannot collide with another while the gap is closed.
COMPACTION_SENTINEL_OFFSET = 1_000_000


async def set_position(db: AsyncSession, entry_id: str, position: int) -> None:
    """Move one queue entry to ``position``."""
    await db.execute(
        update(QueueEntry)
        .where(QueueEntry.id == entry_id)
        .values(position=position)
        .execution_options(synchronize_session=False)
    )


async def compact_after_removal(
    db: AsyncSession, session_id: str, queue_type: QueueType, removed_position: int
) -> None:
    """Shift every entry below ``removed_position`` up by one.

    Two phases, row by row: each affected row first moves to ``position +
    1_000_000``, then to ``position - 1``, so no intermediate state collides
    on the (session, queue, position) unique index.
    """
    rows = (
        await db.execute(
            select(QueueEntry.id, QueueEntry.position)
            .where(
                QueueEntry.session_id == session_id,
                QueueEntry.queue_type == queue_type,
                QueueEntry.position > removed_position,
            )
            .order_by(QueueEntry.position.asc())
        )
    ).all()
    if not rows:
        return
    for row in rows:
        await set_position(db, row.id, row.position + COMPACTION_SENTINEL_OFFSET)
    for row in rows:
        await set_position(db, row.id, row.position - 1)


async def next_bottom_position(
    db: AsyncSession, session_id: str, queue_type: QueueType
) -> int:
    """The position to append at: ``MAX(position) + 1``, or 1 for an empty queue."""
    max_position = (
        await db.execute(
            select(func.coalesce(func.max(QueueEntry.position), 0)).where(
                QueueEntry.session_id == session_id,
                QueueEntry.queue_type == queue_type,
            )
        )
    ).scalar()
    return int(max_position or 0) + 1


async def remove_entry_of_checkin(
    db: AsyncSession, session_id: str, checkin_id: str
) -> tuple[QueueType, int] | None:
    """Remove the check-in's queue entry and close the gap it leaves.

    Call it under the session lock. The entry is read here, by its check-in,
    not from a read made before the lock: a promotion in between re-inserts
    an entry under a new id in another queue, and a stale read would delete
    nothing and compact the wrong queue. Returns the queue and position it
    was removed from, or None when the check-in has no entry any more.
    """
    row = (
        await db.execute(
            select(QueueEntry.id, QueueEntry.queue_type, QueueEntry.position).where(
                QueueEntry.session_id == session_id,
                QueueEntry.checkin_id == checkin_id,
            )
        )
    ).first()
    if row is None:
        return None
    await db.execute(delete(QueueEntry).where(QueueEntry.id == row.id))
    await compact_after_removal(db, session_id, row.queue_type, row.position)
    return row.queue_type, row.position
