"""``/v1/sessions`` (deejaytools-api docs/API.md, src/routes/sessions.ts).

The two reads are public with an optional caller: a valid, synced caller
also gets ``has_active_checkin`` (and on the detail, the division of their
live entry); anonymous responses omit those keys entirely. Changes are
behind ``deejaytools.sessions.write``.

The shared part of each read is cached for 5 s; the caller's own fields are
always computed fresh.
"""

from __future__ import annotations

import time
import uuid
from typing import Annotated, Any, ClassVar, Literal

from fastapi import APIRouter, Depends, Header, Path, Request
from fastapi.responses import JSONResponse
from mini_app_polis.logger import LOG_FAILURE, get_logger, with_log_prefix
from pydantic import BaseModel, Field
from sqlalchemy import Select, delete, func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import Caller, optional_synced_user_id, require_scope
from ..cache import SESSION_TTL_SECONDS, invalidate_session_cache, response_cache
from ..database import get_db_session
from ..domain import ms_to_date_in_zone
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
    ManagedPartnership,
    Pair,
    QueueEntry,
    QueueEvent,
    Run,
    Session,
    SessionDivision,
)
from ..validation import JsInt, JsNumber, NonEmptyStr, NonNegativeJsInt, ZodModel
from ..zod_coerce import js_trim
from ..zod_types import QueryStr, parse_zod_query, zod_body

logger = get_logger()

router = APIRouter(prefix="/v1/sessions", tags=["sessions"])

SessionStatus = Literal[
    "scheduled", "checkin_open", "in_progress", "completed", "cancelled"
]


class DivisionItem(ZodModel):
    """One division of a session, as sent."""

    division_name: str = Field(
        ..., description="Division name. Blank and 'Other' are skipped."
    )
    is_priority: bool | None = Field(
        None, description="Priority division; default false."
    )
    sort_order: JsInt | None = Field(
        None, description="Display order; default its index."
    )
    priority_run_limit: NonNegativeJsInt | None = Field(
        None, description="Runs before an entry loses priority; default 0."
    )


class CreateSessionBody(ZodModel):
    """Body of ``POST /v1/sessions``. Times are epoch milliseconds."""

    event_id: str | None = Field(None, description="Event the session belongs to.")
    name: NonEmptyStr = Field(..., description="Session name.")
    date: str | None = Field(None, description="Display date.")
    checkin_opens_at: JsNumber = Field(..., description="Check-in opens, epoch ms.")
    floor_trial_starts_at: JsNumber = Field(
        ..., description="Floor trial starts, epoch ms."
    )
    floor_trial_ends_at: JsNumber = Field(
        ..., description="Floor trial ends, epoch ms."
    )
    active_priority_max: NonNegativeJsInt | None = Field(
        None, description="Active-queue slots for priority entries; default 6."
    )
    active_non_priority_max: NonNegativeJsInt | None = Field(
        None, description="Active-queue slots for standard entries; default 4."
    )
    divisions: list[DivisionItem] = Field(
        ..., description="Divisions the session runs."
    )


class PatchSessionBody(ZodModel):
    """Body of ``PATCH /v1/sessions/{id}``. ``date`` and ``event_id`` may be null."""

    _NULLABLE: ClassVar[frozenset[str]] = frozenset({"date", "event_id"})

    name: NonEmptyStr | None = Field(None, description="Session name.")
    date: str | None = Field(None, description="Display date; null clears it.")
    event_id: str | None = Field(None, description="Event; null detaches it.")
    checkin_opens_at: JsNumber | None = Field(None, description="Epoch ms.")
    floor_trial_starts_at: JsNumber | None = Field(None, description="Epoch ms.")
    floor_trial_ends_at: JsNumber | None = Field(None, description="Epoch ms.")
    active_priority_max: NonNegativeJsInt | None = Field(None, description="Slots.")
    active_non_priority_max: NonNegativeJsInt | None = Field(None, description="Slots.")


class PutDivisionsBody(ZodModel):
    """Body of ``PUT /v1/sessions/{id}/divisions``."""

    divisions: list[DivisionItem] = Field(..., description="Divisions to upsert.")


class StatusBody(ZodModel):
    """Body of ``PATCH /v1/sessions/{id}/status``."""

    status: SessionStatus = Field(..., description="Stored status.")


