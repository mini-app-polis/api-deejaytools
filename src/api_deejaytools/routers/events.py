"""``/v1/events`` (deejaytools-api docs/API.md, src/routes/events.ts).

Reads of an event are public; who is entered is behind
``deejaytools.entities.read``; changes are behind ``deejaytools.events.write``.
"""

from __future__ import annotations

import re
import time
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path
from fastapi.responses import JSONResponse
from mini_app_polis.logger import LOG_FAILURE, get_logger, with_log_prefix
from pydantic import AfterValidator, BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from ..auth import Caller, require_scope
from ..database import get_db_session
from ..domain import (
    DEFAULT_TIMEZONE,
    DIVISIONS,
    canonical_timezone,
    full_name,
    locale_key,
    partnership_display,
    season_year_from_date_string,
    song_entity_key,
    today_in_zone,
)
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
from ..models import (
    Checkin,
    Event,
    EventSongSubmission,
    ManagedPartnership,
    Partner,
    QueueEntry,
    QueueEvent,
    Run,
    Session,
    SessionDivision,
    Song,
    User,
    event_division_run_limits,
)
from ..services.drive_jobs import enqueue_trash_jobs
from ..validation import NonEmptyStr, ZodModel

logger = get_logger()

router = APIRouter(prefix="/v1/events", tags=["events"])

_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_YEAR = re.compile(r"^\d{4}$")


def _date(value: str) -> str:
    if not _DATE.match(value):
        raise ValueError("Must be YYYY-MM-DD")
    return value


def _season_year(value: str) -> str:
    if not _YEAR.match(value):
        raise ValueError("Must be a 4-digit year")
    return value


def _timezone(value: str) -> str:
    # Stored as sent, as deejaytools-api stores it; checked the way Intl
    # checks it (any IANA name, case-insensitively).
    if not value or canonical_timezone(value) is None:
        raise ValueError("Must be a valid IANA timezone (e.g. 'America/Chicago')")
    return value


DateString = Annotated[str, AfterValidator(_date)]
SeasonYear = Annotated[str, AfterValidator(_season_year)]
Timezone = Annotated[str, AfterValidator(_timezone)]


class CreateEventBody(ZodModel):
    """Body of ``POST /v1/events``."""

    name: NonEmptyStr = Field(..., description="Event name.")
    start_date: DateString = Field(..., description="First day, YYYY-MM-DD, local.")
    end_date: DateString = Field(..., description="Last day, YYYY-MM-DD, local.")
    timezone: Timezone = Field(DEFAULT_TIMEZONE, description="IANA timezone.")
    season_year: SeasonYear | None = Field(
        None, description="Season; omit to derive from start_date."
    )


class PatchEventBody(ZodModel):
    """Body of ``PATCH /v1/events/{id}``. Every field optional."""

    name: NonEmptyStr | None = Field(None, description="Event name.")
    start_date: DateString | None = Field(None, description="First day, YYYY-MM-DD.")
    end_date: DateString | None = Field(None, description="Last day, YYYY-MM-DD.")
    timezone: Timezone | None = Field(None, description="IANA timezone.")
    season_year: SeasonYear | None = Field(None, description="Season year.")


class EventData(BaseModel):
    """An event as the API returns it. ``status`` is derived, never stored."""

    id: str = Field(..., description="Event id.")
    name: str = Field(..., description="Event name.")
    start_date: str = Field(..., description="First day, YYYY-MM-DD, local.")
    end_date: str = Field(..., description="Last day, YYYY-MM-DD, local.")
    timezone: str = Field(..., description="IANA timezone of the dates.")
    season_year: str = Field(..., description="Competitive season.")
    status: str = Field(..., description="upcoming, active or completed, today.")
    created_by: str | None = Field(None, description="users.id of the creator.")
    created_at: int = Field(..., description="Created, epoch ms.")
    updated_at: int = Field(..., description="Last updated, epoch ms.")


class EventResponse(BaseModel):
    """One event."""

    data: EventData = Field(..., description="The event.")
    meta: Meta = Field(..., description="Response metadata.")


class EventListResponse(BaseModel):
    """Every event, newest start date first."""

    data: list[EventData] = Field(..., description="The events.")
    meta: ListMeta = Field(..., description="Response metadata.")


class EntityItem(BaseModel):
    """One competing entity in a division, without any song identity."""

    entity_key: str = Field(..., description="mp:, pt: or us: key of the entity.")
    label: str = Field(..., description="Display name of the entity.")
    song_count: int = Field(..., description="Songs it has in this division.")


class DivisionEntities(BaseModel):
    """The entities entered in one division."""

    division: str = Field(..., description="Division name.")
    entities: list[EntityItem] = Field(..., description="Entities, by label.")


class EntitiesResponse(BaseModel):
    """Entities with at least one song submitted, grouped by division."""

    data: list[DivisionEntities] = Field(..., description="Divisions, in order.")
    meta: ListMeta = Field(..., description="Response metadata.")


