"""``POST /v1/queue/promote``: a waiting entry moves to the bottom of the
active queue.

One transaction that starts by locking the session row (ADR-0006,
"Concurrency — pessimistic"). While the session is live the caps apply; a
refusal is a 400, a failed transaction a 409 ``conflict``, with no
server-side retry.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends
from mini_app_polis.logger import LOG_FAILURE, LOG_WARNING, get_logger, with_log_prefix
from sqlalchemy import delete, insert, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from ...auth import Caller, require_event_scope
from ...cache import invalidate_queue_cache
from ...clock import now_ms
from ...database import get_db_session
from ...errors import ErrorResponse, bad_request, conflict, not_found, success
from ...models import QueueEntry, Session
from ...queue import (
    PromotionGate,
    can_promote_priority,
    compact_after_removal,
    next_bottom_position,
)
from ...queue.audit import record_queue_event
from ...queue.fill import count_queue
from ...zod_types import zod_body
from .common import ENTRY_EVENT, MANAGE, ActionResponse, PromoteBody

logger = get_logger()

router = APIRouter(prefix="/v1/queue", tags=["queue"])


class _PromoteAbort(Exception):
    """Raised inside the promote transaction to answer a specific refusal."""

    def __init__(self, reason: str, gate: PromotionGate | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.gate = gate


def _promote_refusal(exc: _PromoteAbort) -> Exception | None:
    gate = exc.gate
    if exc.reason == "session_not_found":
        return not_found("Session")
    if gate is None:
        return None
    if exc.reason == "priority_cap":
        return bad_request(
            f"Active queue is at its priority cap "
            f"({gate.active_count}/{gate.active_priority_max} active)."
        )
    if exc.reason == "non_priority_cap_full":
        return bad_request(
            f"Active queue is at its standard cap "
            f"({gate.active_count}/{gate.active_non_priority_max} active). "
            "Finish or withdraw an active entry before promoting another "
            "standard entry."
        )
    if exc.reason == "non_priority_blocked_by_priority":
        noun = "entry" if gate.priority_count == 1 else "entries"
        return bad_request(
            f"Cannot promote a standard entry while the priority queue has "
            f"{gate.priority_count} waiting {noun}. Promote priority entries first."
        )
    return None


@router.post(
    "/promote",
    response_model=ActionResponse,
    summary="Promote a waiting entry",
    description=(
        "Requires deejaytools.queue.manage on the entry's event. Moves a "
        "priority or standard entry to the bottom of the active queue. While the "
        "session is live the caps apply: priority needs room under the priority "
        "cap; standard needs an empty priority queue and room under the standard "
        "cap. A completed or cancelled session skips the caps."
    ),
    responses={
        **MANAGE,
        400: {"model": ErrorResponse, "description": "Already active, or a cap."},
        404: {"model": ErrorResponse, "description": "No such entry or session."},
    },
)
async def promote(
    caller: Caller = Depends(
        require_event_scope("deejaytools.queue.manage", ENTRY_EVENT)
    ),
    body: PromoteBody = Depends(zod_body(PromoteBody)),
    db: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Promote an entry to active."""
    now = now_ms()
    entry = (
        await db.execute(
            select(
                QueueEntry.id,
                QueueEntry.checkin_id,
                QueueEntry.session_id,
                QueueEntry.entity_pair_id,
                QueueEntry.entity_solo_user_id,
                QueueEntry.entity_managed_partnership_id,
                QueueEntry.queue_type,
                QueueEntry.position,
            ).where(QueueEntry.id == body.queueEntryId)
        )
    ).first()
    if entry is None:
        raise not_found("Queue entry")
    if entry.queue_type == "active":
        raise bad_request("Entry is already active")

    await db.commit()
    try:
        session = (
            await db.execute(
                select(
                    Session.status,
                    Session.active_priority_max,
                    Session.active_non_priority_max,
                )
                .where(Session.id == entry.session_id)
                .with_for_update()
            )
        ).first()
        if session is None:
            raise _PromoteAbort("session_not_found")

        # Caps bind only a live session; a closed one may be cleared freely.
        if session.status not in ("completed", "cancelled"):
            gate = PromotionGate(
                active_count=await count_queue(db, entry.session_id, "active"),
                priority_count=await count_queue(db, entry.session_id, "priority"),
                active_priority_max=session.active_priority_max,
                active_non_priority_max=session.active_non_priority_max,
            )
            if entry.queue_type == "priority" and not can_promote_priority(gate):
                raise _PromoteAbort("priority_cap", gate)
            if entry.queue_type == "non_priority":
                if gate.priority_count > 0:
                    raise _PromoteAbort("non_priority_blocked_by_priority", gate)
                if gate.active_count >= gate.active_non_priority_max:
                    raise _PromoteAbort("non_priority_cap_full", gate)

        await db.execute(delete(QueueEntry).where(QueueEntry.id == entry.id))
        await compact_after_removal(
            db, entry.session_id, entry.queue_type, entry.position
        )
        new_position = await next_bottom_position(db, entry.session_id, "active")
        await db.execute(
            insert(QueueEntry).values(
                id=str(uuid.uuid4()),
                checkin_id=entry.checkin_id,
                session_id=entry.session_id,
                entity_pair_id=entry.entity_pair_id,
                entity_solo_user_id=entry.entity_solo_user_id,
                entity_managed_partnership_id=entry.entity_managed_partnership_id,
                queue_type="active",
                position=new_position,
                entered_queue_at=now,
            )
        )
        await record_queue_event(
            db,
            session_id=entry.session_id,
            checkin_id=entry.checkin_id,
            action="promoted_to_active",
            from_queue=entry.queue_type,
            from_position=entry.position,
            to_queue="active",
            to_position=new_position,
            actor_user_id=caller.user_id,
            reason=None,
            created_at=now,
        )
        await db.commit()
    except (
        SQLAlchemyError,
        _PromoteAbort,
    ) as exc:  # a refusal, or the transaction failed: a 409
        await db.rollback()
        if isinstance(exc, _PromoteAbort):
            if exc.gate is not None:
                g = exc.gate
                logger.warning(
                    with_log_prefix(
                        LOG_WARNING,
                        f"queue_promote_blocked entry={body.queueEntryId} "
                        f"session={entry.session_id} reason={exc.reason} "
                        f"active={g.active_count} priority={g.priority_count} "
                        f"priority_max={g.active_priority_max} "
                        f"non_priority_max={g.active_non_priority_max}",
                    )
                )
            refusal = _promote_refusal(exc)
            if refusal is not None:
                raise refusal from exc
        logger.error(
            with_log_prefix(
                LOG_FAILURE,
                f"queue_promote_failed entry={body.queueEntryId}: {exc!r}",
            )
        )
        raise conflict(
            "Promotion conflicted with concurrent activity; please retry"
        ) from exc

    invalidate_queue_cache(entry.session_id)
    return success({"promoted": True})
