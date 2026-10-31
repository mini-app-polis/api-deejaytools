"""``POST /v1/queue/complete`` and ``/incomplete``: the outcome of an active
entry's run.

Complete records a run and removes the entry; incomplete rotates it to the
bottom of the active queue. Each is one transaction that starts by locking
the session row (ADR-0006, "Concurrency — pessimistic") and auto-fills the
active queue. A failed transaction answers 409 ``conflict``; there is no
server-side retry.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends
from mini_app_polis.logger import LOG_FAILURE, get_logger, with_log_prefix
from sqlalchemy import delete, insert, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from ...auth import Caller, require_event_scope
from ...cache import invalidate_queue_cache
from ...clock import now_ms
from ...database import get_db_session
from ...errors import ErrorResponse, bad_request, conflict, not_found, success
from ...models import Checkin, QueueEntry, Run, Session
from ...queue import compact_after_removal, fill_active_queue, lock_session_for_fill
from ...queue.audit import record_queue_event
from ...queue.compaction import set_position
from ...zod_types import zod_body
from .common import (
    ENTRY_EVENT,
    MANAGE,
    SWAP_SENTINEL,
    ActionResponse,
    EntryActionBody,
    _EntryMissing,
)

logger = get_logger()

router = APIRouter(prefix="/v1/queue", tags=["queue"])


async def _load_active_entry(db: AsyncSession, queue_entry_id: str) -> Any:
    return (
        await db.execute(
            select(
                QueueEntry.id,
                QueueEntry.checkin_id,
                QueueEntry.session_id,
                QueueEntry.entity_pair_id,
                QueueEntry.entity_solo_user_id,
                QueueEntry.entity_managed_partnership_id,
                QueueEntry.position,
            ).where(QueueEntry.id == queue_entry_id, QueueEntry.queue_type == "active")
        )
    ).first()


@router.post(
    "/complete",
    response_model=ActionResponse,
    summary="Run complete",
    description=(
        "Requires deejaytools.queue.manage on the entry's event. Any active "
        "entry, not only position 1: records a run, removes the entry, closes "
        "the gap and auto-fills."
    ),
    responses={
        **MANAGE,
        400: {"model": ErrorResponse, "description": "Not an active entry."},
        404: {"model": ErrorResponse, "description": "Check-in missing."},
    },
)
async def complete(
    caller: Caller = Depends(
        require_event_scope("deejaytools.queue.manage", ENTRY_EVENT)
    ),
    body: EntryActionBody = Depends(zod_body(EntryActionBody)),
    db: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Mark an active entry's run complete."""
    admin_id = caller.user_id
    now = now_ms()
    entry = await _load_active_entry(db, body.queueEntryId)
    if entry is None:
        raise bad_request("Active queue entry not found")
    session_id = entry.session_id

    checkin = (
        await db.execute(
            select(Checkin.division_name, Checkin.song_id).where(
                Checkin.id == entry.checkin_id
            )
        )
    ).first()
    if checkin is None:
        raise not_found("Check-in")
    event_id = (
        await db.execute(select(Session.event_id).where(Session.id == session_id))
    ).scalar_one_or_none()

    await db.commit()
    try:
        locked = await lock_session_for_fill(db, session_id)
        await db.execute(delete(QueueEntry).where(QueueEntry.id == entry.id))
        await compact_after_removal(db, session_id, "active", entry.position)
        await db.execute(
            insert(Run).values(
                id=str(uuid.uuid4()),
                checkin_id=entry.checkin_id,
                session_id=session_id,
                event_id=event_id,
                division_name=checkin.division_name,
                entity_pair_id=entry.entity_pair_id,
                entity_solo_user_id=entry.entity_solo_user_id,
                entity_managed_partnership_id=entry.entity_managed_partnership_id,
                song_id=checkin.song_id,
                completed_at=now,
                completed_by_user_id=admin_id,
            )
        )
        await record_queue_event(
            db,
            session_id=session_id,
            checkin_id=entry.checkin_id,
            action="run_completed",
            from_queue="active",
            from_position=entry.position,
            to_queue=None,
            to_position=None,
            actor_user_id=admin_id,
            reason=body.reason,
            created_at=now,
        )
        if locked is not None:
            await fill_active_queue(db, locked, admin_id, now)
        await db.commit()
    except SQLAlchemyError as exc:  # the transaction failed: a 409
        await db.rollback()
        logger.error(
            with_log_prefix(
                LOG_FAILURE,
                f"queue_complete_failed entry={body.queueEntryId} "
                f"session={session_id}: {exc!r}",
            )
        )
        raise conflict(
            "Completion conflicted with concurrent activity; please retry"
        ) from exc

    invalidate_queue_cache(session_id)
    return success({"completed": True})


@router.post(
    "/incomplete",
    response_model=ActionResponse,
    summary="Run incomplete",
    description=(
        "Requires deejaytools.queue.manage on the entry's event. Rotates an "
        "active entry to the bottom of the active queue (no run recorded) and "
        "auto-fills. An entry already at the bottom is left where it is."
    ),
    responses={
        **MANAGE,
        400: {"model": ErrorResponse, "description": "Not an active entry."},
    },
)
async def incomplete(
    caller: Caller = Depends(
        require_event_scope("deejaytools.queue.manage", ENTRY_EVENT)
    ),
    body: EntryActionBody = Depends(zod_body(EntryActionBody)),
    db: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Rotate an active entry to the bottom."""
    admin_id = caller.user_id
    now = now_ms()
    entry = await _load_active_entry(db, body.queueEntryId)
    if entry is None:
        raise bad_request("Active queue entry not found")
    session_id = entry.session_id

    await db.commit()
    try:
        locked = await lock_session_for_fill(db, session_id)
        rows = (
            await db.execute(
                select(QueueEntry.id, QueueEntry.position)
                .where(
                    QueueEntry.session_id == session_id,
                    QueueEntry.queue_type == "active",
                )
                .order_by(QueueEntry.position.asc())
            )
        ).all()
        target = next((r for r in rows if r.id == entry.id), None)
        if target is None:
            raise _EntryMissing()

        n = len(rows)
        # Already at the bottom: nothing to rotate, no audit row, no fill.
        if target.position != n:
            target_pos = target.position
            await set_position(db, target.id, SWAP_SENTINEL)
            for r in rows:
                if r.position > target_pos:
                    await set_position(db, r.id, r.position - 1)
            await db.execute(
                update(QueueEntry)
                .where(QueueEntry.id == target.id)
                .values(position=n, entered_queue_at=now)
                .execution_options(synchronize_session=False)
            )
            await record_queue_event(
                db,
                session_id=session_id,
                checkin_id=entry.checkin_id,
                action="run_incomplete_rotated",
                from_queue="active",
                from_position=target_pos,
                to_queue="active",
                to_position=n,
                actor_user_id=admin_id,
                reason=body.reason,
                created_at=now,
            )
            if locked is not None:
                await fill_active_queue(db, locked, admin_id, now)
        await db.commit()
    except (
        SQLAlchemyError,
        _EntryMissing,
    ) as exc:  # a refusal, or the transaction failed: a 409
        await db.rollback()
        if isinstance(exc, _EntryMissing):
            raise bad_request("Active queue entry not found") from exc
        logger.error(
            with_log_prefix(
                LOG_FAILURE,
                f"queue_incomplete_failed entry={body.queueEntryId} "
                f"session={session_id}: {exc!r}",
            )
        )
        raise conflict(
            "Rotation conflicted with concurrent activity; please retry"
        ) from exc

    invalidate_queue_cache(session_id)
    return success({"rotated": True})
