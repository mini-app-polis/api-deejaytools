"""``/v1/events/{id}/song-submissions``: an event's entries, for its staff (ADR-0012).

What ``GET /v1/admin/event-song-submissions`` answers for one event, outside
``/v1/admin``, so the event's coordinator, DJ and event manager can see it.
Event-scoped ``deejaytools.entries.read``: the scope held through a role (an
admin's) allows on any event, a grant of it on ``event:<id>`` allows on that
event only. The query and the response are the admin list's.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import Caller, EventRef, is_admin, require_event_scope
from ..database import get_db_session
from ..errors import ErrorCode, ErrorResponse, api_error
from ..models import Event
from .admin_event_submissions import (
    AdminSubmissionListResponse,
    event_submissions_response,
)

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
