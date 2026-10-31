"""``/v1/event-song-submissions`` (docs/API.md).

A user's songs entered in events. The list is behind
``deejaytools.submissions.read``, create and delete behind
``deejaytools.submissions.write``; the list also shows submissions of songs
the caller shares, the other routes only those the caller made. Each entity has one song per division per event (per round, for
Classic at The Open). Drive copies run from the ``drive_jobs`` queue.
"""

from __future__ import annotations

import time
import uuid
from typing import Annotated, Any

import sentry_sdk
from fastapi import APIRouter, Depends, Path, Response
from fastapi.responses import JSONResponse
from mini_app_polis.logger import LOG_FAILURE, get_logger, with_log_prefix
from pydantic import BaseModel, Field
from sqlalchemy import delete, or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import Caller, require_scope
from ..database import get_db_session
from ..db_errors import is_unique_violation
from ..domain import song_entity_key
from ..errors import (
    ErrorCode,
    ErrorResponse,
    ListMeta,
    Meta,
    api_error,
    error_body,
    success,
    success_list,
)
from ..models import Event, EventSongSubmission, Partner, Song
from ..services.drive_jobs import enqueue_copy_job, enqueue_trash_jobs
from ..song_records import shared_with
from ..submissions import (
    DEFAULT_ROUND,
    ROUND_SPLIT_DIVISION,
    SubmissionRound,
    entity_slot_taken,
    fetch_user_submission_rows,
    is_open_event,
    map_submission_row,
    slot_taken_message,
)
from ..validation import NonEmptyStr, ZodModel
from ..zod_coerce import js_trim
from ..zod_types import Division, QueryStr, zod_body, zod_query

logger = get_logger()

router = APIRouter(prefix="/v1/event-song-submissions", tags=["event-song-submissions"])


class ListQuery(ZodModel):
    """Query of ``GET /v1/event-song-submissions``."""

    event_id: QueryStr | None = Field(None, description="Only this event.")


class CreateSubmissionBody(ZodModel):
    """Body of ``POST /v1/event-song-submissions``."""

    event_id: NonEmptyStr = Field(..., description="Event to enter.")
    song_id: NonEmptyStr = Field(
        ..., description="One of the caller's songs, or one shared with them."
    )
    division: Division | None = Field(
        None, description="This event only: overrides the song's division."
    )
    round: SubmissionRound | None = Field(
        None,
        description="Classic at The Open only. Default prelims_and_finals.",
    )


class SubmissionData(BaseModel):
    """A submission as the API returns it."""

    id: str = Field(..., description="Submission id.")
    event_id: str = Field(..., description="Event id.")
    event_name: str = Field(..., description="Event name.")
    event_start_date: str = Field(..., description="Event's first day.")
    event_status: str = Field(..., description="upcoming, active or completed.")
    song_id: str = Field(..., description="Song id.")
    song_label: str = Field(..., description="Structured song label.")
    division: str | None = Field(
        None, description="The override, else the song's division."
    )
    round: str = Field(..., description="Rounds the song is entered for.")
    created_at: int = Field(..., description="Submitted, epoch ms.")


class ListedSubmissionData(SubmissionData):
    """A listed submission, and whether the caller may withdraw it."""

    can_withdraw: bool = Field(
        ..., description="True only for the caller who submitted it."
    )


class SubmissionResponse(BaseModel):
    """One submission."""

    data: SubmissionData = Field(..., description="The submission.")
    meta: Meta = Field(..., description="Response metadata.")


class SubmissionListResponse(BaseModel):
    """The caller's and their shared songs' submissions, newest first."""

    data: list[ListedSubmissionData] = Field(..., description="The submissions.")
    meta: ListMeta = Field(..., description="Response metadata.")


def _internal() -> JSONResponse:
    return JSONResponse(
        status_code=500,
        content=error_body(ErrorCode.INTERNAL, "Internal server error"),
    )


SubmissionId = Annotated[str, Path(description="Submission id.")]
AUTH: dict[int | str, dict[str, Any]] = {
    401: {"model": ErrorResponse, "description": "Missing or invalid token."},
    403: {"model": ErrorResponse, "description": "Lacks the scope."},
}


