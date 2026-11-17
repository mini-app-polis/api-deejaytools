"""``/v1/admin/event-song-submissions`` (docs/API.md).

Every submission to one event, for the Manager's Event Songs tab, behind
``deejaytools.entries.read`` (ADR-0008). The query and its answer are
``event_submissions_response``, which the event staff's
``GET /v1/events/{id}/song-submissions`` shares (ADR-0012).

``PATCH /{id}`` moves one submission to another division of its event,
behind ``deejaytools.entries.write`` (the admin role's only): the
submission's per-event division override changes in place, the song's own
division does not, and one ``move`` drive job puts its Drive copy in the new
division's folder under the new name. The dancer is not notified.
"""

from __future__ import annotations

from typing import Annotated, Any

import sentry_sdk
from fastapi import APIRouter, Depends, Path
from fastapi.responses import JSONResponse
from mini_app_polis.logger import (
    LOG_FAILURE,
    LOG_SUCCESS,
    get_logger,
    with_log_prefix,
)
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from ..auth import Caller, require_scope
from ..database import get_db_session
from ..domain import song_entity_key
from ..errors import (
    Envelope,
    ErrorCode,
    ErrorResponse,
    api_error,
    error_body,
    success,
    success_list,
)
from ..event_connections import email_for_caller
from ..labels import build_structured_song_label, partnership_label
from ..models import (
    Checkin,
    Event,
    EventSongSubmission,
    ManagedPartnership,
    Partner,
    QueueEntry,
    Session,
    Song,
    User,
)
from ..services import change_feed
from ..services.drive_jobs import MOVE, add_drive_job
from ..submissions import (
    DEFAULT_ROUND,
    ROUND_SPLIT_DIVISION,
    SubmissionRound,
    effective_division,
    entity_slot_taken,
    is_open_event,
    lock_entity_slot,
    slot_taken_message,
)
from ..validation import ZodModel
from ..zod_types import Division, QueryStr, zod_body, zod_query

logger = get_logger()

router = APIRouter(
    prefix="/v1/admin/event-song-submissions", tags=["admin-event-submissions"]
)


class ListQuery(ZodModel):
    """Query of ``GET /v1/admin/event-song-submissions``."""

    event_id: Annotated[QueryStr, Field(min_length=1)] = Field(
        ..., description="The event whose submissions to list."
    )


class AdminSubmissionData(BaseModel):
    """A submission as the admin list returns it."""

    id: str = Field(..., description="Submission id.")
    event_id: str = Field(..., description="Event id.")
    event_name: str = Field(..., description="Event name.")
    division: str | None = Field(
        None,
        description="The division it is entered under: the override, else the song's.",
    )
    round: str = Field(
        ...,
        description=(
            "Rounds it is entered for: prelims_and_finals, prelims_only or "
            "finals_only (Classic at The Open only splits)."
        ),
    )
    song_id: str = Field(..., description="Song id.")
    owner_user_id: str = Field(
        ...,
        description=(
            "User id of the song's owner: the on_behalf_of_user_id a staff"
            " check-in for this song takes."
        ),
    )
    song_label: str = Field(..., description="Structured song label.")
    partnership_label: str = Field(..., description="Who the song belongs to.")
    duration_ms: int | None = Field(
        None,
        description=(
            "How long the song's file plays, in milliseconds; null until it "
            "has been read from the file, or when it cannot be."
        ),
    )
    submitter_email: str = Field(
        ...,
        description=(
            "Email of the user who submitted; masked on the event-scoped route "
            "unless the caller is an admin."
        ),
    )
    created_at: int = Field(..., description="Submitted, epoch ms.")


class AdminSubmissionListResponse(Envelope[list[AdminSubmissionData]]):
    """An event's submissions, newest first."""

    data: list[AdminSubmissionData] = Field(..., description="The submissions.")


