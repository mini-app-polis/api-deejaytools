"""``/v1/admin/events/{id}/registrations`` and
``/v1/admin/event-registrations/{id}`` (docs/API.md).

An event's registration list, imported by an admin from the event's own
registration export, so the admin page can show who has not submitted
music yet. Optional per event. Reads are behind
``deejaytools.registrations.read``, changes behind
``deejaytools.registrations.write``; both are the admin role's only.

The list is plain data: matching it against the event's submissions is the
web app's job. The API keeps the admin's marks (``linked_label``,
``handled``, ``scratched``, ``note``) across re-imports, by each entry's
normalized (division, dancer_1, dancer_2).

Scratching an entry (signed up, no longer competing) keeps its songs
submitted to the event but hides the ones the app matched to it from the
event's public entry list, through ``event_song_submissions.hidden_by_registration_id``.
"""

from __future__ import annotations

import unicodedata
import uuid
from collections import defaultdict
from typing import Annotated, Any, ClassVar

import sentry_sdk
from fastapi import APIRouter, Depends, Path
from fastapi.responses import JSONResponse
from mini_app_polis.logger import LOG_FAILURE, get_logger, with_log_prefix
from pydantic import BaseModel, Field
from sqlalchemy import delete, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import Caller, require_scope
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
from ..models import Event, EventRegistration, EventSongSubmission
from ..validation import ZodModel
from ..zod_coerce import js_trim
from ..zod_types import trimmed, zod_body

logger = get_logger()

router = APIRouter(prefix="/v1/admin", tags=["admin-event-registrations"])

#: Most entries one import may carry.
MAX_ENTRIES = 2000

Text200 = Annotated[str, trimmed(min_length=1, max_length=200)]
OptionalText200 = Annotated[str, trimmed(max_length=200)]
Note = Annotated[str, trimmed(max_length=1000)]


def entry_key(division: str, dancer_1: str, dancer_2: str | None) -> str:
    """An entry's identity across imports: case, accents and spacing ignored."""

    def norm(value: str | None) -> str:
        decomposed = unicodedata.normalize("NFKD", value or "")
        bare = "".join(c for c in decomposed if not unicodedata.combining(c))
        return " ".join(bare.casefold().split())

    return "|".join((norm(division), norm(dancer_1), norm(dancer_2)))


class RegistrationItem(ZodModel):
    """One entry as an import sends it."""

    division: Text200 = Field(
        ..., description="Division, as the registration names it."
    )
    dancer_1: Text200 = Field(..., description="First dancer (or team name).")
    dancer_2: OptionalText200 | None = Field(
        None, description="Second dancer; omit or empty for none."
    )


class PutRegistrationsBody(ZodModel):
    """Body of ``PUT /v1/admin/events/{id}/registrations``."""

    entries: list[RegistrationItem] = Field(
        ...,
        max_length=MAX_ENTRIES,
        description="The whole list, in order. An empty list clears it.",
    )


class PatchRegistrationBody(ZodModel):
    """Body of ``PATCH /v1/admin/event-registrations/{id}``.

    Every field is optional; ``linked_label`` and ``note`` take null to clear.
    """

    _NULLABLE: ClassVar[frozenset[str]] = frozenset({"linked_label", "note"})

    linked_label: OptionalText200 | None = Field(
        None, description="A submission's partnership label; null or empty clears it."
    )
    handled: bool | None = Field(
        None, description="Needs no submission here (handled elsewhere)."
    )
    note: Note | None = Field(None, description="Admin note; null or empty clears it.")
    scratched: bool | None = Field(
        None,
        description=(
            "Signed up but no longer competing. Unscratching unhides every "
            "submission it hid."
        ),
    )
    hide_submission_ids: list[Annotated[str, trimmed(min_length=1)]] | None = Field(
        None,
        max_length=50,
        description=(
            "With a scratched entry: the event's submissions to hide from its "
            "public entry list, replacing those it hid before. Ids of other "
            "events' submissions are ignored."
        ),
    )


class RegistrationData(BaseModel):
    """A registration entry as the API returns it."""

    id: str = Field(..., description="Entry id.")
    event_id: str = Field(..., description="Event id.")
    position: int = Field(..., description="Order in the imported list, from 0.")
    division: str = Field(..., description="Division, as the registration names it.")
    dancer_1: str = Field(..., description="First dancer (or team name).")
    dancer_2: str | None = Field(None, description="Second dancer, if any.")
    linked_label: str | None = Field(
        None, description="Partnership label the admin linked it to by hand."
    )
    handled: bool = Field(..., description="Marked as needing no submission here.")
    note: str | None = Field(None, description="Admin note.")
    scratched: bool = Field(..., description="Signed up but no longer competing.")
    hidden_submission_ids: list[str] = Field(
        ...,
        description="The event's submissions it hides from the public entry list.",
    )
    created_at: int = Field(..., description="First imported, epoch ms.")
    updated_at: int = Field(..., description="Last changed, epoch ms.")


