"""``/v1/admin/event-song-submissions`` (docs/API.md).

Every submission to one event, for the Manager's Event Songs tab, behind
``deejaytools.entries.read`` (ADR-0008). The query and its answer are
``event_submissions_response``, which the event staff's
``GET /v1/events/{id}/song-submissions`` shares (ADR-0012).
"""

from __future__ import annotations

from typing import Annotated, Any

import sentry_sdk
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from mini_app_polis.logger import LOG_FAILURE, get_logger, with_log_prefix
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from ..auth import Caller, require_scope
from ..database import get_db_session
from ..errors import Envelope, ErrorCode, ErrorResponse, error_body, success_list
from ..event_connections import email_for_caller
from ..labels import build_structured_song_label, partnership_label
from ..models import Event, EventSongSubmission, ManagedPartnership, Partner, Song, User
from ..validation import ZodModel
from ..zod_types import QueryStr, zod_query

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
    division: str | None = Field(None, description="The song's division.")
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
    owner = aliased(User, name="song_owner")
    submitter = aliased(User, name="submitter")
    managed = aliased(ManagedPartnership, name="managed_partnership")
    try:
        rows = (
            await session.execute(
                select(
                    EventSongSubmission.id,
                    EventSongSubmission.event_id,
                    EventSongSubmission.created_at,
                    Event.name.label("event_name"),
                    Song.id.label("song_id"),
                    Song.user_id.label("owner_user_id"),
                    Song.division.label("song_division"),
                    Song.display_name.label("song_display_name"),
                    Song.processed_filename.label("song_processed_filename"),
                    Song.routine_name.label("song_routine_name"),
                    Song.season_year.label("song_season_year"),
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
                .join(
                    submitter, submitter.id == EventSongSubmission.submitted_by_user_id
                )
                .where(EventSongSubmission.event_id == event_id)
                .order_by(EventSongSubmission.created_at.desc())
            )
        ).all()

        data = []
        for r in rows:
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
                division=r.song_division,
            )
            data.append(
                {
                    "id": r.id,
                    "event_id": r.event_id,
                    "event_name": r.event_name,
                    "division": r.song_division,
                    "song_id": r.song_id,
                    "owner_user_id": r.owner_user_id,
                    "song_label": build_structured_song_label(
                        partnership=label,
                        division=r.song_division,
                        season_year=r.song_season_year,
                        routine_name=r.song_routine_name,
                        processed_filename=r.song_processed_filename,
                        display_name=r.song_display_name,
                        song_id=r.song_id,
                    ),
                    "partnership_label": label,
                    "submitter_email": email_for_caller(
                        r.submitter_email, whole=whole_emails
                    )
                    or "",
                    "created_at": r.created_at,
                }
            )
        return success_list(data)
    except SQLAlchemyError as exc:
        sentry_sdk.capture_exception(exc)
        logger.error(
            with_log_prefix(
                LOG_FAILURE,
                f"admin_event_song_submission_list_failed event={event_id}: {exc!r}",
            )
        )
        return JSONResponse(
            status_code=500,
            content=error_body(ErrorCode.INTERNAL, "Internal server error"),
        )
