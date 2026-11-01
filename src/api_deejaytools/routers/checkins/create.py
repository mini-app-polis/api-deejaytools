"""``POST /v1/checkins``: a dancer checks an entity into a session's waiting
queue.

Behind ``deejaytools.checkins.write``; an admin, or the DJ or event manager
of the session's event, may check in for someone else with
``on_behalf_of_user_id``, which is a second decision
(``deejaytools.delegation.act`` on the session's event, ADR-0008 and
ADR-0012). The check-in is a queue transaction: it locks the session row
first, changes the queue, and auto-fills the active queue (ADR-0006).
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any, ClassVar, Literal

from fastapi import APIRouter, Depends, Request
from mini_app_polis.logger import LOG_FAILURE, get_logger, with_log_prefix
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import and_, insert, or_, select
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from ...auth import Caller, authorize_delegation, require_scope
from ...cache import invalidate_queue_cache
from ...clock import now_ms
from ...database import get_db_session
from ...db_errors import driver_message
from ...errors import (
    Envelope,
    ErrorCode,
    ErrorResponse,
    api_error,
    bad_request,
    conflict,
    success,
)
from ...models import (
    Checkin,
    EventSongSubmission,
    Pair,
    Partner,
    QueueEntry,
    Session,
    Song,
    User,
)
from ...queue import (
    AdmissionError,
    EntityRef,
    InitialQueue,
    determine_initial_queue,
    entity_has_live_entry,
    fill_active_queue,
    load_admission_context,
    lock_session_for_fill,
    next_bottom_position,
)
from ...queue.audit import record_queue_event
from ...song_records import shared_with
from ...validation import NonEmptyStr, ZodModel
from ...zod_coerce import js_trim
from ...zod_types import trimmed, zod_body
from .common import AUTH
from .entities import _couple_has_live_entry, _managed_entity, _song_pair

logger = get_logger()

router = APIRouter(prefix="/v1/checkins", tags=["checkins"])


class _CoupleCheckedIn(Exception):
    """The other half of the couple checked in before the session lock."""


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


class CheckinCreatedResponse(Envelope[CheckinCreatedData]):
    """Check-in created."""

    data: CheckinCreatedData = Field(..., description="The check-in.")


@router.post(
    "",
    status_code=201,
    response_model=CheckinCreatedResponse,
    summary="Check in",
    description=(
        "Requires deejaytools.checkins.write. Checks an entity into the session's "
        "priority or standard queue (admission rules, ADR-0006) and auto-fills "
        "the active queue. The song must belong to the user, or be a live "
        "song shared with them as its linked partner, and, for an event "
        "session, be submitted to the event. For a shared song the entity is "
        "the owner's pair with that partner, whichever partner checks in, so "
        "the two cannot both hold an entry; its entityPairId must name that "
        "pair or one the caller leads. A couple holds one entry per session: "
        "a pair is refused while the other partner's pair for the same couple "
        "has one (each pair's partner row stands for the other's account, by "
        "link or, unlinked, by email). With on_behalf_of_user_id the caller "
        "also needs deejaytools.delegation.act on the session's event, and "
        "the entity comes from the song."
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
        # Decided on the session's event: its DJ or event manager may act
        # for a dancer there. No such session, or one with no event, leaves
        # it to roles (an admin's); the 404 follows.
        session_event_id = (
            await db.execute(
                select(Session.event_id).where(Session.id == body.sessionId)
            )
        ).scalar_one_or_none()
        await authorize_delegation(
            caller, on_behalf, db, request, event_id=session_event_id
        )
    effective_user_id = on_behalf or caller.user_id
    if on_behalf:
        target = (
            await db.execute(select(User.id).where(User.id == on_behalf).limit(1))
        ).first()
        if target is None:
            raise bad_request("Target user not found")
    now = now_ms()

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
        raise bad_request("Check-in has not opened yet")
    if now > session.floor_trial_ends_at:
        raise bad_request("Check-in is closed for this session")

    # The caller's own live song or, checking in for themselves, a live song
    # shared with them as its linked partner. A managed partnership's song
    # stays its owner's to check in.
    whose = Song.user_id == effective_user_id
    if not on_behalf:
        whose = or_(
            whose,
            and_(
                shared_with(effective_user_id),
                Song.managed_partnership_id.is_(None),
            ),
        )
    song = (
        await db.execute(
            select(Song.id, Song.user_id, Song.managed_partnership_id, Song.partner_id)
            .outerjoin(Partner, Partner.id == Song.partner_id)
            .where(Song.id == body.songId, Song.deleted_at.is_(None), whose)
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
            entity = EntityRef(
                pair_id=await _song_pair(db, effective_user_id, song.partner_id, now)
            )
        else:
            raise bad_request(NO_ATTACHABLE_ENTITY_MSG)
    elif song.user_id != effective_user_id:
        # A shared song: the entity is the owner's pair with this partner,
        # not one of the caller's own, so both partners check in as one.
        assert song.partner_id is not None  # shared means a linked partner
        if body.entityManagedPartnershipId:
            raise bad_request("This song is not associated with a managed partnership")
        named = (
            await db.execute(
                select(Pair.user_a_id, Pair.partner_b_id).where(
                    Pair.id == body.entityPairId
                )
            )
        ).first()
        if named is None:
            raise bad_request("Pair not found")
        if named.user_a_id != effective_user_id and (
            named.user_a_id != song.user_id or named.partner_b_id != song.partner_id
        ):
            raise bad_request("You are not a member of this pair")
        entity = EntityRef(
            pair_id=await _song_pair(db, song.user_id, song.partner_id, now)
        )
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
            raise bad_request("Pair not found")
        if pair.user_a_id != effective_user_id:
            raise bad_request("You are not a member of this pair")
        entity = EntityRef(pair_id=body.entityPairId)
    elif body.entityManagedPartnershipId:
        raise bad_request("This song is not associated with a managed partnership")
    else:
        raise bad_request(NO_ATTACHABLE_ENTITY_MSG)

    if await entity_has_live_entry(db, entity, body.sessionId):
        raise conflict(LIVE_ENTRY_MSG)
    # The couple rule: a couple dances once per session, whichever partner's
    # pair holds the entry.
    if entity.pair_id and await _couple_has_live_entry(
        db, entity.pair_id, body.sessionId
    ):
        raise conflict(LIVE_ENTRY_MSG)

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
            raise bad_request(NOT_SUBMITTED_MSG)

    try:
        ctx = await load_admission_context(db, body.sessionId, body.divisionName)
        initial_queue: InitialQueue = await determine_initial_queue(db, entity, ctx)
    except AdmissionError as exc:
        raise bad_request(str(exc)) from exc
    except DBAPIError as exc:
        # Any error from the admission lookup (a NUL byte in divisionName,
        # say) is a 400 with the database's message.
        await db.rollback()
        raise bad_request(driver_message(exc)) from exc

    checkin_id = str(uuid.uuid4())
    # End the reads' implicit transaction so the check-in's own transaction
    # starts with the session lock.
    await db.commit()
    try:
        locked = await lock_session_for_fill(db, body.sessionId)
        # Again under the lock: the check above ran before it, and no index
        # holds a couple to one entry, so the other partner's check-in may
        # have landed since.
        if entity.pair_id and await _couple_has_live_entry(
            db, entity.pair_id, body.sessionId
        ):
            raise _CoupleCheckedIn()
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
    except (
        SQLAlchemyError,
        _CoupleCheckedIn,
    ) as exc:  # a refusal, or the transaction failed: a 409
        await db.rollback()
        if isinstance(exc, _CoupleCheckedIn):
            raise conflict(LIVE_ENTRY_MSG) from exc
        logger.error(
            with_log_prefix(
                LOG_FAILURE,
                f"checkin_create_failed session={body.sessionId} "
                f"division={body.divisionName} user={effective_user_id} "
                f"pair={entity.pair_id} managed={entity.managed_partnership_id}: "
                f"{exc!r}",
            )
        )
        raise conflict(
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
