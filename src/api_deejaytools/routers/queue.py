"""``/v1/queue`` (deejaytools-api docs/API.md, src/routes/queue.ts).

Floor-manager actions on a session's queues, and the queue reads.

- promote, complete, incomplete, move-down, withdraw: behind
  ``deejaytools.queue.manage``. Each is one transaction that starts by
  locking the session row (ADR-005, "Concurrency — pessimistic"), except
  move-down, which Node runs without the lock. A failed transaction answers
  409 ``conflict``; there is no server-side retry.
- active and waiting: public (API-008). priority and non-priority: behind
  ``deejaytools.queue.read``. All four are cached for 3 s per session and
  invalidated by every mutation of that session.
"""

from __future__ import annotations

import time
import uuid
from typing import Annotated, Any, ClassVar, Literal

from fastapi import APIRouter, Depends, Path
from mini_app_polis.logger import (
    LOG_FAILURE,
    LOG_WARNING,
    get_logger,
    with_log_prefix,
)
from pydantic import BaseModel, Field
from sqlalchemy import delete, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from ..auth import Caller, require_scope
from ..cache import QUEUE_TTL_SECONDS, invalidate_queue_cache, response_cache
from ..database import get_db_session
from ..domain import full_name
from ..errors import ErrorCode, ErrorResponse, Meta, api_error, success
from ..models import (
    Checkin,
    ManagedPartnership,
    Pair,
    Partner,
    QueueEntry,
    Run,
    Session,
    Song,
    User,
)
from ..queue import (
    PromotionGate,
    can_promote_priority,
    compact_after_removal,
    fill_active_queue,
    lock_session_for_fill,
    next_bottom_position,
)
from ..queue.audit import record_queue_event
from ..queue.compaction import set_position
from ..queue.fill import count_queue
from ..queue.labels import managed_label, pair_label
from ..validation import NonEmptyStr, ZodModel
from ..zod_types import zod_body

logger = get_logger()

router = APIRouter(prefix="/v1/queue", tags=["queue"])


# Out of range of any real position, for rotate and move-down.
SWAP_SENTINEL = 2_000_000


class PromoteBody(ZodModel):
    """Body of ``POST /v1/queue/promote`` and ``/move-down``."""

    queueEntryId: NonEmptyStr = Field(..., description="Queue entry id.")


class EntryActionBody(ZodModel):
    """Body of ``/complete``, ``/incomplete`` and ``/withdraw``."""

    _NULLABLE: ClassVar[frozenset[str]] = frozenset({"reason"})

    queueEntryId: NonEmptyStr = Field(..., description="Queue entry id.")
    reason: str | None = Field(None, description="Recorded on the audit row.")


class QueueEntryData(BaseModel):
    """One queue entry with its check-in and a server-rendered entity label."""

    queueEntryId: str = Field(..., description="queue_entries.id.")
    checkinId: str = Field(..., description="Check-in id.")
    position: int = Field(..., description="1-based; lower is closer to the front.")
    enteredQueueAt: int = Field(
        ..., description="When it entered this queue, epoch ms."
    )
    entityPairId: str | None = Field(None, description="Pair entity.")
    entitySoloUserId: str | None = Field(None, description="Legacy solo entity.")
    entityManagedPartnershipId: str | None = Field(
        None, description="Managed partnership entity."
    )
    entityLabel: str = Field(..., description="'Leader & Follower', a name, or '—'.")
    divisionName: str = Field(..., description="Division.")
    songId: str | None = Field(None, description="Song id.")
    songDisplayName: str | None = Field(None, description="Song display name.")
    songProcessedFilename: str | None = Field(None, description="Processed filename.")
    notes: str | None = Field(None, description="Check-in notes.")
    initialQueue: str = Field(..., description="Queue the check-in was admitted to.")
    checkedInAt: int = Field(..., description="Check-in time, epoch ms.")


class WaitingEntryData(QueueEntryData):
    """A waiting entry, tagged with the queue it waits in."""

    subQueue: Literal["priority", "non_priority"] = Field(
        ..., description="Which waiting queue."
    )