class AdminSubmissionResponse(Envelope[AdminSubmissionData]):
    """One submission."""

    data: AdminSubmissionData = Field(..., description="The submission.")


class MoveSubmissionBody(ZodModel):
    """Body of ``PATCH /v1/admin/event-song-submissions/{id}``."""

    division: Division = Field(
        ..., description="The division of the same event to move it to."
    )
    round: SubmissionRound | None = Field(
        None,
        description=(
            "Classic at The Open only: the rounds it is entered for in the new "
            "division. Default prelims_and_finals."
        ),
    )


SubmissionId = Annotated[str, Path(description="Submission id.")]


@router.get(
    "",
    response_model=AdminSubmissionListResponse,
    summary="List an event's submissions",
    description=(
        "Every submission to one event (query event_id), newest first. "
        "Requires deejaytools.entries.read."
    ),
    responses={
        401: {"model": ErrorResponse, "description": "Missing or invalid token."},
        403: {"model": ErrorResponse, "description": "Lacks the scope."},
        400: {"model": ErrorResponse, "description": "Missing event_id."},
        500: {"model": ErrorResponse, "description": "The query failed."},
    },
)
async def list_event_submissions(
    _caller: Caller = Depends(require_scope("deejaytools.entries.read")),
    query: ListQuery = Depends(zod_query(ListQuery)),
    session: AsyncSession = Depends(get_db_session),
) -> Any:
    """List an event's submissions."""
    return await event_submissions_response(session, query.event_id)


async def event_submissions_response(
    session: AsyncSession, event_id: str, *, whole_emails: bool = True
) -> Any:
    """Every submission to ``event_id``, newest first, as the list answers it.

    A ``successList`` of ``AdminSubmissionData``, or the 500 envelope when
    the query fails. Authorization is the caller's.
    """
    try:
        return success_list(
            await _submission_rows(session, event_id, whole_emails=whole_emails)
        )
    except SQLAlchemyError as exc:
        sentry_sdk.capture_exception(exc)
        logger.error(
            with_log_prefix(
                LOG_FAILURE,
                f"admin_event_song_submission_list_failed event={event_id}: {exc!r}",
            )
        )
        return _internal()


def _internal() -> JSONResponse:
    return JSONResponse(
        status_code=500,
        content=error_body(ErrorCode.INTERNAL, "Internal server error"),
    )