@router.get(
    "",
    response_model=SubmissionListResponse,
    summary="List the caller's submissions",
    description=(
        "Submissions the caller made, and those of songs the caller owns or "
        "shares as linked partner, whoever submitted them, newest first, "
        "optionally for one event (query event_id). can_withdraw is true only "
        "for the submitter. Requires deejaytools.submissions.read."
    ),
    responses={
        **AUTH,
        500: {"model": ErrorResponse, "description": "The query failed."},
    },
)
async def list_submissions(
    caller: Caller = Depends(require_scope("deejaytools.submissions.read")),
    query: ListQuery = Depends(zod_query(ListQuery)),
    session: AsyncSession = Depends(get_db_session),
) -> Any:
    """List the caller's submissions."""
    try:
        rows = await fetch_user_submission_rows(
            session,
            caller.user_id,
            event_id=query.event_id or None,
            include_shared=True,
        )
        return success_list(
            [
                {
                    **map_submission_row(r),
                    "can_withdraw": r.submitted_by_user_id == caller.user_id,
                }
                for r in rows
            ]
        )
    except SQLAlchemyError as exc:
        sentry_sdk.capture_exception(exc)
        logger.error(
            with_log_prefix(
                LOG_FAILURE,
                f"event_song_submission_list_failed user={caller.user_id} "
                f"event={query.event_id}: {exc!r}",
            )
        )
        return _internal()


@router.post(
    "",
    status_code=201,
    response_model=SubmissionResponse,
    summary="Submit a song to an event",
    description=(
        "Enters one of the caller's songs, or a live song shared with the "
        "caller as its linked partner, in an event, then queues its Drive "
        "copy (a failure to queue is reported, never answered). An entity may "
        "have one song per division per event; at The Open, Classic may split "
        "into prelims_only and finals_only; both partners of a shared song "
        "fill the same slot. The caller who submits it is the one who sees "
        "and withdraws the submission. Requires "
        "deejaytools.submissions.write."
    ),
    responses={
        **AUTH,
        400: {"model": ErrorResponse, "description": "Round not allowed here."},
        404: {"model": ErrorResponse, "description": "No such song or event."},
        409: {
            "model": ErrorResponse,
            "description": "Already submitted, or the entity's slot is taken.",
        },
        500: {"model": ErrorResponse, "description": "The insert failed."},
    },
)
async def create_submission(
    caller: Caller = Depends(require_scope("deejaytools.submissions.write")),
    body: CreateSubmissionBody = Depends(zod_body(CreateSubmissionBody)),
    session: AsyncSession = Depends(get_db_session),
) -> Any:
    """Submit a song to an event."""
    user_id = caller.user_id
    # A deleted song is not submitted, by its owner or a linked partner: its
    # Drive file is queued for trashing, so there is nothing to copy.
    song = (
        await session.execute(
            select(
                Song.id,
                Song.user_id,
                Song.partner_id,
                Song.managed_partnership_id,
                Song.division,
            )
            .outerjoin(Partner, Partner.id == Song.partner_id)
            .where(
                Song.id == body.song_id,
                Song.deleted_at.is_(None),
                or_(Song.user_id == user_id, shared_with(user_id)),
            )
            .limit(1)
        )
    ).first()
    if song is None:
        raise api_error(404, ErrorCode.NOT_FOUND, "Song not found")

    event = (
        await session.execute(
            select(Event.id, Event.name).where(Event.id == body.event_id).limit(1)
        )
    ).first()
    if event is None:
        raise api_error(404, ErrorCode.NOT_FOUND, "Event not found")

    division = body.division if body.division is not None else song.division
    normalized = js_trim(division or "")
    round_ = body.round if body.round is not None else DEFAULT_ROUND

    # Rounds are an Open-only affordance for Classic; refused rather than
    # coerced, so a client sending one it is not entitled to finds out.
    if round_ != DEFAULT_ROUND:
        if not is_open_event(event.name):
            raise api_error(
                400,
                ErrorCode.BAD_REQUEST,
                "Round selection is only available for The Open",
            )
        if normalized != ROUND_SPLIT_DIVISION:
            raise api_error(
                400,
                ErrorCode.BAD_REQUEST,
                "Round selection is only available for the "
                f"{ROUND_SPLIT_DIVISION} division",
            )

    slot_entity = song_entity_key(
        song.user_id, song.partner_id, song.managed_partnership_id
    )
    if await entity_slot_taken(
        session,
        event_id=body.event_id,
        entity=slot_entity,
        division=normalized,
        round_=round_,
        song_id=song.id,
    ):
        raise api_error(409, "ENTITY_SLOT_TAKEN", slot_taken_message(normalized))

    submission_id = str(uuid.uuid4())
    try:
        session.add(
            EventSongSubmission(
                id=submission_id,
                event_id=body.event_id,
                song_id=body.song_id,
                submitted_by_user_id=user_id,
                # Only an override the client sent is stored, normalized; the
                # song's own division stays the source of truth otherwise.
                division=(normalized or None) if body.division is not None else None,
                round=body.round,
                created_at=int(time.time() * 1000),
            )
        )
        await session.commit()
    except SQLAlchemyError as exc:
        await session.rollback()
        if is_unique_violation(exc):
            raise api_error(
                409, "conflict", "That song is already submitted to this event."
            ) from exc
        sentry_sdk.capture_exception(exc)
        logger.error(
            with_log_prefix(
                LOG_FAILURE,
                f"event_song_submission_create_failed user={user_id} "
                f"event={body.event_id} song={body.song_id}: {exc!r}",
            )
        )
        return _internal()

    # The copy runs on the scheduler tick, never here: a Drive outage or a
    # slow copy must not fail or delay a submission.
    await enqueue_copy_job(
        session,
        submission_id,
        context={"submission_id": submission_id, "user_id": user_id},
    )

    rows = await fetch_user_submission_rows(
        session, user_id, submission_id=submission_id
    )
    return success(map_submission_row(rows[0]))


