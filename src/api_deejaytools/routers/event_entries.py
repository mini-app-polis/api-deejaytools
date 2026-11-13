"""``/v1/events/{id}/song-submissions`` and ``/v1/events/{id}/registrations``:
an event's entries and registration list, for its staff (ADR-0012).

Reading the registration list takes event-scoped ``deejaytools.entries.read``;
importing it (replacing it, or adding only the entries not on it yet), adding
or removing one entry, and marking an entry take
event-scoped ``deejaytools.registrations.write``, which every staff role
carries on its event. Only an admin sees or sets an entry's ``note``.

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

import uuid
from typing import Annotated, Any

import sentry_sdk
from fastapi import APIRouter, Depends, Path
from fastapi.responses import JSONResponse
from mini_app_polis.logger import LOG_FAILURE, get_logger, with_log_prefix
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import Caller, EventRef, is_admin, require_event_scope
from ..clock import now_ms
from ..database import get_db_session
from ..errors import (
    Envelope,
    ErrorCode,
    ErrorResponse,
    api_error,
    error_body,
    success,
    success_list,
)
from ..models import Event, EventRegistration
from ..validation import ZodModel
from ..zod_coerce import js_trim
from ..zod_types import zod_body
from .admin_event_registrations import (
    OptionalText200,
    PatchRegistrationBody,
    PutRegistrationsBody,
    RegistrationListResponse,
    RegistrationResponse,
    Text200,
    append_registrations,
    entry_key,
    map_registration,
    mark_registration,
    registration_rows,
    replace_registrations,
    without_notes,
)
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
    return success_list(rows if is_admin(caller) else without_notes(rows))


EntryId = Annotated[str, Path(description="Registration entry id.")]
WRITE_RESPONSES: dict[int | str, dict[str, Any]] = {
    401: {"model": ErrorResponse, "description": "Missing or invalid token."},
    403: {"model": ErrorResponse, "description": "Lacks the scope on this event."},
    500: {"model": ErrorResponse, "description": "The write failed."},
}


class AddRegistrationBody(ZodModel):
    """Body of ``POST /v1/events/{id}/registrations``: one entry."""

    division: Text200 = Field(
        ..., description="Division, as the registration names it."
    )
    dancer_1: Text200 = Field(..., description="First dancer (or team name).")
    dancer_2: OptionalText200 | None = Field(
        None, description="Second dancer; omit or empty for none."
    )


class AppendedData(BaseModel):
    """What an add-only import did."""

    added: int = Field(..., description="Entries appended to the list.")
    skipped: int = Field(
        ...,
        description=(
            "Entries sent but not added: already on the list, or repeating "
            "one earlier in the request."
        ),
    )


class AppendedResponse(Envelope[AppendedData]):
    """An add-only import's counts."""

    data: AppendedData = Field(..., description="The counts.")


class RemovedData(BaseModel):
    """Confirmation that an entry was removed."""

    deleted: bool = Field(..., description="Always true.")


class RemovedResponse(Envelope[RemovedData]):
    """An entry removed."""

    data: RemovedData = Field(..., description="Confirmation.")


async def _require_event(session: AsyncSession, event_id: str) -> None:
    if await session.get(Event, event_id) is None:
        raise api_error(404, ErrorCode.NOT_FOUND, "Event not found")


async def _event_entry(
    session: AsyncSession, event_id: str, entry_id: str
) -> EventRegistration:
    """The entry ``entry_id`` on ``event_id``'s list; 404 when it is not there."""
    row = await session.get(EventRegistration, entry_id)
    if row is None or row.event_id != event_id:
        raise api_error(404, ErrorCode.NOT_FOUND, "Registration not found")
    return row


def _write_failed(what: str, exc: SQLAlchemyError) -> JSONResponse:
    sentry_sdk.capture_exception(exc)
    logger.error(with_log_prefix(LOG_FAILURE, f"{what}: {exc!r}"))
    return JSONResponse(
        status_code=500,
        content=error_body(ErrorCode.INTERNAL, "Internal server error"),
    )


@router.put(
    "/{id}/registrations",
    response_model=RegistrationListResponse,
    summary="Replace an event's registration list, for its staff",
    description=(
        "As PUT /v1/admin/events/{id}/registrations: replaces the list with "
        "entries, in order, keeping the marks on entries already there. note "
        "is null in the answer for every caller but an admin. Requires "
        "deejaytools.registrations.write on the event."
    ),
    responses={
        **WRITE_RESPONSES,
        400: {"model": ErrorResponse, "description": "Invalid body."},
        404: {"model": ErrorResponse, "description": "No such event."},
    },
)
async def import_event_registrations(
    id: Annotated[str, Path(description="Event id.")],
    caller: Caller = Depends(
        require_event_scope("deejaytools.registrations.write", THIS_EVENT)
    ),
    body: PutRegistrationsBody = Depends(zod_body(PutRegistrationsBody)),
    session: AsyncSession = Depends(get_db_session),
) -> Any:
    """Replace the event's registration list."""
    await _require_event(session, id)
    return await replace_registrations(
        session, id, body, caller, show_note=is_admin(caller)
    )