class QueueListResponse(BaseModel):
    """A queue, front first. No meta.count."""

    data: list[QueueEntryData] = Field(..., description="The entries.")
    meta: Meta = Field(..., description="Response metadata.")


class WaitingListResponse(BaseModel):
    """Priority then standard waiting entries. No meta.count."""

    data: list[WaitingEntryData] = Field(..., description="The entries.")
    meta: Meta = Field(..., description="Response metadata.")


class ActionResponse(BaseModel):
    """Action confirmation: {'promoted'|'completed'|'rotated'|'moved'|'withdrawn': true}."""

    data: dict[str, bool] = Field(..., description="Which action succeeded.")
    meta: Meta = Field(..., description="Response metadata.")


class _PromoteAbort(Exception):
    """Raised inside the promote transaction to answer a specific refusal."""

    def __init__(self, reason: str, gate: PromotionGate | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.gate = gate


def _now_ms() -> int:
    return int(time.time() * 1000)


def _bad_request(message: str) -> Exception:
    return api_error(400, ErrorCode.BAD_REQUEST, message)


def _not_found(what: str) -> Exception:
    return api_error(404, ErrorCode.NOT_FOUND, f"{what} not found")


def _conflict(message: str) -> Exception:
    return api_error(409, "conflict", message)


def _promote_refusal(exc: _PromoteAbort) -> Exception | None:
    gate = exc.gate
    if exc.reason == "session_not_found":
        return _not_found("Session")
    if gate is None:
        return None
    if exc.reason == "priority_cap":
        return _bad_request(
            f"Active queue is at its priority cap "
            f"({gate.active_count}/{gate.active_priority_max} active)."
        )
    if exc.reason == "non_priority_cap_full":
        return _bad_request(
            f"Active queue is at its standard cap "
            f"({gate.active_count}/{gate.active_non_priority_max} active). "
            "Finish or withdraw an active entry before promoting another "
            "standard entry."
        )
    if exc.reason == "non_priority_blocked_by_priority":
        noun = "entry" if gate.priority_count == 1 else "entries"
        return _bad_request(
            f"Cannot promote a standard entry while the priority queue has "
            f"{gate.priority_count} waiting {noun}. Promote priority entries first."
        )
    return None


MANAGE: dict[int | str, dict[str, Any]] = {
    401: {"model": ErrorResponse, "description": "Missing or invalid token."},
    403: {"model": ErrorResponse, "description": "Lacks deejaytools.queue.manage."},
    409: {"model": ErrorResponse, "description": "Concurrent activity; retry."},
}


@router.post(
    "/promote",
    response_model=ActionResponse,
    summary="Promote a waiting entry",
    description=(
        "Requires deejaytools.queue.manage. Moves a priority or standard entry "
        "to the bottom of the active queue. While the session is live the caps "
        "apply: priority needs room under the priority cap; standard needs an "
        "empty priority queue and room under the standard cap. A completed or "
        "cancelled session skips the caps."
    ),
    responses={
        **MANAGE,
        400: {"model": ErrorResponse, "description": "Already active, or a cap."},
        404: {"model": ErrorResponse, "description": "No such entry or session."},
    },
)
async def promote(
    caller: Caller = Depends(require_scope("deejaytools.queue.manage")),
    body: PromoteBody = Depends(zod_body(PromoteBody)),
    db: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Promote an entry to active."""
    now = _now_ms()
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
        raise _not_found("Queue entry")
    if entry.queue_type == "active":
        raise _bad_request("Entry is already active")

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
    except Exception as exc:  # noqa: BLE001 - refusals mapped, anything else is a 409
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
        raise _conflict(
            "Promotion conflicted with concurrent activity; please retry"
        ) from exc

    invalidate_queue_cache(entry.session_id)
    return success({"promoted": True})


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
        "Requires deejaytools.queue.manage. Any active entry, not only "
        "position 1: records a run, removes the entry, closes the gap and "
        "auto-fills."
    ),
    responses={
        **MANAGE,
        400: {"model": ErrorResponse, "description": "Not an active entry."},
        404: {"model": ErrorResponse, "description": "Check-in missing."},
    },
)
async def complete(
    caller: Caller = Depends(require_scope("deejaytools.queue.manage")),
    body: EntryActionBody = Depends(zod_body(EntryActionBody)),
    db: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Mark an active entry's run complete."""
    admin_id = caller.user_id
    now = _now_ms()
    entry = await _load_active_entry(db, body.queueEntryId)
    if entry is None:
        raise _bad_request("Active queue entry not found")
    session_id = entry.session_id

    checkin = (
        await db.execute(
            select(Checkin.division_name, Checkin.song_id).where(
                Checkin.id == entry.checkin_id
            )
        )
    ).first()
    if checkin is None:
        raise _not_found("Check-in")
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
    except Exception as exc:  # noqa: BLE001 - any failure is a 409, as in Node
        await db.rollback()
        logger.error(
            with_log_prefix(
                LOG_FAILURE,
                f"queue_complete_failed entry={body.queueEntryId} "
                f"session={session_id}: {exc!r}",
            )
        )
        raise _conflict(
            "Completion conflicted with concurrent activity; please retry"
        ) from exc

    invalidate_queue_cache(session_id)
    return success({"completed": True})


class _EntryMissing(Exception):
    """The entry left the active queue between the read and the lock."""


@router.post(
    "/incomplete",
    response_model=ActionResponse,
    summary="Run incomplete",
    description=(
        "Requires deejaytools.queue.manage. Rotates an active entry to the "
        "bottom of the active queue (no run recorded) and auto-fills. An entry "
        "already at the bottom is left where it is."
    ),
    responses={
        **MANAGE,
        400: {"model": ErrorResponse, "description": "Not an active entry."},
    },
)
async def incomplete(
    caller: Caller = Depends(require_scope("deejaytools.queue.manage")),
    body: EntryActionBody = Depends(zod_body(EntryActionBody)),
    db: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Rotate an active entry to the bottom."""
    admin_id = caller.user_id
    now = _now_ms()
    entry = await _load_active_entry(db, body.queueEntryId)
    if entry is None:
        raise _bad_request("Active queue entry not found")
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
    except Exception as exc:  # noqa: BLE001 - any failure is a 409, as in Node
        await db.rollback()
        if isinstance(exc, _EntryMissing):
            raise _bad_request("Active queue entry not found") from exc
        logger.error(
            with_log_prefix(
                LOG_FAILURE,
                f"queue_incomplete_failed entry={body.queueEntryId} "
                f"session={session_id}: {exc!r}",
            )
        )
        raise _conflict(
            "Rotation conflicted with concurrent activity; please retry"
        ) from exc

    invalidate_queue_cache(session_id)
    return success({"rotated": True})


@router.post(
    "/move-down",
    response_model=ActionResponse,
    summary="Move an entry down one place",
    description=(
        "Requires deejaytools.queue.manage. Swaps the entry with the one at "
        "position + 1 in the same queue; entries never move between queues. "
        "One moved_within_queue audit row is written, for the initiating entry."
    ),
    responses={
        **MANAGE,
        400: {"model": ErrorResponse, "description": "Already at the bottom."},
        404: {"model": ErrorResponse, "description": "No such entry."},
    },
)
async def move_down(
    caller: Caller = Depends(require_scope("deejaytools.queue.manage")),
    body: PromoteBody = Depends(zod_body(PromoteBody)),
    db: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Swap an entry with the one below it."""
    now = _now_ms()
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
        raise _not_found("Queue entry")

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
        raise _bad_request("Entry is already at the bottom of its queue")

    await db.commit()
    try:
        # Node takes no session lock here; the unique index on
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
    except Exception as exc:  # noqa: BLE001 - any failure is a 409, as in Node
        await db.rollback()
        logger.error(
            with_log_prefix(
                LOG_FAILURE,
                f"queue_move_down_failed entry={body.queueEntryId} "
                f"session={entry.session_id}: {exc!r}",
            )
        )
        raise _conflict(
            "Move conflicted with concurrent activity; please retry"
        ) from exc

    invalidate_queue_cache(entry.session_id)
    return success({"moved": True})


@router.post(
    "/withdraw",
    response_model=ActionResponse,
    summary="Withdraw an entry",
    description=(
        "Requires deejaytools.queue.manage. Removes an entry from any queue "
        "without recording a run, closes the gap and auto-fills."
    ),
    responses={
        **MANAGE,
        404: {"model": ErrorResponse, "description": "No such entry."},
    },
)
async def withdraw(
    caller: Caller = Depends(require_scope("deejaytools.queue.manage")),
    body: EntryActionBody = Depends(zod_body(EntryActionBody)),
    db: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Withdraw an entry."""
    admin_id = caller.user_id
    now = _now_ms()
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
        raise _not_found("Queue entry")

    await db.commit()
    try:
        locked = await lock_session_for_fill(db, entry.session_id)
        await db.execute(delete(QueueEntry).where(QueueEntry.id == entry.id))
        await compact_after_removal(
            db, entry.session_id, entry.queue_type, entry.position
        )
        await record_queue_event(
            db,
            session_id=entry.session_id,
            checkin_id=entry.checkin_id,
            action="withdrawn",
            from_queue=entry.queue_type,
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
    except Exception as exc:  # noqa: BLE001 - any failure is a 409, as in Node
        await db.rollback()
        logger.error(
            with_log_prefix(
                LOG_FAILURE,
                f"queue_withdraw_failed entry={body.queueEntryId} "
                f"session={entry.session_id} queue={entry.queue_type} "
                f"position={entry.position}: {exc!r}",
            )
        )
        raise _conflict(
            "Withdraw conflicted with concurrent activity; please retry"
        ) from exc

    invalidate_queue_cache(entry.session_id)
    return success({"withdrawn": True})


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


async def list_queue(
    db: AsyncSession, session_id: str, queue_type: str
) -> list[dict[str, Any]]:
    """A queue's entries, front first, with check-in details and entity labels."""
    pair_user = aliased(User, name="pair_user")
    solo_user = aliased(User, name="solo_user")
    rows = (
        await db.execute(
            select(
                QueueEntry.id,
                QueueEntry.checkin_id,
                QueueEntry.position,
                QueueEntry.entered_queue_at,
                QueueEntry.entity_pair_id,
                QueueEntry.entity_solo_user_id,
                QueueEntry.entity_managed_partnership_id,
                Checkin.division_name,
                Checkin.song_id,
                Checkin.notes,
                Checkin.initial_queue,
                Checkin.created_at,
                pair_user.first_name.label("pair_user_first"),
                pair_user.last_name.label("pair_user_last"),
                Partner.first_name.label("partner_first"),
                Partner.last_name.label("partner_last"),
                Partner.kind.label("partner_kind"),
                solo_user.first_name.label("solo_first"),
                solo_user.last_name.label("solo_last"),
                ManagedPartnership.leader_first_name,
                ManagedPartnership.leader_last_name,
                ManagedPartnership.follower_first_name,
                ManagedPartnership.follower_last_name,
                Song.display_name.label("song_display_name"),
                Song.processed_filename.label("song_processed_filename"),
            )
            .select_from(QueueEntry)
            .join(Checkin, QueueEntry.checkin_id == Checkin.id)
            .outerjoin(Pair, Pair.id == QueueEntry.entity_pair_id)
            .outerjoin(pair_user, pair_user.id == Pair.user_a_id)
            .outerjoin(Partner, Partner.id == Pair.partner_b_id)
            .outerjoin(solo_user, solo_user.id == QueueEntry.entity_solo_user_id)
            .outerjoin(
                ManagedPartnership,
                ManagedPartnership.id == QueueEntry.entity_managed_partnership_id,
            )
            .outerjoin(Song, Song.id == Checkin.song_id)
            .where(
                QueueEntry.session_id == session_id, QueueEntry.queue_type == queue_type
            )
            .order_by(QueueEntry.position.asc())
        )
    ).all()

    out = []
    for r in rows:
        if r.entity_managed_partnership_id and r.leader_first_name is not None:
            label = managed_label(
                r.leader_first_name,
                r.leader_last_name,
                r.follower_first_name,
                r.follower_last_name,
            )
        elif r.entity_pair_id and (r.pair_user_first or r.pair_user_last):
            label = pair_label(
                r.pair_user_first,
                r.pair_user_last,
                r.partner_first,
                r.partner_last,
                r.partner_kind,
            )
        elif r.entity_solo_user_id and (r.solo_first or r.solo_last):
            label = full_name(r.solo_first, r.solo_last)
        else:
            label = "—"
        out.append(
            {
                "queueEntryId": r.id,
                "checkinId": r.checkin_id,
                "position": r.position,
                "enteredQueueAt": r.entered_queue_at,
                "entityPairId": r.entity_pair_id,
                "entitySoloUserId": r.entity_solo_user_id,
                "entityManagedPartnershipId": r.entity_managed_partnership_id,
                "entityLabel": label,
                "divisionName": r.division_name,
                "songId": r.song_id,
                "songDisplayName": r.song_display_name,
                "songProcessedFilename": r.song_processed_filename,
                "notes": r.notes,
                "initialQueue": r.initial_queue,
                "checkedInAt": r.created_at,
            }
        )
    return out


async def _cached(key: str, build: Any) -> dict[str, Any]:
    cached = response_cache.get(key)
    if cached is not None:
        return cached  # type: ignore[no-any-return]
    result = success(await build())
    response_cache.set(key, result, QUEUE_TTL_SECONDS)
    return result


SessionId = Annotated[str, Path(description="Session id.")]
READ: dict[int | str, dict[str, Any]] = {
    401: {"model": ErrorResponse, "description": "Missing or invalid token."},
    403: {"model": ErrorResponse, "description": "Lacks deejaytools.queue.read."},
}


@router.get(
    "/{session_id}/active",
    response_model=QueueListResponse,
    summary="Active queue",
    description=(
        "The active queue, position 1 first (position 1 is on the floor). "
        "Intentionally public: the dancer-facing session page polls it. "
        "Cached for 3 s."
    ),
)
async def active_queue(
    session_id: SessionId, db: AsyncSession = Depends(get_db_session)
) -> dict[str, Any]:
    """List the active queue. Public."""
    return await _cached(
        f"queue:{session_id}:active", lambda: list_queue(db, session_id, "active")
    )


@router.get(
    "/{session_id}/waiting",
    response_model=WaitingListResponse,
    summary="Waiting queues",
    description=(
        "Priority entries then standard entries, each tagged with subQueue. "
        "Intentionally public: the dancer-facing session page polls it. "
        "Cached for 3 s."
    ),
)
async def waiting_queue(
    session_id: SessionId, db: AsyncSession = Depends(get_db_session)
) -> dict[str, Any]:
    """List both waiting queues. Public."""

    async def build() -> list[dict[str, Any]]:
        priority = await list_queue(db, session_id, "priority")
        standard = await list_queue(db, session_id, "non_priority")
        return [{**r, "subQueue": "priority"} for r in priority] + [
            {**r, "subQueue": "non_priority"} for r in standard
        ]

    return await _cached(f"queue:{session_id}:waiting", build)


@router.get(
    "/{session_id}/priority",
    response_model=QueueListResponse,
    summary="Priority queue",
    description="Requires deejaytools.queue.read. The priority queue. Cached for 3 s.",
    responses=READ,
)
async def priority_queue(
    session_id: SessionId,
    _caller: Caller = Depends(require_scope("deejaytools.queue.read")),
    db: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """List the priority queue."""
    return await _cached(
        f"queue:{session_id}:priority", lambda: list_queue(db, session_id, "priority")
    )


@router.get(
    "/{session_id}/non-priority",
    response_model=QueueListResponse,
    summary="Standard queue",
    description="Requires deejaytools.queue.read. The standard queue. Cached for 3 s.",
    responses=READ,
)
async def non_priority_queue(
    session_id: SessionId,
    _caller: Caller = Depends(require_scope("deejaytools.queue.read")),
    db: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """List the standard queue."""
    return await _cached(
        f"queue:{session_id}:non-priority",
        lambda: list_queue(db, session_id, "non_priority"),
    )
