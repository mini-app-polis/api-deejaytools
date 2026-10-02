"""``/v1/checkins`` (deejaytools-api docs/API.md, src/routes/checkins.ts).

A dancer checks an entity into a session's waiting queue, lists their live
check-ins, and withdraws their own. Writes are behind
``deejaytools.checkins.write``, the list behind ``deejaytools.checkins.read``;
an admin may check in for someone else with ``on_behalf_of_user_id``, which
is a second decision (``deejaytools.delegation.act``, ADR-007).

Check-in and withdraw are queue transactions: they lock the session row
first, change the queue, and auto-fill the active queue (ADR-005).
"""

from __future__ import annotations

import time
import uuid
from typing import Annotated, Any, ClassVar, Literal

from fastapi import APIRouter, Depends, Path, Request
from mini_app_polis.logger import LOG_FAILURE, get_logger, with_log_prefix
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import and_, delete, desc, func, insert, or_, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from ..auth import Caller, authorize_delegation, require_scope
from ..cache import invalidate_queue_cache, invalidate_session_cache
from ..database import get_db_session
from ..db_errors import driver_message
from ..errors import ErrorCode, ErrorResponse, Meta, api_error, forbidden, success
from ..models import (
    Checkin,
    Event,
    EventSongSubmission,
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
    AdmissionError,
    EntityRef,
    InitialQueue,
    compact_after_removal,
    determine_initial_queue,
    entity_has_live_entry,
    fill_active_queue,
    load_admission_context,
    lock_session_for_fill,
    next_bottom_position,
)
from ..queue.audit import record_queue_event
from ..queue.labels import managed_label, pair_label
from ..validation import NonEmptyStr, ZodModel
from ..zod_coerce import js_trim
from ..zod_types import trimmed, zod_body

logger = get_logger()

router = APIRouter(prefix="/v1/checkins", tags=["checkins"])


NO_ATTACHABLE_ENTITY_MSG = (
    "This song has no partner or managed partnership. "
    "Attach one on the song before checking in."
)
NOT_SUBMITTED_MSG = (
    "This song hasn't been submitted to this event. "
    "Add it to the event on My Content before checking in."
)
LIVE_ENTRY_MSG = "This entity already has a live queue entry in this session"


class CreateCheckinBody(ZodModel):
    """Body of ``POST /v1/checkins`` (createCheckinBodySchema).

    Exactly one of ``entityPairId`` / ``entityManagedPartnershipId``, or none
    when an admin checks in on someone's behalf: the server then derives
    the entity from the song.
    """

    _NULLABLE: ClassVar[frozenset[str]] = frozenset(
        {"entityPairId", "entityManagedPartnershipId", "on_behalf_of_user_id", "notes"}
    )

    sessionId: NonEmptyStr = Field(..., description="Session to check into.")
    divisionName: NonEmptyStr = Field(
        ..., description="Division, as the session runs it."
    )
    entityPairId: str | None = Field(None, description="Pair the caller leads.")
    entityManagedPartnershipId: str | None = Field(
        None, description="Managed partnership; only valid when the song is managed."
    )
    on_behalf_of_user_id: Annotated[str, trimmed()] | None = Field(
        None, description="Admins only: the user to check in for (trimmed)."
    )
    songId: NonEmptyStr = Field(..., description="The song to dance to.")
    notes: str | None = Field(None, description="Notes for the floor manager.")

    @model_validator(mode="after")
    def _one_entity(self) -> CreateCheckinBody:
        entity_count = len(
            [v for v in (self.entityPairId, self.entityManagedPartnershipId) if v]
        )
        ok = entity_count == 0 if self.on_behalf_of_user_id else entity_count == 1
        if not ok:
            raise ValueError(
                "Exactly one of entityPairId or entityManagedPartnershipId must be "
                "provided (or none when checking in on behalf)"
            )
        return self


class CheckinCreatedData(BaseModel):
    """The new check-in."""

    id: str = Field(..., description="Check-in id.")
    sessionId: str = Field(..., description="Session id.")
    divisionName: str = Field(..., description="Division.")
    initialQueue: Literal["priority", "non_priority"] = Field(
        ..., description="Queue it was admitted to."
    )


class CheckinCreatedResponse(BaseModel):
    """Check-in created."""

    data: CheckinCreatedData = Field(..., description="The check-in.")
    meta: Meta = Field(..., description="Response metadata.")