class DivisionData(BaseModel):
    """A session division on the wire."""

    id: str = Field(..., description="Division row id.")
    division_name: str = Field(..., description="Division name.")
    is_priority: bool = Field(..., description="Priority division.")
    sort_order: int = Field(..., description="Display order.")
    priority_run_limit: int = Field(..., description="Priority run limit.")


class QueueDepth(BaseModel):
    """How many entries each queue holds."""

    priority: int = Field(..., description="Waiting, priority.")
    non_priority: int = Field(..., description="Waiting, standard.")
    active: int = Field(..., description="On the floor.")


class SessionData(BaseModel):
    """A session on the wire. ``status`` is derived from the clock."""

    model_config = {"extra": "allow"}

    id: str = Field(..., description="Session id.")
    event_id: str | None = Field(None, description="Event id.")
    name: str = Field(..., description="Session name.")
    date: str | None = Field(None, description="Display date.")
    checkin_opens_at: int = Field(..., description="Epoch ms.")
    floor_trial_starts_at: int = Field(..., description="Epoch ms.")
    floor_trial_ends_at: int = Field(..., description="Epoch ms.")
    active_priority_max: int = Field(..., description="Priority slots.")
    active_non_priority_max: int = Field(..., description="Standard slots.")
    status: str = Field(..., description="Status now; 'cancelled' is kept as stored.")
    created_by: str | None = Field(None, description="users.id of the creator.")
    created_at: int = Field(..., description="Epoch ms.")
    divisions: list[DivisionData] = Field(..., description="Divisions, in order.")
    queue_depth: QueueDepth = Field(..., description="Queue sizes.")


class SessionResponse(BaseModel):
    """One session. Optional-caller and event fields appear only when present."""

    data: SessionData = Field(..., description="The session.")
    meta: Meta = Field(..., description="Response metadata.")


class SessionListResponse(BaseModel):
    """Sessions, newest date first."""

    data: list[SessionData] = Field(..., description="The sessions.")
    meta: ListMeta = Field(..., description="Response metadata.")


class DeletedResponse(BaseModel):
    """Delete confirmation envelope."""

    data: dict[str, bool] = Field(..., description="{'deleted': true}.")
    meta: Meta = Field(..., description="Response metadata.")


def derive_status(row: Session, now: int) -> str:
    """The status for the wall clock now. 'cancelled' is an admin override and
    always kept; otherwise the stored value is ignored for display."""
    if row.status == "cancelled":
        return "cancelled"
    if now < row.checkin_opens_at:
        return "scheduled"
    if now < row.floor_trial_starts_at:
        return "checkin_open"
    if now < row.floor_trial_ends_at:
        return "in_progress"
    return "completed"


def map_session_base(row: Session, now: int | None = None) -> dict[str, Any]:
    """A sessions row on the wire, without divisions or queue depth."""
    return {
        "id": row.id,
        "event_id": row.event_id,
        "name": row.name,
        "date": row.date,
        "checkin_opens_at": row.checkin_opens_at,
        "floor_trial_starts_at": row.floor_trial_starts_at,
        "floor_trial_ends_at": row.floor_trial_ends_at,
        "active_priority_max": row.active_priority_max,
        "active_non_priority_max": row.active_non_priority_max,
        "status": derive_status(row, now if now is not None else _now_ms()),
        "created_by": row.created_by,
        "created_at": row.created_at,
    }


def map_division(d: SessionDivision) -> dict[str, Any]:
    """A session_divisions row on the wire."""
    return {
        "id": d.id,
        "division_name": d.division_name,
        "is_priority": d.is_priority,
        "sort_order": d.sort_order,
        "priority_run_limit": d.priority_run_limit,
    }


def _now_ms() -> int:
    return int(time.time() * 1000)


def _empty_depth() -> dict[str, int]:
    return {"priority": 0, "non_priority": 0, "active": 0}


async def _queue_depths(
    db: AsyncSession, session_ids: list[str]
) -> dict[str, dict[str, int]]:
    depths = {sid: _empty_depth() for sid in session_ids}
    if not session_ids:
        return depths
    rows = await db.execute(
        select(QueueEntry.session_id, QueueEntry.queue_type, func.count())
        .where(QueueEntry.session_id.in_(session_ids))
        .group_by(QueueEntry.session_id, QueueEntry.queue_type)
    )
    for sid, queue_type, n in rows:
        if sid in depths and queue_type in depths[sid]:
            depths[sid][queue_type] = int(n)
    return depths