async def _submission_rows(
    session: AsyncSession,
    event_id: str,
    *,
    whole_emails: bool = True,
    submission_id: str | None = None,
) -> list[dict[str, Any]]:
    """``event_id``'s submissions (only ``submission_id``, when given) as
    ``AdminSubmissionData``, newest first. Raises on a database error."""
    owner = aliased(User, name="song_owner")
    submitter = aliased(User, name="submitter")
    managed = aliased(ManagedPartnership, name="managed_partnership")
    conditions = [EventSongSubmission.event_id == event_id]
    if submission_id is not None:
        conditions.append(EventSongSubmission.id == submission_id)
    rows = (
        await session.execute(
            select(
                EventSongSubmission.id,
                EventSongSubmission.event_id,
                EventSongSubmission.created_at,
                Event.name.label("event_name"),
                Song.id.label("song_id"),
                Song.user_id.label("owner_user_id"),
                EventSongSubmission.division.label("submission_division"),
                EventSongSubmission.round.label("submission_round"),
                Song.division.label("song_division"),
                Song.display_name.label("song_display_name"),
                Song.processed_filename.label("song_processed_filename"),
                Song.routine_name.label("song_routine_name"),
                Song.season_year.label("song_season_year"),
                Song.duration_ms.label("song_duration_ms"),
                owner.first_name.label("owner_first"),
                owner.last_name.label("owner_last"),
                Partner.first_name.label("partner_first"),
                Partner.last_name.label("partner_last"),
                Partner.kind.label("partner_kind"),
                Partner.partner_role.label("partner_role"),
                managed.leader_first_name.label("managed_leader_first"),
                managed.leader_last_name.label("managed_leader_last"),
                managed.follower_first_name.label("managed_follower_first"),
                managed.follower_last_name.label("managed_follower_last"),
                submitter.email.label("submitter_email"),
            )
            .select_from(EventSongSubmission)
            .join(Event, Event.id == EventSongSubmission.event_id)
            .join(Song, Song.id == EventSongSubmission.song_id)
            .outerjoin(owner, owner.id == Song.user_id)
            .outerjoin(Partner, Partner.id == Song.partner_id)
            .outerjoin(managed, managed.id == Song.managed_partnership_id)
            .join(submitter, submitter.id == EventSongSubmission.submitted_by_user_id)
            .where(*conditions)
            .order_by(EventSongSubmission.created_at.desc())
        )
    ).all()

    data = []
    for r in rows:
        # The division it is entered under: the override, else the song's.
        division = (
            r.submission_division
            if r.submission_division is not None
            else r.song_division
        )
        label = partnership_label(
            managed_leader_first=r.managed_leader_first,
            managed_leader_last=r.managed_leader_last,
            managed_follower_first=r.managed_follower_first,
            managed_follower_last=r.managed_follower_last,
            owner_first=r.owner_first,
            owner_last=r.owner_last,
            partner_first=r.partner_first,
            partner_last=r.partner_last,
            partner_kind=r.partner_kind,
            partner_role=r.partner_role,
            division=division,
        )
        data.append(
            {
                "id": r.id,
                "event_id": r.event_id,
                "event_name": r.event_name,
                "division": division,
                "round": (
                    r.submission_round
                    if r.submission_round is not None
                    else DEFAULT_ROUND
                ),
                "song_id": r.song_id,
                "owner_user_id": r.owner_user_id,
                "song_label": build_structured_song_label(
                    partnership=label,
                    division=division,
                    season_year=r.song_season_year,
                    routine_name=r.song_routine_name,
                    processed_filename=r.song_processed_filename,
                    display_name=r.song_display_name,
                    song_id=r.song_id,
                ),
                "partnership_label": label,
                "duration_ms": r.song_duration_ms,
                "submitter_email": email_for_caller(
                    r.submitter_email, whole=whole_emails
                )
                or "",
                "created_at": r.created_at,
            }
        )
    return data


