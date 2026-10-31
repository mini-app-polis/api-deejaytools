"""``POST /v1/queue/move-down`` and ``/withdraw``: an entry's place in any
queue.

Move-down swaps an entry with the one below it in its own queue, without
the session lock: the unique index on (session, queue, position) refuses a
racing change. Withdraw removes an entry from whichever queue holds it in
one transaction that starts by locking the session row (ADR-0006,
"Concurrency — pessimistic") and auto-fills the active queue. A failed
transaction answers 409 ``conflict``; there is no server-side retry.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from mini_app_polis.logger import LOG_FAILURE, get_logger, with_log_prefix
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from ...auth import Caller, require_event_scope
from ...cache import invalidate_queue_cache
from ...clock import now_ms
from ...database import get_db_session
from ...errors import ErrorResponse, bad_request, conflict, not_found, success
from ...models import QueueEntry
from ...queue import fill_active_queue, lock_session_for_fill, remove_entry_of_checkin
from ...queue.audit import record_queue_event
from ...queue.compaction import set_position
from ...zod_types import zod_body
from .common import (
    ENTRY_EVENT,
    MANAGE,
    SWAP_SENTINEL,
    ActionResponse,
    EntryActionBody,
    PromoteBody,
    _EntryMissing,
)

logger = get_logger()

router = APIRouter(prefix="/v1/queue", tags=["queue"])


@router.post(
    "/move-down",
    response_model=ActionResponse,
    summary="Move an entry down one place",
    description=(
        "Requires deejaytools.queue.manage on the entry's event. Swaps the entry "
        "with the one at position + 1 in the same queue; entries never move "
        "between queues. One moved_within_queue audit row is written, for the "
        "initiating entry."
    ),
    responses={
        **MANAGE,
        400: {"model": ErrorResponse, "description": "Already at the bottom."},
        404: {"model": ErrorResponse, "description": "No such entry."},
    },
)
async def move_down(
    caller: Caller = Depends(
        require_event_scope("deejaytools.queue.manage", ENTRY_EVENT)
    ),
    body: PromoteBody = Depends(zod_body(PromoteBody)),
    db: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Swap an entry with the one below it."""
    now = now_ms()
    entry = (
        await db.execute(
            select(
                QueueEntry.id,
                QueueEntry.checkin_id,
                QueueEntry.session_id,
                QueueEntry.queue_type,
                QueueEntry.position,
            ).where(QueueEntry.id == body.queueEntryId)
        )
    ).first()
    if entry is None:
        raise not_found("Queue entry")

    below = (
        await db.execute(
            select(QueueEntry.id, QueueEntry.position).where(
                QueueEntry.session_id == entry.session_id,
                QueueEntry.queue_type == entry.queue_type,
                QueueEntry.position == entry.position + 1,
            )
        )
    ).first()
    if below is None:
        raise bad_request("Entry is already at the bottom of its queue")

    await db.commit()
    try:
        # No session lock here; the unique index on
        # (session, queue, position) is what refuses a racing change.
        await set_position(db, entry.id, SWAP_SENTINEL)
        await set_position(db, below.id, entry.position)
        await set_position(db, entry.id, below.position)
        await record_queue_event(
            db,
            session_id=entry.session_id,
            checkin_id=entry.checkin_id,
            action="moved_within_queue",
            from_queue=entry.queue_type,
            from_position=entry.position,
            to_queue=entry.queue_type,
            to_position=below.position,
            actor_user_id=caller.user_id,
            reason=None,
            created_at=now,
        )
        await db.commit()
    except SQLAlchemyError as exc:  # the transaction failed: a 409
        await db.rollback()
        logger.error(
            with_log_prefix(
                LOG_FAILURE,
                f"queue_move_down_failed entry={body.queueEntryId} "
                f"session={entry.session_id}: {exc!r}",
            )
        )
        raise conflict(
            "Move conflicted with concurrent activity; please retry"
        ) from exc

    invalidate_queue_cache(entry.session_id)
    return success({"moved": True})


@router.post(
    "/withdraw",
    response_model=ActionResponse,
    summary="Withdraw an entry",
    description=(
        "Requires deejaytools.queue.manage on the entry's event. Removes an "
        "entry from any queue without recording a run, closes the gap and "
        "auto-fills."
    ),
    responses={
        **MANAGE,
        404: {"model": ErrorResponse, "description": "No such entry."},
    },
)
async def withdraw(
    caller: Caller = Depends(
        require_event_scope("deejaytools.queue.manage", ENTRY_EVENT)
    ),
    body: EntryActionBody = Depends(zod_body(EntryActionBody)),
    db: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Withdraw an entry."""
    admin_id = caller.user_id
    now = now_ms()
    entry = (
        await db.execute(
            select(
                QueueEntry.id,
                QueueEntry.checkin_id,
                QueueEntry.session_id,
                QueueEntry.queue_type,
                QueueEntry.position,
            ).where(QueueEntry.id == body.queueEntryId)
        )
    ).first()
    if entry is None:
        raise not_found("Queue entry")

    await db.commit()
    try:
        locked = await lock_session_for_fill(db, entry.session_id)
        removed = await remove_entry_of_checkin(db, entry.session_id, entry.checkin_id)
        if removed is None:
            raise _EntryMissing()
        from_queue, from_position = removed
        await record_queue_event(
            db,
            session_id=entry.session_id,
            checkin_id=entry.checkin_id,
            action="withdrawn",
            from_queue=from_queue,
            from_position=from_position,
            to_queue=None,
            to_position=None,
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
            raise not_found("Queue entry") from exc
        logger.error(
            with_log_prefix(
                LOG_FAILURE,
                f"queue_withdraw_failed entry={body.queueEntryId} "
                f"session={entry.session_id} queue={entry.queue_type} "
                f"position={entry.position}: {exc!r}",
            )
        )
        raise conflict(
            "Withdraw conflicted with concurrent activity; please retry"
        ) from exc

    invalidate_queue_cache(entry.session_id)
    return success({"withdrawn": True})