class MyCheckinData(BaseModel):
    """One of the caller's live check-ins, with its place in line."""

    id: str = Field(..., description="Check-in id.")
    sessionId: str = Field(..., description="Session id.")
    eventName: str | None = Field(None, description="Event name, if any.")
    sessionName: str = Field(..., description="Session name.")
    sessionFloorTrialStartsAt: int = Field(..., description="Epoch ms.")
    sessionStatus: str = Field(..., description="The session's stored status.")
    eventTimezone: str | None = Field(None, description="Event timezone, if any.")
    divisionName: str = Field(..., description="Division.")
    entityPairId: str | None = Field(None, description="Pair entity.")
    entitySoloUserId: str | None = Field(None, description="Legacy solo entity.")
    entityManagedPartnershipId: str | None = Field(
        None, description="Managed partnership entity."
    )
    entityLabel: str = Field(..., description="Who is dancing; 'Solo' as a fallback.")
    songDisplayName: str | None = Field(None, description="Song display name.")
    songProcessedFilename: str | None = Field(None, description="Processed filename.")
    notes: str | None = Field(None, description="Check-in notes.")
    checkedInAt: int = Field(..., description="Epoch ms.")
    queueEntryId: str = Field(..., description="Live queue entry id.")
    queueType: str = Field(..., description="Queue it is in now.")
    queuePosition: int = Field(..., description="1-based position in that queue.")
    overallPosition: int = Field(
        ..., description="Position across active, then priority, then standard."
    )
    runCount: int = Field(..., description="Runs this entity has in the session.")


class MyCheckinsResponse(BaseModel):
    """The caller's live check-ins, newest first. No meta.count."""

    data: list[MyCheckinData] = Field(..., description="The check-ins.")
    meta: Meta = Field(..., description="Response metadata.")


class WithdrawnResponse(BaseModel):
    """Withdraw confirmation."""

    data: dict[str, bool] = Field(..., description="{'withdrawn': true}.")
    meta: Meta = Field(..., description="Response metadata.")


def _now_ms() -> int:
    return int(time.time() * 1000)


def _bad_request(message: str) -> Exception:
    return api_error(400, ErrorCode.BAD_REQUEST, message)


def _conflict(message: str) -> Exception:
    return api_error(409, "conflict", message)


async def _managed_entity(
    db: AsyncSession, managed_partnership_id: str, user_id: str
) -> EntityRef:
    managed = (
        await db.execute(
            select(ManagedPartnership.id, ManagedPartnership.user_id)
            .where(
                ManagedPartnership.id == managed_partnership_id,
                ManagedPartnership.deleted_at.is_(None),
            )
            .limit(1)
        )
    ).first()
    if managed is None or managed.user_id != user_id:
        raise _bad_request("Managed partnership not found")
    return EntityRef(managed_partnership_id=managed_partnership_id)


AUTH: dict[int | str, dict[str, Any]] = {
    401: {"model": ErrorResponse, "description": "Missing or invalid token."},
    403: {"model": ErrorResponse, "description": "Lacks the scope."},
}


