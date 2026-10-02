"""The ``queue_events`` audit trail: append-only, one row per transition."""

from __future__ import annotations

import uuid

from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import QueueEvent


async def record_queue_event(
    db: AsyncSession,
    *,
    session_id: str,
    checkin_id: str,
    action: str,
    from_queue: str | None,
    from_position: int | None,
    to_queue: str | None,
    to_position: int | None,
    actor_user_id: str | None,
    reason: str | None,
    created_at: int,
    event_id: str | None = None,
) -> None:
    """Append one queue_events row. ``actor_user_id`` is None for the scheduler."""
    await db.execute(
        insert(QueueEvent).values(
            id=event_id or str(uuid.uuid4()),
            session_id=session_id,
            checkin_id=checkin_id,
            action=action,
            from_queue=from_queue,
            from_position=from_position,
            to_queue=to_queue,
            to_position=to_position,
            actor_user_id=actor_user_id,
            reason=reason,
            created_at=created_at,
        )
    )
