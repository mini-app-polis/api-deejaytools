"""Which waiting queue a check-in enters, and the promotion gates
(deejaytools-api lib/queue/admission.ts).

Priority requires all of: the session division is a priority division; the
entity's completed runs in that division for the session are below its
``priority_run_limit``; and, when the event sets a limit for the division,
the entity's runs in that division across the event are below it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Session, SessionDivision, event_division_run_limits
from .run_counts import EntityRef, runs_for_entity_in_event, runs_for_entity_in_session

InitialQueue = Literal["priority", "non_priority"]


class AdmissionError(Exception):
    """The session or division a check-in names cannot be admitted to.

    Its message is answered verbatim as a 400, as Node answers the Error
    that ``loadAdmissionContext`` throws.
    """


@dataclass(frozen=True, slots=True)
class AdmissionContext:
    """What the admission predicate needs for one session and division."""

    session_id: str
    event_id: str | None
    division_name: str
    is_division_priority: bool
    session_priority_run_limit: int
    event_priority_run_limit: int | None


async def determine_initial_queue(
    db: AsyncSession, entity: EntityRef, ctx: AdmissionContext
) -> InitialQueue:
    """The queue a fresh check-in lands in: priority or non_priority."""
    if not ctx.is_division_priority:
        return "non_priority"

    session_runs = await runs_for_entity_in_session(
        db, entity, ctx.session_id, ctx.division_name
    )
    if session_runs >= ctx.session_priority_run_limit:
        return "non_priority"

    if ctx.event_priority_run_limit is not None and ctx.event_id:
        event_runs = await runs_for_entity_in_event(
            db, entity, ctx.event_id, ctx.division_name
        )
        if event_runs >= ctx.event_priority_run_limit:
            return "non_priority"

    return "priority"


async def load_admission_context(
    db: AsyncSession, session_id: str, division_name: str
) -> AdmissionContext:
    """Load the admission context. Raises ``AdmissionError`` when the session
    does not exist or does not run the division."""
    session = (
        await db.execute(
            select(Session.id, Session.event_id).where(Session.id == session_id)
        )
    ).first()
    if session is None:
        raise AdmissionError("Session not found")

    division = (
        await db.execute(
            select(
                SessionDivision.is_priority, SessionDivision.priority_run_limit
            ).where(
                SessionDivision.session_id == session_id,
                SessionDivision.division_name == division_name,
            )
        )
    ).first()
    if division is None:
        raise AdmissionError("Division not configured for this session")

    event_limit: int | None = None
    if session.event_id:
        limit_row = (
            await db.execute(
                select(event_division_run_limits.c.priority_run_limit).where(
                    event_division_run_limits.c.event_id == session.event_id,
                    event_division_run_limits.c.division_name == division_name,
                )
            )
        ).first()
        event_limit = limit_row[0] if limit_row is not None else None

    return AdmissionContext(
        session_id=session_id,
        event_id=session.event_id,
        division_name=division_name,
        is_division_priority=division.is_priority,
        session_priority_run_limit=division.priority_run_limit,
        event_priority_run_limit=event_limit,
    )


@dataclass(frozen=True, slots=True)
class PromotionGate:
    """Queue counts and caps, read under the session lock."""

    active_count: int
    priority_count: int
    active_priority_max: int
    active_non_priority_max: int


def can_promote_priority(g: PromotionGate) -> bool:
    """A priority entry can go active now."""
    return g.active_count < g.active_priority_max


def can_promote_non_priority(g: PromotionGate) -> bool:
    """A standard entry can go active now: room under the standard cap and
    nobody waiting in priority."""
    return g.active_count < g.active_non_priority_max and g.priority_count == 0