@router.patch(
    "/{id}",
    response_model=AdminSubmissionResponse,
    summary="Move a submission to another division",
    description=(
        "Moves one song submission to another division of the same event, in "
        "place: its per-event division (and round) change, the song's own "
        "division does not, and one move drive job puts its Drive copy in the "
        "new division's folder under the new name. Refused while the song is "
        "checked in to a session of the event in its old division, or when "
        "the entity already has a song in the new division (for Classic at "
        "The Open, in an overlapping round). Drops the hide a scratched "
        "registration entry put on it. The dancer is not notified. Requires "
        "deejaytools.entries.write."
    ),
    responses={
        401: {"model": ErrorResponse, "description": "Missing or invalid token."},
        403: {"model": ErrorResponse, "description": "Lacks the scope."},
        400: {
            "model": ErrorResponse,
            "description": "Already in that division, or round not allowed there.",
        },
        404: {"model": ErrorResponse, "description": "No such submission."},
        409: {
            "model": ErrorResponse,
            "description": "Checked in, or the entity's slot there is taken.",
        },
        500: {"model": ErrorResponse, "description": "The update failed."},
    },
)
async def move_event_submission(
    id: SubmissionId,
    caller: Caller = Depends(require_scope("deejaytools.entries.write")),
    body: MoveSubmissionBody = Depends(zod_body(MoveSubmissionBody)),
    session: AsyncSession = Depends(get_db_session),
) -> Any:
    """Move a submission to another division of its event."""
    current = (
        await session.execute(
            select(
                EventSongSubmission.event_id,
                EventSongSubmission.song_id,
                EventSongSubmission.division.label("submission_division"),
                Song.division.label("song_division"),
                Song.user_id,
                Song.partner_id,
                Song.managed_partnership_id,
                Event.name.label("event_name"),
            )
            .join(Song, Song.id == EventSongSubmission.song_id)
            .join(Event, Event.id == EventSongSubmission.event_id)
            .where(EventSongSubmission.id == id)
            .limit(1)
        )
    ).first()
    if current is None:
        raise api_error(404, ErrorCode.NOT_FOUND, "Event song submission not found")

    old_division = effective_division(
        current.submission_division, current.song_division
    )
    division = body.division
    if division == old_division:
        raise api_error(
            400, ErrorCode.BAD_REQUEST, f"This entry is already in {division}."
        )
    round_ = body.round if body.round is not None else DEFAULT_ROUND
    # As for a new submission: rounds are Classic at The Open's only.
    if round_ != DEFAULT_ROUND and not (
        is_open_event(current.event_name) and division == ROUND_SPLIT_DIVISION
    ):
        raise api_error(
            400,
            ErrorCode.BAD_REQUEST,
            "Round selection is only available for the "
            f"{ROUND_SPLIT_DIVISION} division at The Open",
        )

    # A live check-in names the old division: moving the entry under it
    # would leave the queue running the song under a division the entry is
    # not in.
    # A finished session's queue entries are history and do not block it.
    checked_in = (
        await session.execute(
            select(Checkin.id)
            .join(QueueEntry, QueueEntry.checkin_id == Checkin.id)
            .join(Session, Session.id == Checkin.session_id)
            .where(
                Checkin.song_id == current.song_id,
                Checkin.division_name == old_division,
                Session.event_id == current.event_id,
                Session.status.not_in(["completed", "cancelled"]),
            )
            .limit(1)
        )
    ).first()
    if checked_in is not None:
        raise api_error(
            409,
            "SUBMISSION_CHECKED_IN",
            f"This entry is checked in to a session in {old_division}. "
            "Complete or withdraw the check-in before moving it.",
        )

    entity = song_entity_key(
        current.user_id, current.partner_id, current.managed_partnership_id
    )
    await lock_entity_slot(session, event_id=current.event_id, entity=entity)
    if await entity_slot_taken(
        session,
        event_id=current.event_id,
        entity=entity,
        division=division,
        round_=round_,
        song_id=current.song_id,
    ):
        raise api_error(409, "ENTITY_SLOT_TAKEN", slot_taken_message(division))

    try:
        await session.execute(
            update(EventSongSubmission)
            .where(EventSongSubmission.id == id)
            .values(
                division=division,
                round=body.round,
                # The hide a scratched entry in the old division put on it
                # was for that entry; the new division's list matches it anew.
                hidden_by_registration_id=None,
            )
        )
        # In the move's transaction: the Drive copy follows only a move that
        # committed, and is lost only with it.
        add_drive_job(session, MOVE, submission_id=id)
        change_feed.record(
            session,
            "event_song_submissions",
            change_feed.UPDATED,
            {
                "id": id,
                "event_id": current.event_id,
                "song_id": current.song_id,
                "from": old_division,
                "to": division,
            },
            covers=["event_song_submissions"],
        )
        await session.commit()
        moved = await _submission_rows(session, current.event_id, submission_id=id)
    except SQLAlchemyError as exc:
        await session.rollback()
        sentry_sdk.capture_exception(exc)
        logger.error(
            with_log_prefix(
                LOG_FAILURE,
                f"admin_event_song_submission_move_failed submission={id} "
                f"user={caller.user_id}: {exc!r}",
            )
        )
        return _internal()
    logger.info(
        with_log_prefix(
            LOG_SUCCESS,
            f"admin_event_song_submission_moved submission={id} "
            f"from={old_division!r} to={division!r} user={caller.user_id}",
        )
    )
    return success(moved[0])