@router.delete(
    "/{id}",
    response_model=None,  # 204: no body
    status_code=204,
    response_class=Response,
    summary="Withdraw a submission",
    description=(
        "Deletes one of the caller's submissions; after the delete, queues its "
        "Drive copy, if it has one, for trashing. Requires "
        "deejaytools.submissions.write."
    ),
    responses={
        **AUTH,
        404: {"model": ErrorResponse, "description": "Not the caller's."},
        500: {"model": ErrorResponse, "description": "The delete failed."},
    },
)
async def delete_submission(
    id: SubmissionId,
    caller: Caller = Depends(require_scope("deejaytools.submissions.write")),
    session: AsyncSession = Depends(get_db_session),
) -> Response:
    """Withdraw a submission."""
    user_id = caller.user_id
    existing = (
        await session.execute(
            select(EventSongSubmission.id)
            .where(
                EventSongSubmission.id == id,
                EventSongSubmission.submitted_by_user_id == user_id,
            )
            .limit(1)
        )
    ).first()
    if existing is None:
        raise api_error(404, ErrorCode.NOT_FOUND, "Event song submission not found")

    try:
        # RETURNING, not the copy id read above: a copy job finishing in
        # between records a copy this delete must trash.
        copy_file_id = (
            await session.execute(
                delete(EventSongSubmission)
                .where(
                    EventSongSubmission.id == id,
                    EventSongSubmission.submitted_by_user_id == user_id,
                )
                .returning(EventSongSubmission.drive_copy_file_id)
            )
        ).scalar_one_or_none()
        await session.commit()
    except SQLAlchemyError as exc:
        await session.rollback()
        sentry_sdk.capture_exception(exc)
        logger.error(
            with_log_prefix(
                LOG_FAILURE,
                f"event_song_submission_delete_failed user={user_id} "
                f"submission={id}: {exc!r}",
            )
        )
        return _internal()

    # Enqueued after the delete commits, so a failed delete never trashes the
    # copy of a submission that still exists.
    if copy_file_id:
        await enqueue_trash_jobs(
            session,
            [copy_file_id],
            source="submission_delete",
            context={"submission_id": id, "user_id": user_id},
        )
    return Response(status_code=204)