async def _divisions(
    db: AsyncSession, session_ids: list[str]
) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {sid: [] for sid in session_ids}
    if not session_ids:
        return out
    rows = await db.execute(
        select(SessionDivision)
        .where(SessionDivision.session_id.in_(session_ids))
        .order_by(SessionDivision.session_id, SessionDivision.sort_order)
    )
    for d in rows.scalars():
        out[d.session_id].append(map_division(d))
    return out


async def _full(db: AsyncSession, session_id: str) -> dict[str, Any]:
    """The session with divisions and queue depth, as every write answers."""
    row = await db.get(Session, session_id, populate_existing=True)
    assert row is not None  # noqa: S101 - callers checked it exists
    return {
        **map_session_base(row),
        "divisions": (await _divisions(db, [session_id]))[session_id],
        "queue_depth": (await _queue_depths(db, [session_id]))[session_id],
    }


async def _load(db: AsyncSession, session_id: str) -> Session:
    row = await db.get(Session, session_id)
    if row is None:
        raise api_error(404, ErrorCode.NOT_FOUND, "Session not found")
    return row


def _live_entry_filter(
    user_id: str, pair_ids: list[str], managed_ids: list[str]
) -> Any:
    """Queue entries that belong to the caller: solo, a pair of theirs, or a
    managed partnership of theirs. Read from queue_entries' own entity
    columns, the authoritative record of who is queued."""
    parts = [QueueEntry.entity_solo_user_id == user_id]
    if pair_ids:
        parts.append(QueueEntry.entity_pair_id.in_(pair_ids))
    if managed_ids:
        parts.append(QueueEntry.entity_managed_partnership_id.in_(managed_ids))
    return or_(*parts)


async def _caller_entities(
    db: AsyncSession, user_id: str
) -> tuple[list[str], list[str]]:
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
    return pair_ids, managed_ids


async def _overlaps(
    db: AsyncSession,
    event_id: str,
    start: float,
    end: float,
    exclude: str | None = None,
) -> bool:
    """Whether [start, end) overlaps another session's floor trial in the event."""
    query: Select[Any] = select(Session.id).where(
        Session.event_id == event_id,
        Session.floor_trial_starts_at < end,
        Session.floor_trial_ends_at > start,
    )
    if exclude is not None:
        query = query.where(Session.id != exclude)
    return (await db.execute(query.limit(1))).first() is not None


async def _outside_event(
    db: AsyncSession, event_id: str, checkin_opens_at: float, floor_trial_ends_at: float
) -> str | None:
    """Why the session's dates fall outside the event's, in its timezone, or None."""
    event = await db.get(Event, event_id)
    if event is None:
        return "Event not found"
    tz = event.timezone
    start = ms_to_date_in_zone(checkin_opens_at, tz)
    end = ms_to_date_in_zone(floor_trial_ends_at, tz)
    if start < event.start_date:
        return f"Session starts ({start}) before event start date ({event.start_date}) in timezone {tz}"
    if end > event.end_date:
        return f"Session ends ({end}) after event end date ({event.end_date}) in timezone {tz}"
    return None


def _bad_request(message: str) -> Exception:
    return api_error(400, ErrorCode.BAD_REQUEST, message)


def _division_rows(session_id: str, items: list[DivisionItem]) -> list[dict[str, Any]]:
    rows = []
    for i, d in enumerate(items):
        name = js_trim(d.division_name)
        if not name or name == "Other":
            continue
        rows.append(
            {
                "id": str(uuid.uuid4()),
                "session_id": session_id,
                "division_name": name,
                "is_priority": d.is_priority if d.is_priority is not None else False,
                "sort_order": d.sort_order if d.sort_order is not None else i,
                "priority_run_limit": d.priority_run_limit or 0,
            }
        )
    return rows


SessionId = Annotated[str, Path(description="Session id.")]
Authorization = Annotated[str | None, Header(alias="Authorization")]
NOT_FOUND: dict[int | str, dict[str, Any]] = {
    404: {"model": ErrorResponse, "description": "No such session."}
}
ADMIN: dict[int | str, dict[str, Any]] = {
    401: {"model": ErrorResponse, "description": "Missing or invalid token."},
    403: {"model": ErrorResponse, "description": "Lacks deejaytools.sessions.write."},
}