class DeletedData(BaseModel):
    """Confirmation of a delete."""

    deleted: bool = Field(..., description="Always true.")


class DeletedResponse(BaseModel):
    """Delete confirmation envelope."""

    data: DeletedData = Field(..., description="Confirmation.")
    meta: Meta = Field(..., description="Response metadata.")


def compute_status(start_date: str, end_date: str, timezone: str) -> str:
    """upcoming / active / completed, with "today" taken in the event's own zone.

    Using the server's date (UTC) would mark a Chicago event completed at
    19:00 on its last day.
    """
    today = today_in_zone(timezone)
    if today < start_date:
        return "upcoming"
    if today > end_date:
        return "completed"
    return "active"


def map_event(row: Event) -> dict[str, Any]:
    """An events row on the wire."""
    return {
        "id": row.id,
        "name": row.name,
        "start_date": row.start_date,
        "end_date": row.end_date,
        "timezone": row.timezone,
        # Rows predating the column fall back, so consumers never see null.
        "season_year": row.season_year or season_year_from_date_string(row.start_date),
        "status": compute_status(row.start_date, row.end_date, row.timezone),
        "created_by": row.created_by,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _now_ms() -> int:
    return int(time.time() * 1000)


async def _load(session: AsyncSession, event_id: str) -> Event:
    row = await session.get(Event, event_id)
    if row is None:
        raise api_error(404, ErrorCode.NOT_FOUND, "Event not found")
    return row


EventId = Annotated[str, Path(description="Event id.")]
NOT_FOUND: dict[int | str, dict[str, Any]] = {
    404: {"model": ErrorResponse, "description": "No such event."}
}
ADMIN: dict[int | str, dict[str, Any]] = {
    401: {"model": ErrorResponse, "description": "Missing or invalid token."},
    403: {"model": ErrorResponse, "description": "Lacks deejaytools.events.write."},
}


@router.get(
    "",
    response_model=EventListResponse,
    summary="List events",
    description="Every event, newest start date first. Intentionally public.",
)
async def list_events(
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """List events. Public."""
    rows = (
        (
            await session.execute(
                select(Event).order_by(Event.start_date.desc(), Event.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    return success_list([map_event(r) for r in rows])


@router.get(
    "/{id}",
    response_model=EventResponse,
    summary="Get an event",
    description=(
        "One event's name and dates. Intentionally public: the event listing "
        "is the signed-out entry point. Who is entered is /entities."
    ),
    responses=NOT_FOUND,
)
async def get_event(
    id: EventId, session: AsyncSession = Depends(get_db_session)
) -> dict[str, Any]:
    """Get one event. Public."""
    return success(map_event(await _load(session, id)))


UNSPECIFIED_DIVISION = "Unspecified"


def _division_order(division: str) -> int:
    """Canonical position; Unspecified sorts last, unknown divisions before it."""
    if division == UNSPECIFIED_DIVISION:
        return len(DIVISIONS) + 1
    return DIVISIONS.index(division) if division in DIVISIONS else len(DIVISIONS)


@router.get(
    "/{id}/entities",
    response_model=EntitiesResponse,
    summary="Entities entered in an event",
    description=(
        "Entities with at least one song submitted to the event, grouped by "
        "division, with how many songs each has there. Carries no song "
        "identity. Requires deejaytools.entities.read."
    ),
    responses=NOT_FOUND,
)
async def event_entities(
    id: EventId,
    _caller: Caller = Depends(require_scope("deejaytools.entities.read")),
    session: AsyncSession = Depends(get_db_session),
) -> Any:
    """Entities per division for one event."""
    await _load(session, id)
    owner = aliased(User, name="song_owner")
    try:
        rows = (
            await session.execute(
                select(
                    Song.user_id,
                    Song.partner_id,
                    Song.managed_partnership_id,
                    Song.division.label("song_division"),
                    EventSongSubmission.division.label("submission_division"),
                    owner.first_name.label("owner_first"),
                    owner.last_name.label("owner_last"),
                    Partner.first_name.label("partner_first"),
                    Partner.last_name.label("partner_last"),
                    Partner.kind.label("partner_kind"),
                    ManagedPartnership.leader_first_name,
                    ManagedPartnership.leader_last_name,
                    ManagedPartnership.follower_first_name,
                    ManagedPartnership.follower_last_name,
                )
                .select_from(EventSongSubmission)
                .join(Song, Song.id == EventSongSubmission.song_id)
                .outerjoin(owner, owner.id == Song.user_id)
                .outerjoin(Partner, Partner.id == Song.partner_id)
                .outerjoin(
                    ManagedPartnership,
                    ManagedPartnership.id == Song.managed_partnership_id,
                )
                .where(EventSongSubmission.event_id == id)
            )
        ).all()
    except Exception as exc:  # noqa: BLE001 - logged, answered as deejaytools-api does
        logger.error(
            with_log_prefix(LOG_FAILURE, f"event_entity_list_failed {id}: {exc!r}")
        )
        return JSONResponse(
            status_code=500,
            content=error_body(ErrorCode.INTERNAL, "Internal server error"),
        )

    by_division: dict[str, dict[str, dict[str, Any]]] = {}
    for r in rows:
        # The per-event override wins, else the song's own division.
        division = (r.submission_division or r.song_division or "").strip()
        division = division or UNSPECIFIED_DIVISION
        key = song_entity_key(r.user_id, r.partner_id, r.managed_partnership_id)
        entities = by_division.setdefault(division, {})
        if key in entities:
            entities[key]["song_count"] += 1
            continue
        if r.leader_first_name is not None:
            leader = full_name(r.leader_first_name, r.leader_last_name)
            follower = full_name(r.follower_first_name, r.follower_last_name)
            label = f"{leader} & {follower}" if follower else leader
        else:
            label = partnership_display(
                full_name(r.owner_first, r.owner_last),
                full_name(r.partner_first, r.partner_last),
                r.partner_kind,
            )
        entities[key] = {"label": label or "Unnamed entry", "song_count": 1}

    divisions: list[dict[str, Any]] = [
        {
            "division": division,
            "entities": sorted(
                (
                    {
                        "entity_key": k,
                        "label": e["label"],
                        "song_count": e["song_count"],
                    }
                    for k, e in entities.items()
                ),
                key=lambda e: locale_key(e["label"]),
            ),
        }
        for division, entities in by_division.items()
    ]
    divisions.sort(
        key=lambda d: (_division_order(d["division"]), locale_key(d["division"]))
    )
    return success_list(divisions)


@router.post(
    "",
    status_code=201,
    response_model=EventResponse,
    summary="Create an event",
    description="Requires deejaytools.events.write. season_year defaults from start_date.",
    responses=ADMIN,
)
async def create_event(
    body: CreateEventBody,
    caller: Caller = Depends(require_scope("deejaytools.events.write")),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Create an event."""
    if body.start_date > body.end_date:
        raise api_error(
            400, ErrorCode.BAD_REQUEST, "start_date must be on or before end_date"
        )
    now = _now_ms()
    row = Event(
        id=str(uuid.uuid4()),
        name=body.name,
        start_date=body.start_date,
        end_date=body.end_date,
        timezone=body.timezone,
        season_year=body.season_year or season_year_from_date_string(body.start_date),
        created_by=caller.user_id,
        created_at=now,
        updated_at=now,
    )
    session.add(row)
    await session.commit()
    return success(map_event(row))


@router.patch(
    "/{id}",
    response_model=EventResponse,
    summary="Update an event",
    description=(
        "Requires deejaytools.events.write. season_year changes only when sent; "
        "moving start_date does not recompute it."
    ),
    responses={**ADMIN, **NOT_FOUND},
)
async def patch_event(
    id: EventId,
    body: PatchEventBody,
    _caller: Caller = Depends(require_scope("deejaytools.events.write")),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Update an event."""
    row = await _load(session, id)
    if (body.start_date or row.start_date) > (body.end_date or row.end_date):
        raise api_error(
            400, ErrorCode.BAD_REQUEST, "start_date must be on or before end_date"
        )
    for field in ("name", "start_date", "end_date", "timezone", "season_year"):
        value = getattr(body, field)
        if value is not None:
            setattr(row, field, value)
    row.updated_at = _now_ms()
    await session.commit()
    return success(map_event(row))


@router.delete(
    "/{id}",
    response_model=DeletedResponse,
    summary="Delete an event",
    description=(
        "Requires deejaytools.events.write. Deletes its sessions with their "
        "check-ins, queues and runs, its song submissions and run limits, "
        "then queues the submissions' Drive copies for trashing."
    ),
    responses={**ADMIN, **NOT_FOUND},
)
async def delete_event(
    id: EventId,
    _caller: Caller = Depends(require_scope("deejaytools.events.write")),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Delete an event and everything hanging off it."""
    await _load(session, id)
    session_ids = list(
        (
            await session.execute(select(Session.id).where(Session.event_id == id))
        ).scalars()
    )
    if session_ids:
        for model in (QueueEntry, QueueEvent, Run, Checkin, SessionDivision):
            await session.execute(
                delete(model).where(model.session_id.in_(session_ids))
            )
        await session.execute(delete(Session).where(Session.id.in_(session_ids)))
    # Captured before the delete: afterwards the file ids are gone.
    orphaned_copies = [
        file_id
        for file_id in (
            await session.execute(
                select(EventSongSubmission.drive_copy_file_id).where(
                    EventSongSubmission.event_id == id
                )
            )
        ).scalars()
        if file_id is not None
    ]
    await session.execute(
        delete(EventSongSubmission).where(EventSongSubmission.event_id == id)
    )
    await session.execute(
        delete(event_division_run_limits).where(
            event_division_run_limits.c.event_id == id
        )
    )
    await session.execute(delete(Event).where(Event.id == id))
    await session.commit()

    await enqueue_trash_jobs(
        session, orphaned_copies, source="event_delete", context={"event_id": id}
    )
    return success({"deleted": True})