@router.post(
    "/{id}/registrations/append",
    response_model=AppendedResponse,
    summary="Add the entries not yet on an event's registration list",
    description=(
        "An add-only import: of entries, appends those whose division and "
        "dancers match none already on the list (case, accents and spacing "
        "ignored), in the order sent, after the entries already there. "
        "Entries already there, their marks and their order are left as they "
        "are; a repeat within entries counts once. Answers how many were "
        "added and skipped. Requires deejaytools.registrations.write on the "
        "event."
    ),
    responses={
        **WRITE_RESPONSES,
        400: {"model": ErrorResponse, "description": "Invalid body."},
        404: {"model": ErrorResponse, "description": "No such event."},
    },
)
async def append_event_registrations(
    id: Annotated[str, Path(description="Event id.")],
    caller: Caller = Depends(
        require_event_scope("deejaytools.registrations.write", THIS_EVENT)
    ),
    body: PutRegistrationsBody = Depends(zod_body(PutRegistrationsBody)),
    session: AsyncSession = Depends(get_db_session),
) -> Any:
    """Append the entries not yet on the event's list."""
    await _require_event(session, id)
    return await append_registrations(session, id, body, caller)


@router.post(
    "/{id}/registrations",
    status_code=201,
    response_model=RegistrationResponse,
    summary="Add one entry to an event's registration list",
    description=(
        "Adds one entry at the end of the event's list, unmarked: someone "
        "who registered after the list was imported, or one the import "
        "missed. Requires deejaytools.registrations.write on the event."
    ),
    responses={
        **WRITE_RESPONSES,
        400: {"model": ErrorResponse, "description": "Invalid body."},
        404: {"model": ErrorResponse, "description": "No such event."},
    },
)
async def add_event_registration(
    id: Annotated[str, Path(description="Event id.")],
    caller: Caller = Depends(
        require_event_scope("deejaytools.registrations.write", THIS_EVENT)
    ),
    body: AddRegistrationBody = Depends(zod_body(AddRegistrationBody)),
    session: AsyncSession = Depends(get_db_session),
) -> Any:
    """Add one entry at the end of the event's list."""
    await _require_event(session, id)
    dancer_2 = js_trim(body.dancer_2 or "") or None
    now = now_ms()
    try:
        last = (
            await session.execute(
                select(func.max(EventRegistration.position)).where(
                    EventRegistration.event_id == id
                )
            )
        ).scalar_one_or_none()
        row = EventRegistration(
            id=str(uuid.uuid4()),
            event_id=id,
            position=0 if last is None else last + 1,
            division=body.division,
            dancer_1=body.dancer_1,
            dancer_2=dancer_2,
            entry_key=entry_key(body.division, body.dancer_1, dancer_2),
            handled=False,
            created_at=now,
            updated_at=now,
        )
        session.add(row)
        await session.commit()
    except SQLAlchemyError as exc:
        await session.rollback()
        return _write_failed(
            f"event_registration_add_failed event={id} user={caller.user_id}", exc
        )
    await session.refresh(row)
    return success(map_registration(row, []))


@router.patch(
    "/{id}/registrations/{entry_id}",
    response_model=RegistrationResponse,
    summary="Mark an entry on an event's registration list",
    description=(
        "As PATCH /v1/admin/event-registrations/{id}, for an entry on this "
        "event's list. Only an admin may send note; note is null in the "
        "answer for every caller but an admin. Requires "
        "deejaytools.registrations.write on the event."
    ),
    responses={
        **WRITE_RESPONSES,
        400: {"model": ErrorResponse, "description": "Invalid body."},
        404: {"model": ErrorResponse, "description": "No such entry on this event."},
    },
)
async def mark_event_registration(
    id: Annotated[str, Path(description="Event id.")],
    entry_id: EntryId,
    caller: Caller = Depends(
        require_event_scope("deejaytools.registrations.write", THIS_EVENT)
    ),
    body: PatchRegistrationBody = Depends(zod_body(PatchRegistrationBody)),
    session: AsyncSession = Depends(get_db_session),
) -> Any:
    """Set an entry's marks."""
    admin = is_admin(caller)
    if "note" in body.model_fields_set and not admin:
        raise api_error(403, ErrorCode.FORBIDDEN, "Only an admin can set the note")
    row = await _event_entry(session, id, entry_id)
    return await mark_registration(session, row, body, caller, show_note=admin)


@router.delete(
    "/{id}/registrations/{entry_id}",
    response_model=RemovedResponse,
    summary="Remove an entry from an event's registration list",
    description=(
        "Removes one entry. Submissions it hid from the public entry list "
        "are unhidden. Requires deejaytools.registrations.write on the event."
    ),
    responses={
        **WRITE_RESPONSES,
        404: {"model": ErrorResponse, "description": "No such entry on this event."},
    },
)
async def remove_event_registration(
    id: Annotated[str, Path(description="Event id.")],
    entry_id: EntryId,
    caller: Caller = Depends(
        require_event_scope("deejaytools.registrations.write", THIS_EVENT)
    ),
    session: AsyncSession = Depends(get_db_session),
) -> Any:
    """Remove one entry."""
    await _event_entry(session, id, entry_id)
    try:
        # event_song_submissions.hidden_by_registration_id is ON DELETE SET NULL.
        await session.execute(
            delete(EventRegistration).where(EventRegistration.id == entry_id)
        )
        await session.commit()
    except SQLAlchemyError as exc:
        await session.rollback()
        return _write_failed(
            f"event_registration_remove_failed id={entry_id} user={caller.user_id}",
            exc,
        )
    return success({"deleted": True})
