"""``/v1/events/{id}/song-submissions`` and ``/v1/events/{id}/registrations``:
an event's entries and registration list, for its staff (ADR-0012).

What ``GET /v1/admin/event-song-submissions`` and
``GET /v1/admin/events/{id}/registrations`` answer for one event, outside
``/v1/admin``, so the event's coordinator, DJ and event manager can see who is
entered and who is still missing music. Event-scoped
``deejaytools.entries.read``: the scope held through a role (an admin's)
allows on any event, a grant of it on ``event:<id>`` allows on that event
only. The queries and the responses are the admin routes', except that an
entry's admin ``note`` is null for every caller but an admin.
"""

from __future__ import annotations

from typing import Annotated, Any

import sentry_sdk
from fastapi import APIRouter, Depends, Path
from fastapi.responses import JSONResponse
from mini_app_polis.logger import LOG_FAILURE, get_logger, with_log_prefix
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import Caller, EventRef, is_admin, require_event_scope
from ..database import get_db_session
from ..errors import ErrorCode, ErrorResponse, api_error, error_body, success_list
from ..models import Event
from .admin_event_registrations import RegistrationListResponse, registration_rows
from .admin_event_submissions import (
    AdminSubmissionListResponse,
    event_submissions_response,
)

logger = get_logger()

router = APIRouter(prefix="/v1/events", tags=["event-entries"])

THIS_EVENT = EventRef(source="path", name="id")


@router.get(
    "/{id}/song-submissions",
    response_model=AdminSubmissionListResponse,
    summary="An event's submissions, for its staff",
    description=(
        "Every submission to the event, newest first, as "
        "GET /v1/admin/event-song-submissions answers it. Requires "
        "deejaytools.entries.read on the event."
    ),
    responses={
        401: {"model": ErrorResponse, "description": "Missing or invalid token."},
        403: {"model": ErrorResponse, "description": "Lacks the scope on this event."},
        404: {"model": ErrorResponse, "description": "No such event."},
        500: {"model": ErrorResponse, "description": "The query failed."},
    },
)
async def list_event_entries(
    id: Annotated[str, Path(description="Event id.")],
    caller: Caller = Depends(
        require_event_scope("deejaytools.entries.read", THIS_EVENT)
    ),
    session: AsyncSession = Depends(get_db_session),
) -> Any:
    """List the event's submissions."""
    if await session.get(Event, id) is None:
        raise api_error(404, ErrorCode.NOT_FOUND, "Event not found")
    return await event_submissions_response(session, id, whole_emails=is_admin(caller))


@router.get(
    "/{id}/registrations",
    response_model=RegistrationListResponse,
    summary="An event's registration list, for its staff",
    description=(
        "The event's imported registration list, in order, as "
        "GET /v1/admin/events/{id}/registrations answers it, except that "
        "note is null for every caller but an admin; empty when none was "
        "imported. Requires deejaytools.entries.read on the event."
    ),
    responses={
        401: {"model": ErrorResponse, "description": "Missing or invalid token."},
        403: {"model": ErrorResponse, "description": "Lacks the scope on this event."},
        404: {"model": ErrorResponse, "description": "No such event."},
        500: {"model": ErrorResponse, "description": "The query failed."},
    },
)
async def list_event_registrations(
    id: Annotated[str, Path(description="Event id.")],
    caller: Caller = Depends(
        require_event_scope("deejaytools.entries.read", THIS_EVENT)
    ),
    session: AsyncSession = Depends(get_db_session),
) -> Any:
    """List the event's registrations."""
    if await session.get(Event, id) is None:
        raise api_error(404, ErrorCode.NOT_FOUND, "Event not found")
    try:
        rows = await registration_rows(session, id)
    except SQLAlchemyError as exc:
        sentry_sdk.capture_exception(exc)
        logger.error(
            with_log_prefix(
                LOG_FAILURE, f"event_registration_list_failed event={id}: {exc!r}"
            )
        )
        return JSONResponse(
            status_code=500,
            content=error_body(ErrorCode.INTERNAL, "Internal server error"),
        )
    if not is_admin(caller):
        rows = [{**row, "note": None} for row in rows]
    return success_list(rows)
