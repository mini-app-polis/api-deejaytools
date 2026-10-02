"""Completed runs per entity and division (deejaytools-api lib/queue/runCounts.ts)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Run


@dataclass(frozen=True, slots=True)
class EntityRef:
    """One competing entity: a pair, a managed partnership, or (legacy) a solo user.

    Exactly one is set. Solo is read-only (ADR-005): no new code writes it,
    but historical rows keep counting.
    """

    pair_id: str | None = None
    solo_user_id: str | None = None
    managed_partnership_id: str | None = None


def entity_filter(column_owner: Any, entity: EntityRef) -> Any:
    """The predicate matching ``entity`` on a table with the three entity columns.

    Branch order is Node's: pair, then managed partnership, then solo.
    """
    if entity.pair_id:
        return column_owner.entity_pair_id == entity.pair_id
    if entity.managed_partnership_id:
        return (
            column_owner.entity_managed_partnership_id == entity.managed_partnership_id
        )
    return column_owner.entity_solo_user_id == entity.solo_user_id


async def runs_for_entity_in_session(
    db: AsyncSession, entity: EntityRef, session_id: str, division_name: str
) -> int:
    """Completed runs for ``entity`` in ``division_name`` within one session."""
    n = (
        await db.execute(
            select(func.count())
            .select_from(Run)
            .where(
                Run.session_id == session_id,
                Run.division_name == division_name,
                entity_filter(Run, entity),
            )
        )
    ).scalar()
    return int(n or 0)


async def runs_for_entity_in_event(
    db: AsyncSession, entity: EntityRef, event_id: str, division_name: str
) -> int:
    """Completed runs for ``entity`` in ``division_name`` across an event's sessions."""
    n = (
        await db.execute(
            select(func.count())
            .select_from(Run)
            .where(
                Run.event_id == event_id,
                Run.division_name == division_name,
                entity_filter(Run, entity),
            )
        )
    ).scalar()
    return int(n or 0)