@router.post(
    "",
    status_code=201,
    response_model=CheckinCreatedResponse,
    summary="Check in",
    description=(
        "Requires deejaytools.checkins.write. Checks an entity into the session's "
        "priority or standard queue (admission rules, ADR-005) and auto-fills "
        "the active queue. The song must belong to the user and, for an event "
        "session, be submitted to the event. With on_behalf_of_user_id the "
        "caller also needs deejaytools.delegation.act, and the entity comes "
        "from the song."
    ),
    responses={
        **AUTH,
        400: {"model": ErrorResponse, "description": "Window, entity or admission."},
        404: {"model": ErrorResponse, "description": "No such session or song."},
        409: {"model": ErrorResponse, "description": "Live entry, or a conflict."},
    },
)
async def create_checkin(
    request: Request,
    caller: Caller = Depends(require_scope("deejaytools.checkins.write")),
    body: CreateCheckinBody = Depends(zod_body(CreateCheckinBody)),
    db: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Check an entity in."""
    on_behalf = js_trim(body.on_behalf_of_user_id or "") or None
    if on_behalf:
        await authorize_delegation(caller, on_behalf, db, request)
    effective_user_id = on_behalf or caller.user_id
    if on_behalf:
        target = (
            await db.execute(select(User.id).where(User.id == on_behalf).limit(1))
        ).first()
        if target is None:
            raise _bad_request("Target user not found")
    now = _now_ms()

    session = (
        await db.execute(
            select(
                Session.id,
                Session.event_id,
                Session.checkin_opens_at,
                Session.floor_trial_ends_at,
            ).where(Session.id == body.sessionId)
        )
    ).first()
    if session is None:
        raise api_error(404, ErrorCode.NOT_FOUND, "Session not found")
    if now < session.checkin_opens_at:
        raise _bad_request("Check-in has not opened yet")
    if now > session.floor_trial_ends_at:
        raise _bad_request("Check-in is closed for this session")

    song = (
        await db.execute(
            select(Song.id, Song.managed_partnership_id, Song.partner_id)
            .where(
                Song.id == body.songId,
                Song.user_id == effective_user_id,
                Song.deleted_at.is_(None),
            )
            .limit(1)
        )
    ).first()
    if song is None:
        raise api_error(404, ErrorCode.NOT_FOUND, "Song not found")

    entity: EntityRef
    if on_behalf:
        if song.managed_partnership_id:
            entity = await _managed_entity(
                db, song.managed_partnership_id, effective_user_id
            )
        elif song.partner_id:
            pair_id = (
                await db.execute(
                    select(Pair.id)
                    .where(
                        Pair.user_a_id == effective_user_id,
                        Pair.partner_b_id == song.partner_id,
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
            if pair_id is None:
                pair_id = str(uuid.uuid4())
                await db.execute(
                    insert(Pair).values(
                        id=pair_id,
                        user_a_id=effective_user_id,
                        partner_b_id=song.partner_id,
                        created_at=now,
                    )
                )
                # Node writes the pair outside the check-in transaction: it
                # stays even when the check-in is refused below.
                await db.commit()
            entity = EntityRef(pair_id=pair_id)
        else:
            raise _bad_request(NO_ATTACHABLE_ENTITY_MSG)
    elif song.managed_partnership_id:
        entity = await _managed_entity(
            db, song.managed_partnership_id, effective_user_id
        )
    elif body.entityPairId:
        pair = (
            await db.execute(
                select(Pair.user_a_id, Pair.partner_b_id).where(
                    Pair.id == body.entityPairId
                )
            )
        ).first()
        if pair is None:
            raise _bad_request("Pair not found")
        if pair.user_a_id != effective_user_id:
            raise _bad_request("You are not a member of this pair")
        entity = EntityRef(pair_id=body.entityPairId)
    elif body.entityManagedPartnershipId:
        raise _bad_request("This song is not associated with a managed partnership")
    else:
        raise _bad_request(NO_ATTACHABLE_ENTITY_MSG)

    if await entity_has_live_entry(db, entity, body.sessionId):
        raise _conflict(LIVE_ENTRY_MSG)

    if session.event_id is not None:
        submission = (
            await db.execute(
                select(EventSongSubmission.id)
                .where(
                    EventSongSubmission.event_id == session.event_id,
                    EventSongSubmission.song_id == body.songId,
                )
                .limit(1)
            )
        ).first()
        if submission is None:
            logger.error(
                with_log_prefix(
                    LOG_FAILURE,
                    f"checkin_song_not_submitted session={body.sessionId} "
                    f"event={session.event_id} song={body.songId} "
                    f"user={effective_user_id}",
                )
            )
            raise _bad_request(NOT_SUBMITTED_MSG)

    try:
        ctx = await load_admission_context(db, body.sessionId, body.divisionName)
        initial_queue: InitialQueue = await determine_initial_queue(db, entity, ctx)
    except AdmissionError as exc:
        raise _bad_request(str(exc)) from exc
    except DBAPIError as exc:
        # As deejaytools-api, which answers any error from the admission
        # lookup (a NUL byte in divisionName, say) with 400 and its message.
        await db.rollback()
        raise _bad_request(driver_message(exc)) from exc

    checkin_id = str(uuid.uuid4())
    # End the reads' implicit transaction so the check-in's own transaction
    # starts with the session lock.
    await db.commit()
    try:
        locked = await lock_session_for_fill(db, body.sessionId)
        await db.execute(
            insert(Checkin).values(
                id=checkin_id,
                session_id=body.sessionId,
                division_name=body.divisionName,
                entity_pair_id=entity.pair_id,
                entity_solo_user_id=None,
                entity_managed_partnership_id=entity.managed_partnership_id,
                song_id=body.songId,
                submitted_by_user_id=effective_user_id,
                initial_queue=initial_queue,
                notes=body.notes,
                created_at=now,
            )
        )
        position = await next_bottom_position(db, body.sessionId, initial_queue)
        await db.execute(
            insert(QueueEntry).values(
                id=str(uuid.uuid4()),
                checkin_id=checkin_id,
                session_id=body.sessionId,
                entity_pair_id=entity.pair_id,
                entity_solo_user_id=None,
                entity_managed_partnership_id=entity.managed_partnership_id,
                queue_type=initial_queue,
                position=position,
                entered_queue_at=now,
            )
        )
        await record_queue_event(
            db,
            session_id=body.sessionId,
            checkin_id=checkin_id,
            action="checked_in",
            from_queue=None,
            from_position=None,
            to_queue=initial_queue,
            to_position=position,
            actor_user_id=caller.user_id,
            reason=None,
            created_at=now,
        )
        if locked is not None:
            await fill_active_queue(db, locked, caller.user_id, now)
        await db.commit()
    except Exception as exc:  # noqa: BLE001 - any failure is a 409, as in Node
        await db.rollback()
        logger.error(
            with_log_prefix(
                LOG_FAILURE,
                f"checkin_create_failed session={body.sessionId} "
                f"division={body.divisionName} user={effective_user_id} "
                f"pair={entity.pair_id} managed={entity.managed_partnership_id}: "
                f"{exc!r}",
            )
        )
        raise _conflict(
            "Check-in conflicted with concurrent activity; please retry"
        ) from exc

    invalidate_queue_cache(body.sessionId)
    return success(
        {
            "id": checkin_id,
            "sessionId": body.sessionId,
            "divisionName": body.divisionName,
            "initialQueue": initial_queue,
        }
    )


@router.get(
    "/mine",
    response_model=MyCheckinsResponse,
    summary="My live check-ins",
    description=(
        "Requires deejaytools.checkins.read. The caller's check-ins that still "
        "have a queue entry (solo, pairs they lead, managed partnerships they "
        "own), newest first, with queue position, overall position and run count."
    ),
    responses=AUTH,
)
async def my_checkins(
    caller: Caller = Depends(require_scope("deejaytools.checkins.read")),
    db: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """List the caller's live check-ins."""
    user_id = caller.user_id
    pair_user = aliased(User, name="pair_user")

    pair_ids = list(
        (await db.execute(select(Pair.id).where(Pair.user_a_id == user_id))).scalars()
    )
    managed_ids = list(
        (
            await db.execute(
                select(ManagedPartnership.id).where(
                    ManagedPartnership.user_id == user_id
                )
            )
        ).scalars()
    )

    # Ownership is read from queue_entries' entity columns: checkins.entity_*
    # can be stale on legacy rows.
    parts = [QueueEntry.entity_solo_user_id == user_id]
    if pair_ids:
        parts.append(QueueEntry.entity_pair_id.in_(pair_ids))
    if managed_ids:
        parts.append(QueueEntry.entity_managed_partnership_id.in_(managed_ids))

    rows = (
        await db.execute(
            select(
                Checkin.id,
                Checkin.session_id,
                Event.name.label("event_name"),
                Session.name.label("session_name"),
                Session.floor_trial_starts_at,
                Session.status.label("session_status"),
                Event.timezone.label("event_timezone"),
                Checkin.division_name,
                QueueEntry.entity_pair_id,
                QueueEntry.entity_solo_user_id,
                QueueEntry.entity_managed_partnership_id,
                Checkin.notes,
                Checkin.created_at,
                Song.display_name.label("song_display_name"),
                Song.processed_filename.label("song_processed_filename"),
                QueueEntry.id.label("queue_entry_id"),
                QueueEntry.queue_type,
                QueueEntry.position,
                pair_user.first_name.label("pair_user_first"),
                pair_user.last_name.label("pair_user_last"),
                Partner.first_name.label("partner_first"),
                Partner.last_name.label("partner_last"),
                Partner.kind.label("partner_kind"),
                ManagedPartnership.leader_first_name,
                ManagedPartnership.leader_last_name,
                ManagedPartnership.follower_first_name,
                ManagedPartnership.follower_last_name,
            )
            .select_from(Checkin)
            .join(QueueEntry, QueueEntry.checkin_id == Checkin.id)
            .join(Session, Session.id == Checkin.session_id)
            .outerjoin(Event, Event.id == Session.event_id)
            .outerjoin(Song, Song.id == Checkin.song_id)
            .outerjoin(Pair, Pair.id == QueueEntry.entity_pair_id)
            .outerjoin(pair_user, pair_user.id == Pair.user_a_id)
            .outerjoin(Partner, Partner.id == Pair.partner_b_id)
            .outerjoin(
                ManagedPartnership,
                ManagedPartnership.id == QueueEntry.entity_managed_partnership_id,
            )
            .where(or_(*parts) if len(parts) > 1 else parts[0])
            .order_by(desc(Checkin.created_at))
        )
    ).all()

    session_ids = list(dict.fromkeys(r.session_id for r in rows))
    counts: dict[str, dict[str, int]] = {}
    run_counts: dict[str, int] = {}
    if session_ids:
        for sid, queue_type, n in (
            await db.execute(
                select(QueueEntry.session_id, QueueEntry.queue_type, func.count())
                .where(QueueEntry.session_id.in_(session_ids))
                .group_by(QueueEntry.session_id, QueueEntry.queue_type)
            )
        ).all():
            counts.setdefault(sid, {"active": 0, "priority": 0, "non_priority": 0})[
                queue_type
            ] = int(n)

        run_parts = [Run.entity_solo_user_id == user_id]
        if pair_ids:
            run_parts.append(Run.entity_pair_id.in_(pair_ids))
        if managed_ids:
            run_parts.append(Run.entity_managed_partnership_id.in_(managed_ids))
        for rc in (
            await db.execute(
                select(
                    Run.session_id,
                    Run.entity_pair_id,
                    Run.entity_solo_user_id,
                    Run.entity_managed_partnership_id,
                    func.count().label("n"),
                )
                .where(and_(Run.session_id.in_(session_ids), or_(*run_parts)))
                .group_by(
                    Run.session_id,
                    Run.entity_pair_id,
                    Run.entity_solo_user_id,
                    Run.entity_managed_partnership_id,
                )
            )
        ).all():
            key = (
                rc.entity_pair_id
                or rc.entity_solo_user_id
                or rc.entity_managed_partnership_id
            )
            if key:
                run_counts[f"{rc.session_id}:{key}"] = int(rc.n)

    def overall_position(session_id: str, queue_type: str, position: int) -> int:
        c = counts.get(session_id, {"active": 0, "priority": 0, "non_priority": 0})
        if queue_type == "active":
            return position
        if queue_type == "priority":
            return c["active"] + position
        return c["active"] + c["priority"] + position

    data = []
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
        else:
            label = "Solo"
        key = (
            r.entity_pair_id or r.entity_solo_user_id or r.entity_managed_partnership_id
        )
        data.append(
            {
                "id": r.id,
                "sessionId": r.session_id,
                "eventName": r.event_name,
                "sessionName": r.session_name,
                "sessionFloorTrialStartsAt": r.floor_trial_starts_at,
                "sessionStatus": r.session_status,
                "eventTimezone": r.event_timezone,
                "divisionName": r.division_name,
                "entityPairId": r.entity_pair_id,
                "entitySoloUserId": r.entity_solo_user_id,
                "entityManagedPartnershipId": r.entity_managed_partnership_id,
                "entityLabel": label,
                "songDisplayName": r.song_display_name,
                "songProcessedFilename": r.song_processed_filename,
                "notes": r.notes,
                "checkedInAt": r.created_at,
                "queueEntryId": r.queue_entry_id,
                "queueType": r.queue_type,
                "queuePosition": r.position,
                "overallPosition": overall_position(
                    r.session_id, r.queue_type, r.position
                ),
                "runCount": run_counts.get(f"{r.session_id}:{key}", 0) if key else 0,
            }
        )
    return success(data)


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
    now = _now_ms()

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
        # Position and queue as read before the lock, as Node does.
        await db.execute(delete(QueueEntry).where(QueueEntry.id == row.queue_entry_id))
        await compact_after_removal(db, row.session_id, row.queue_type, row.position)
        await record_queue_event(
            db,
            session_id=row.session_id,
            checkin_id=row.checkin_id,
            action="withdrawn",
            from_queue=row.queue_type,
            from_position=row.position,
            to_queue=None,
            to_position=None,
            actor_user_id=user_id,
            reason="self_withdrew",
            created_at=now,
        )
        if locked is not None:
            await fill_active_queue(db, locked, user_id, now)
        await db.commit()
    except Exception as exc:  # noqa: BLE001 - any failure is a 409, as in Node
        await db.rollback()
        logger.error(
            with_log_prefix(
                LOG_FAILURE,
                f"checkin_self_withdraw_failed checkin={id} user={user_id}: {exc!r}",
            )
        )
        raise _conflict(
            "Withdraw conflicted with concurrent activity; please retry"
        ) from exc

    invalidate_session_cache(row.session_id)
    invalidate_queue_cache(row.session_id)
    return success({"withdrawn": True})
