"""The one-live-entry rule (deejaytools-api lib/queue/singleEntry.ts)."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import QueueEntry
from .run_counts import EntityRef, entity_filter


async def entity_has_live_entry(
    db: AsyncSession, entity: EntityRef, session_id: str
) -> bool:
    """Whether ``entity`` already has a queue_entries row in this session.

    The partial unique indexes enforce this too; checking first answers a
    clean 409 instead of an integrity error.
    """
    row = (
        await db.execute(
            select(QueueEntry.id)
            .where(
                QueueEntry.session_id == session_id, entity_filter(QueueEntry, entity)
            )
            .limit(1)
        )
    ).first()
    return row is not None