class SessionListQuery(ZodModel):
    """Query of ``GET /v1/sessions``. A repeated ``event_id`` is refused, as
    zod refuses the array it arrives as."""

    event_id: QueryStr | None = Field(None, description="Only this event's sessions.")


@router.get(
    "",
    response_model=SessionListResponse,
    response_model_exclude_unset=True,
    summary="List sessions",
    description=(
        "Sessions, newest date first, with divisions, queue depth and the "
        "event's timezone. Intentionally public; a valid synced caller also "
        "gets has_active_checkin on each."
    ),
)
async def list_sessions(
    request: Request,
    authorization: Authorization = None,
    db: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """List sessions. Public, with an optional caller."""
    event_id = parse_zod_query(request, SessionListQuery).event_id
    user_id = await optional_synced_user_id(authorization, db)

    cache_key = f"sessions:list:{event_id or 'all'}"
    base = response_cache.get(cache_key)
    if base is None:
        # Newest date first (Postgres puts null dates first, as drizzle's
        # desc() did), then by floor-trial start within a date.
        query = select(Session).order_by(
            Session.date.desc(), Session.floor_trial_starts_at.desc()
        )
        if event_id:
            query = query.where(Session.event_id == event_id)
        rows = list((await db.execute(query)).scalars())
        ids = [r.id for r in rows]
        event_ids = list({r.event_id for r in rows if r.event_id})
        zones: dict[str, str] = {}
        if event_ids:
            zones = dict(
                (
                    await db.execute(
                        select(Event.id, Event.timezone).where(Event.id.in_(event_ids))
                    )
                ).all()  # type: ignore[arg-type]
            )
        base = {
            "rows": rows,
            "divisions": await _divisions(db, ids),
            "depths": await _queue_depths(db, ids),
            "zones": zones,
        }
        response_cache.set(cache_key, base, SESSION_TTL_SECONDS)

    rows = base["rows"]
    active: set[str] = set()
    if user_id and rows:
        pair_ids, managed_ids = await _caller_entities(db, user_id)
        active = set(
            (
                await db.execute(
                    select(QueueEntry.session_id).where(
                        QueueEntry.session_id.in_([r.id for r in rows]),
                        _live_entry_filter(user_id, pair_ids, managed_ids),
                    )
                )
            ).scalars()
        )

    now = _now_ms()
    results = []
    for row in rows:
        item = {
            **map_session_base(row, now),
            "event_timezone": base["zones"].get(row.event_id) if row.event_id else None,
            "divisions": base["divisions"].get(row.id, []),
            "queue_depth": base["depths"].get(row.id, _empty_depth()),
        }
        if user_id:
            item["has_active_checkin"] = row.id in active
        results.append(item)
    return success_list(results)


@router.post(
    "",
    status_code=201,
    response_model=SessionResponse,
    summary="Create a session",
    description=(
        "Requires deejaytools.sessions.write. The floor-trial window may not "
        "overlap another session of the same event, and must fall within the "
        "event's dates in its timezone."
    ),
    responses=ADMIN,
)
async def create_session(
    caller: Caller = Depends(require_scope("deejaytools.sessions.write")),
    body: CreateSessionBody = Depends(zod_body(CreateSessionBody)),
    db: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Create a session with its divisions."""
    priority_max = (
        body.active_priority_max if body.active_priority_max is not None else 6
    )
    standard_max = (
        body.active_non_priority_max if body.active_non_priority_max is not None else 4
    )
    if (
        not js_trim(body.name)
        or body.checkin_opens_at <= 0
        or body.floor_trial_starts_at <= 0
        or body.floor_trial_ends_at <= 0
    ):
        raise _bad_request(
            "Missing or invalid required fields: name, checkin_opens_at, "
            "floor_trial_starts_at, floor_trial_ends_at"
        )
    if body.floor_trial_starts_at <= body.checkin_opens_at:
        raise _bad_request("floor_trial_starts_at must be after checkin_opens_at")
    if body.floor_trial_ends_at <= body.floor_trial_starts_at:
        raise _bad_request("floor_trial_ends_at must be after floor_trial_starts_at")
    if standard_max > priority_max:
        raise _bad_request("active_non_priority_max must be <= active_priority_max")

    if body.event_id:
        if await _overlaps(
            db, body.event_id, body.floor_trial_starts_at, body.floor_trial_ends_at
        ):
            raise _bad_request(
                "Session floor-trial window overlaps another session in this event"
            )
        problem = await _outside_event(
            db, body.event_id, body.checkin_opens_at, body.floor_trial_ends_at
        )
        if problem:
            raise _bad_request(problem)

    session_id = str(uuid.uuid4())
    db.add(
        Session(
            id=session_id,
            event_id=body.event_id,
            name=js_trim(body.name),
            date=body.date,
            checkin_opens_at=body.checkin_opens_at,
            floor_trial_starts_at=body.floor_trial_starts_at,
            floor_trial_ends_at=body.floor_trial_ends_at,
            active_priority_max=priority_max,
            active_non_priority_max=standard_max,
            status="scheduled",
            created_by=caller.user_id,
            created_at=_now_ms(),
        )
    )
    await db.flush()
    division_rows = _division_rows(session_id, body.divisions)
    if division_rows:
        await db.execute(insert(SessionDivision), division_rows)
    await db.commit()
    return success(await _full(db, session_id))


@router.put(
    "/{id}/divisions",
    response_model=SessionResponse,
    summary="Set a session's divisions",
    description=(
        "Requires deejaytools.sessions.write. Upserts each division by name, "
        "so divisions that check-ins reference keep their rows."
    ),
    responses={**ADMIN, **NOT_FOUND},
)
async def put_divisions(
    id: SessionId,
    _caller: Caller = Depends(require_scope("deejaytools.sessions.write")),
    body: PutDivisionsBody = Depends(zod_body(PutDivisionsBody)),
    db: AsyncSession = Depends(get_db_session),
) -> Any:
    """Upsert a session's divisions."""
    await _load(db, id)
    # Upsert by (session_id, division_name) rather than delete-and-reinsert:
    # check-ins reference division rows (fk_checkins_session_division, ON
    # DELETE RESTRICT), so a rebuild fails once a session has any.
    try:
        for row in _division_rows(id, body.divisions):
            stmt = insert(SessionDivision).values(**row)
            await db.execute(
                stmt.on_conflict_do_update(
                    index_elements=[
                        SessionDivision.session_id,
                        SessionDivision.division_name,
                    ],
                    set_={
                        "is_priority": stmt.excluded.is_priority,
                        "priority_run_limit": stmt.excluded.priority_run_limit,
                        "sort_order": stmt.excluded.sort_order,
                    },
                )
            )
        await db.commit()
    except Exception as exc:  # noqa: BLE001 - answered as deejaytools-api does
        await db.rollback()
        logger.error(
            with_log_prefix(
                LOG_FAILURE,
                f"session_divisions_put_failed session={id} "
                f"divisions={[d.division_name for d in body.divisions]}: {exc!r}",
            )
        )
        return JSONResponse(
            status_code=500,
            content=error_body(
                "DIVISIONS_UPDATE_FAILED",
                str(getattr(exc, "orig", None) or exc) or "Failed to update divisions",
            ),
        )
    invalidate_session_cache(id)
    return success(await _full(db, id))


@router.patch(
    "/{id}/status",
    response_model=SessionResponse,
    summary="Set a session's stored status",
    description=(
        "Requires deejaytools.sessions.write. Reads derive status from the "
        "clock, so only 'cancelled' is visible on the wire."
    ),
    responses={**ADMIN, **NOT_FOUND},
)
async def patch_status(
    id: SessionId,
    _caller: Caller = Depends(require_scope("deejaytools.sessions.write")),
    body: StatusBody = Depends(zod_body(StatusBody)),
    db: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Set the stored status."""
    row = await _load(db, id)
    row.status = body.status
    await db.commit()
    invalidate_session_cache(id)
    return success(await _full(db, id))


@router.patch(
    "/{id}",
    response_model=SessionResponse,
    summary="Update a session",
    description=(
        "Requires deejaytools.sessions.write. The result is checked as a whole: "
        "window order, caps, overlap and the event's dates."
    ),
    responses={**ADMIN, **NOT_FOUND},
)
async def patch_session(
    id: SessionId,
    _caller: Caller = Depends(require_scope("deejaytools.sessions.write")),
    body: PatchSessionBody = Depends(zod_body(PatchSessionBody)),
    db: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Update a session."""
    row = await _load(db, id)
    sent = body.model_fields_set

    def next_value(field: str) -> Any:
        return getattr(body, field) if field in sent else getattr(row, field)

    checkin_opens_at = next_value("checkin_opens_at")
    starts = next_value("floor_trial_starts_at")
    ends = next_value("floor_trial_ends_at")
    event_id = next_value("event_id")

    if starts <= checkin_opens_at:
        raise _bad_request("floor_trial_starts_at must be after checkin_opens_at")
    if ends <= starts:
        raise _bad_request("floor_trial_ends_at must be after floor_trial_starts_at")
    if {"active_priority_max", "active_non_priority_max"} & sent:
        if next_value("active_non_priority_max") > next_value("active_priority_max"):
            raise _bad_request("active_non_priority_max must be <= active_priority_max")
    if event_id:
        if await _overlaps(db, event_id, starts, ends, exclude=id):
            raise _bad_request(
                "Session floor-trial window overlaps another session in this event"
            )
        problem = await _outside_event(db, event_id, checkin_opens_at, ends)
        if problem:
            raise _bad_request(problem)

    for field in sent:
        value = getattr(body, field)
        setattr(row, field, js_trim(value) if field == "name" else value)
    await db.commit()
    invalidate_session_cache(id)
    return success(await _full(db, id))


@router.delete(
    "/{id}",
    response_model=DeletedResponse,
    summary="Delete a session",
    description=(
        "Requires deejaytools.sessions.write. Deletes its queue entries, queue "
        "events, runs, check-ins and divisions with it."
    ),
    responses={**ADMIN, **NOT_FOUND},
)
async def delete_session(
    id: SessionId,
    _caller: Caller = Depends(require_scope("deejaytools.sessions.write")),
    db: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Delete a session and everything hanging off it."""
    await _load(db, id)
    for model in (QueueEntry, QueueEvent, Run, Checkin, SessionDivision):
        await db.execute(delete(model).where(model.session_id == id))
    await db.execute(delete(Session).where(Session.id == id))
    await db.commit()
    invalidate_session_cache(id)
    return success({"deleted": True})


@router.get(
    "/{id}",
    response_model=SessionResponse,
    response_model_exclude_unset=True,
    summary="Get a session",
    description=(
        "One session with its event's name and timezone, divisions and queue "
        "depth. Intentionally public; a valid synced caller also gets "
        "has_active_checkin, and active_checkin_division when queued."
    ),
    responses=NOT_FOUND,
)
async def get_session(
    id: SessionId,
    authorization: Authorization = None,
    db: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Get one session. Public, with an optional caller."""
    cache_key = f"sessions:base:{id}"
    base = response_cache.get(cache_key)
    if base is None:
        row = await _load(db, id)
        event_name = event_timezone = None
        if row.event_id:
            event = await db.get(Event, row.event_id)
            if event is not None:
                event_name, event_timezone = event.name, event.timezone
        base = {
            "row": row,
            "event_name": event_name,
            "event_timezone": event_timezone,
            "divisions": (await _divisions(db, [id]))[id],
            "depth": (await _queue_depths(db, [id]))[id],
        }
        response_cache.set(cache_key, base, SESSION_TTL_SECONDS)

    data: dict[str, Any] = {
        **map_session_base(base["row"]),
        "event_name": base["event_name"],
        "event_timezone": base["event_timezone"],
        "divisions": base["divisions"],
        "queue_depth": base["depth"],
    }
    user_id = await optional_synced_user_id(authorization, db)
    if user_id:
        pair_ids, managed_ids = await _caller_entities(db, user_id)
        hit = (
            await db.execute(
                select(Checkin.division_name)
                .select_from(QueueEntry)
                .join(Checkin, Checkin.id == QueueEntry.checkin_id)
                .where(
                    QueueEntry.session_id == id,
                    _live_entry_filter(user_id, pair_ids, managed_ids),
                )
                .limit(1)
            )
        ).first()
        data["has_active_checkin"] = hit is not None
        if hit is not None:
            data["active_checkin_division"] = hit[0]
    return success(data)
