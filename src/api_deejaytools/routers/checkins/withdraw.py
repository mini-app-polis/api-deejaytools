"""``DELETE /v1/checkins/{id}``: a dancer withdraws their own check-in.

Behind ``deejaytools.checkins.write``. A queue transaction: it locks the
session row first, removes the entry, and auto-fills the active queue
(ADR-0006).
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path
from mini_app_polis.logger import LOG_FAILURE, get_logger, with_log_prefix
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from ...auth import Caller, require_scope
from ...cache import invalidate_queue_cache, invalidate_session_cache
from ...clock import now_ms
from ...database import get_db_session
from ...errors import (
    ErrorCode,
    ErrorResponse,
    Meta,
    api_error,
    conflict,
    forbidden,
    success,
)
from ...models import Checkin, ManagedPartnership, Pair, QueueEntry
from ...queue import fill_active_queue, lock_session_for_fill, remove_entry_of_checkin
from ...queue.audit import record_queue_event
from .common import AUTH

logger = get_logger()

router = APIRouter(prefix="/v1/checkins", tags=["checkins"])


class _CheckinLeftQueue(Exception):
    """The check-in had no queue entry once the session was locked."""


class WithdrawnResponse(BaseModel):
    """Withdraw confirmation."""

    data: dict[str, bool] = Field(..., description="{'withdrawn': true}.")
    meta: Meta = Field(..., description="Response metadata.")


@router.delete(
    "/{id}",
    response_model=WithdrawnResponse,
    summary="Withdraw my check-in",
    description=(
        "Requires deejaytools.checkins.write, and the entity must be the "
        "caller's (solo, a pair they lead, or a managed partnership they own). "
        "Removes the queue entry, closes the gap, records 'withdrawn' "
        "(reason self_withdrew) and auto-fills, as an admin withdraw does."
    ),
    responses={
        **AUTH,
        404: {"model": ErrorResponse, "description": "No live check-in."},
        409: {"model": ErrorResponse, "description": "Concurrent activity."},
    },
)
async def withdraw_my_checkin(
    id: Annotated[str, Path(description="Check-in id.")],
    caller: Caller = Depends(require_scope("deejaytools.checkins.write")),
    db: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Withdraw one of the caller's own check-ins."""
    user_id = caller.user_id
    now = now_ms()

    row = (
        await db.execute(
            select(
                Checkin.id.label("checkin_id"),
                Checkin.session_id,
                QueueEntry.entity_pair_id,
                QueueEntry.entity_solo_user_id,
                QueueEntry.entity_managed_partnership_id,
                QueueEntry.id.label("queue_entry_id"),
                QueueEntry.queue_type,
                QueueEntry.position,
            )
            .select_from(Checkin)
            .join(QueueEntry, QueueEntry.checkin_id == Checkin.id)
            .where(Checkin.id == id)
            .limit(1)
        )
    ).first()
    if row is None:
        raise api_error(404, ErrorCode.NOT_FOUND, "Check-in not found")

    owned = row.entity_solo_user_id == user_id
    if not owned and row.entity_pair_id:
        owner = (
            await db.execute(
                select(Pair.user_a_id).where(Pair.id == row.entity_pair_id).limit(1)
            )
        ).scalar_one_or_none()
        owned = owner == user_id
    if not owned and row.entity_managed_partnership_id:
        owner = (
            await db.execute(
                select(ManagedPartnership.user_id)
                .where(ManagedPartnership.id == row.entity_managed_partnership_id)
                .limit(1)
            )
        ).scalar_one_or_none()
        owned = owner == user_id
    if not owned:
        raise forbidden()

    await db.commit()
    try:
        locked = await lock_session_for_fill(db, row.session_id)
        removed = await remove_entry_of_checkin(db, row.session_id, row.checkin_id)
        if removed is None:
            raise _CheckinLeftQueue()
        from_queue, from_position = removed
        await record_queue_event(
            db,
            session_id=row.session_id,
            checkin_id=row.checkin_id,
            action="withdrawn",
            from_queue=from_queue,
            from_position=from_position,
            to_queue=None,
            to_position=None,
            actor_user_id=user_id,
            reason="self_withdrew",
            created_at=now,
        )
        if locked is not None:
            await fill_active_queue(db, locked, user_id, now)
        await db.commit()
    except (
        SQLAlchemyError,
        _CheckinLeftQueue,
    ) as exc:  # a refusal, or the transaction failed: a 409
        await db.rollback()
        if isinstance(exc, _CheckinLeftQueue):
            raise api_error(404, ErrorCode.NOT_FOUND, "Check-in not found") from exc
        logger.error(
            with_log_prefix(
                LOG_FAILURE,
                f"checkin_self_withdraw_failed checkin={id} user={user_id}: {exc!r}",
            )
        )
        raise conflict(
            "Withdraw conflicted with concurrent activity; please retry"
        ) from exc

    invalidate_session_cache(row.session_id)
    invalidate_queue_cache(row.session_id)
    return success({"withdrawn": True})