class RegistrationResponse(Envelope[RegistrationData]):
    """One registration entry."""

    data: RegistrationData = Field(..., description="The entry.")


class RegistrationListResponse(Envelope[list[RegistrationData]]):
    """An event's registration list, in imported order."""

    data: list[RegistrationData] = Field(..., description="The entries.")


def map_registration(
    row: EventRegistration, hidden: list[str] | None = None
) -> dict[str, Any]:
    """An event_registrations row on the wire, with the submissions it hides."""
    return {
        "id": row.id,
        "event_id": row.event_id,
        "position": row.position,
        "division": row.division,
        "dancer_1": row.dancer_1,
        "dancer_2": row.dancer_2,
        "linked_label": row.linked_label,
        "handled": row.handled,
        "note": row.note,
        "scratched": row.scratched,
        "hidden_submission_ids": sorted(hidden or []),
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _internal() -> JSONResponse:
    return JSONResponse(
        status_code=500, content=error_body(ErrorCode.INTERNAL, "Internal server error")
    )


async def _require_event(session: AsyncSession, event_id: str) -> None:
    found = (
        await session.execute(select(Event.id).where(Event.id == event_id).limit(1))
    ).scalar_one_or_none()
    if found is None:
        raise api_error(404, ErrorCode.NOT_FOUND, "Event not found")


async def _hidden_by(
    session: AsyncSession, registration_ids: list[str]
) -> dict[str, list[str]]:
    """Each registration's hidden submission ids."""
    out: dict[str, list[str]] = {}
    if not registration_ids:
        return out
    rows = (
        await session.execute(
            select(
                EventSongSubmission.hidden_by_registration_id, EventSongSubmission.id
            ).where(EventSongSubmission.hidden_by_registration_id.in_(registration_ids))
        )
    ).all()
    for reg_id, sub_id in rows:
        if reg_id is not None:
            out.setdefault(reg_id, []).append(sub_id)
    return out


async def _list(session: AsyncSession, event_id: str) -> list[dict[str, Any]]:
    rows = (
        (
            await session.execute(
                select(EventRegistration)
                .where(EventRegistration.event_id == event_id)
                .order_by(EventRegistration.position)
            )
        )
        .scalars()
        .all()
    )
    hidden = await _hidden_by(session, [r.id for r in rows])
    return [map_registration(r, hidden.get(r.id)) for r in rows]


EventId = Annotated[str, Path(description="Event id.")]
RegistrationId = Annotated[str, Path(description="Registration entry id.")]
AUTH: dict[int | str, dict[str, Any]] = {
    401: {"model": ErrorResponse, "description": "Missing or invalid token."},
    403: {"model": ErrorResponse, "description": "Lacks the scope."},
}
INTERNAL: dict[int | str, dict[str, Any]] = {
    500: {"model": ErrorResponse, "description": "The query or write failed."}
}


@router.get(
    "/events/{id}/registrations",
    response_model=RegistrationListResponse,
    summary="List an event's registrations",
    description=(
        "The event's imported registration list, in order; empty when none "
        "was imported. Requires deejaytools.registrations.read."
    ),
    responses={
        **AUTH,
        404: {"model": ErrorResponse, "description": "No such event."},
        **INTERNAL,
    },
)
async def list_registrations(
    id: EventId,
    _caller: Caller = Depends(require_scope("deejaytools.registrations.read")),
    session: AsyncSession = Depends(get_db_session),
) -> Any:
    """List an event's registrations."""
    await _require_event(session, id)
    try:
        return success_list(await _list(session, id))
    except SQLAlchemyError as exc:
        sentry_sdk.capture_exception(exc)
        logger.error(
            with_log_prefix(
                LOG_FAILURE, f"registration_list_failed event={id}: {exc!r}"
            )
        )
        return _internal()


@router.put(
    "/events/{id}/registrations",
    response_model=RegistrationListResponse,
    summary="Replace an event's registrations",
    description=(
        "Replaces the event's list with entries, in order. An entry whose "
        "division and dancers match one already there (case, accents and "
        "spacing ignored) keeps its id and its marks; the rest are new, and "
        "entries no longer sent are removed. Requires "
        "deejaytools.registrations.write."
    ),
    responses={
        **AUTH,
        400: {"model": ErrorResponse, "description": "Invalid body."},
        404: {"model": ErrorResponse, "description": "No such event."},
        **INTERNAL,
    },
)
async def put_registrations(
    id: EventId,
    caller: Caller = Depends(require_scope("deejaytools.registrations.write")),
    body: PutRegistrationsBody = Depends(zod_body(PutRegistrationsBody)),
    session: AsyncSession = Depends(get_db_session),
) -> Any:
    """Replace an event's registration list, keeping marks on unchanged entries."""
    await _require_event(session, id)
    now = now_ms()
    try:
        existing = (
            (
                await session.execute(
                    select(EventRegistration)
                    .where(EventRegistration.event_id == id)
                    .order_by(EventRegistration.position)
                )
            )
            .scalars()
            .all()
        )
        # A key can repeat (one couple listed twice); reuse in order.
        by_key: dict[str, list[EventRegistration]] = defaultdict(list)
        for row in existing:
            by_key[row.entry_key].append(row)

        kept: set[str] = set()
        for position, item in enumerate(body.entries):
            dancer_2 = js_trim(item.dancer_2 or "") or None
            key = entry_key(item.division, item.dancer_1, dancer_2)
            matches = by_key.get(key)
            if matches:
                row = matches.pop(0)
                kept.add(row.id)
                row.position = position
                row.division = item.division
                row.dancer_1 = item.dancer_1
                row.dancer_2 = dancer_2
                row.updated_at = now
            else:
                session.add(
                    EventRegistration(
                        id=str(uuid.uuid4()),
                        event_id=id,
                        position=position,
                        division=item.division,
                        dancer_1=item.dancer_1,
                        dancer_2=dancer_2,
                        entry_key=key,
                        handled=False,
                        created_at=now,
                        updated_at=now,
                    )
                )
        gone = [row.id for row in existing if row.id not in kept]
        if gone:
            await session.execute(
                delete(EventRegistration).where(EventRegistration.id.in_(gone))
            )
        await session.commit()
        return success_list(await _list(session, id))
    except SQLAlchemyError as exc:
        await session.rollback()
        sentry_sdk.capture_exception(exc)
        logger.error(
            with_log_prefix(
                LOG_FAILURE,
                f"registration_import_failed event={id} user={caller.user_id}: {exc!r}",
            )
        )
        return _internal()


@router.patch(
    "/event-registrations/{id}",
    response_model=RegistrationResponse,
    summary="Mark a registration entry",
    description=(
        "Sets the admin's marks on one entry: linked_label, handled, note and "
        "scratched; fields not sent are left as they are. A scratched entry "
        "hides hide_submission_ids from the event's public entry list; "
        "unscratching unhides them. Requires deejaytools.registrations.write."
    ),
    responses={
        **AUTH,
        400: {"model": ErrorResponse, "description": "Invalid body."},
        404: {"model": ErrorResponse, "description": "No such entry."},
        **INTERNAL,
    },
)
async def patch_registration(
    id: RegistrationId,
    caller: Caller = Depends(require_scope("deejaytools.registrations.write")),
    body: PatchRegistrationBody = Depends(zod_body(PatchRegistrationBody)),
    session: AsyncSession = Depends(get_db_session),
) -> Any:
    """Set an entry's marks."""
    row = await session.get(EventRegistration, id)
    if row is None:
        raise api_error(404, ErrorCode.NOT_FOUND, "Registration not found")
    sent = body.model_fields_set
    scratched = (
        body.scratched
        if "scratched" in sent and body.scratched is not None
        else row.scratched
    )
    if body.hide_submission_ids is not None and not scratched:
        raise api_error(
            400,
            ErrorCode.BAD_REQUEST,
            "hide_submission_ids needs a scratched entry",
        )
    if "linked_label" in sent:
        row.linked_label = js_trim(body.linked_label or "") or None
    if "handled" in sent and body.handled is not None:
        row.handled = body.handled
    if "note" in sent:
        row.note = js_trim(body.note or "") or None
    row.scratched = scratched
    if not sent & {
        "linked_label",
        "handled",
        "note",
        "scratched",
        "hide_submission_ids",
    }:
        return success(map_registration(row, (await _hidden_by(session, [id])).get(id)))
    row.updated_at = now_ms()
    try:
        if not scratched or body.hide_submission_ids is not None:
            await session.execute(
                update(EventSongSubmission)
                .where(EventSongSubmission.hidden_by_registration_id == id)
                .values(hidden_by_registration_id=None)
            )
        if scratched and body.hide_submission_ids:
            await session.execute(
                update(EventSongSubmission)
                .where(
                    EventSongSubmission.id.in_(body.hide_submission_ids),
                    EventSongSubmission.event_id == row.event_id,
                )
                .values(hidden_by_registration_id=id)
            )
        await session.commit()
    except SQLAlchemyError as exc:
        await session.rollback()
        sentry_sdk.capture_exception(exc)
        logger.error(
            with_log_prefix(
                LOG_FAILURE,
                f"registration_patch_failed id={id} user={caller.user_id}: {exc!r}",
            )
        )
        return _internal()
    await session.refresh(row)
    return success(map_registration(row, (await _hidden_by(session, [id])).get(id)))
