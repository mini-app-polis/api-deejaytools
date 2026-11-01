"""The queue reads under ``/v1/queue/{session_id}``.

active and waiting are public (API-008), without check-in notes, which are
for the event's staff only. active/staff, priority and non-priority are
behind ``deejaytools.queue.read`` on the path session's event, with notes.
All are cached for 3 s per session and invalidated by every mutation of
that session.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Path
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from ...auth import Caller, EventRef, require_event_scope
from ...cache import QUEUE_TTL_SECONDS, response_cache
from ...database import get_db_session
from ...domain import full_name
from ...errors import Envelope, ErrorResponse, success_list
from ...models import Checkin, ManagedPartnership, Pair, Partner, QueueEntry, Song, User
from ...queue.labels import managed_label, pair_label

router = APIRouter(prefix="/v1/queue", tags=["queue"])

# The event a read is decided on (ADR-0012): the path session's. The scope
# through a role (an admin's) allows anywhere.
SESSION_EVENT = EventRef(source="path", name="session_id", kind="session")


class QueueEntryData(BaseModel):
    """One queue entry with its check-in and a server-rendered entity label."""

    queueEntryId: str = Field(..., description="queue_entries.id.")
    checkinId: str = Field(..., description="Check-in id.")
    position: int = Field(..., description="1-based; lower is closer to the front.")
    enteredQueueAt: int = Field(
        ..., description="When it entered this queue, epoch ms."
    )
    entityPairId: str | None = Field(None, description="Pair entity.")
    entitySoloUserId: str | None = Field(None, description="Legacy solo entity.")
    entityManagedPartnershipId: str | None = Field(
        None, description="Managed partnership entity."
    )
    entityLabel: str = Field(..., description="'Leader & Follower', a name, or '—'.")
    divisionName: str = Field(..., description="Division.")
    songId: str | None = Field(None, description="Song id.")
    songDisplayName: str | None = Field(None, description="Song display name.")
    songProcessedFilename: str | None = Field(None, description="Processed filename.")
    notes: str | None = Field(None, description="Check-in notes.")
    initialQueue: str = Field(..., description="Queue the check-in was admitted to.")
    checkedInAt: int = Field(..., description="Check-in time, epoch ms.")


class WaitingEntryData(QueueEntryData):
    """A waiting entry, tagged with the queue it waits in."""

    subQueue: Literal["priority", "non_priority"] = Field(
        ..., description="Which waiting queue."
    )


class QueueListResponse(Envelope[list[QueueEntryData]]):
    """A queue, front first."""

    data: list[QueueEntryData] = Field(..., description="The entries.")


class WaitingListResponse(Envelope[list[WaitingEntryData]]):
    """Priority then standard waiting entries."""

    data: list[WaitingEntryData] = Field(..., description="The entries.")


async def list_queue(
    db: AsyncSession, session_id: str, queue_type: str
) -> list[dict[str, Any]]:
    """A queue's entries, front first, with check-in details and entity labels."""
    pair_user = aliased(User, name="pair_user")
    solo_user = aliased(User, name="solo_user")
    rows = (
        await db.execute(
            select(
                QueueEntry.id,
                QueueEntry.checkin_id,
                QueueEntry.position,
                QueueEntry.entered_queue_at,
                QueueEntry.entity_pair_id,
                QueueEntry.entity_solo_user_id,
                QueueEntry.entity_managed_partnership_id,
                Checkin.division_name,
                Checkin.song_id,
                Checkin.notes,
                Checkin.initial_queue,
                Checkin.created_at,
                pair_user.first_name.label("pair_user_first"),
                pair_user.last_name.label("pair_user_last"),
                Partner.first_name.label("partner_first"),
                Partner.last_name.label("partner_last"),
                Partner.kind.label("partner_kind"),
                Partner.partner_role.label("partner_role"),
                solo_user.first_name.label("solo_first"),
                solo_user.last_name.label("solo_last"),
                ManagedPartnership.leader_first_name,
                ManagedPartnership.leader_last_name,
                ManagedPartnership.follower_first_name,
                ManagedPartnership.follower_last_name,
                Song.display_name.label("song_display_name"),
                Song.processed_filename.label("song_processed_filename"),
            )
            .select_from(QueueEntry)
            .join(Checkin, QueueEntry.checkin_id == Checkin.id)
            .outerjoin(Pair, Pair.id == QueueEntry.entity_pair_id)
            .outerjoin(pair_user, pair_user.id == Pair.user_a_id)
            .outerjoin(Partner, Partner.id == Pair.partner_b_id)
            .outerjoin(solo_user, solo_user.id == QueueEntry.entity_solo_user_id)
            .outerjoin(
                ManagedPartnership,
                ManagedPartnership.id == QueueEntry.entity_managed_partnership_id,
            )
            .outerjoin(Song, Song.id == Checkin.song_id)
            .where(
                QueueEntry.session_id == session_id, QueueEntry.queue_type == queue_type
            )
            .order_by(QueueEntry.position.asc())
        )
    ).all()

    out = []
    for r in rows:
        if r.entity_managed_partnership_id and r.leader_first_name is not None:
            label = managed_label(
                r.leader_first_name,
                r.leader_last_name,
                r.follower_first_name,
                r.follower_last_name,
                r.division_name,
            )
        elif r.entity_pair_id and (r.pair_user_first or r.pair_user_last):
            label = pair_label(
                r.pair_user_first,
                r.pair_user_last,
                r.partner_first,
                r.partner_last,
                r.partner_kind,
                r.partner_role,
                r.division_name,
            )
        elif r.entity_solo_user_id and (r.solo_first or r.solo_last):
            label = full_name(r.solo_first, r.solo_last)
        else:
            label = "—"
        out.append(
            {
                "queueEntryId": r.id,
                "checkinId": r.checkin_id,
                "position": r.position,
                "enteredQueueAt": r.entered_queue_at,
                "entityPairId": r.entity_pair_id,
                "entitySoloUserId": r.entity_solo_user_id,
                "entityManagedPartnershipId": r.entity_managed_partnership_id,
                "entityLabel": label,
                "divisionName": r.division_name,
                "songId": r.song_id,
                "songDisplayName": r.song_display_name,
                "songProcessedFilename": r.song_processed_filename,
                "notes": r.notes,
                "initialQueue": r.initial_queue,
                "checkedInAt": r.created_at,
            }
        )
    return out


async def _cached(key: str, build: Any) -> dict[str, Any]:
    cached = response_cache.get(key)
    if cached is not None:
        return cached  # type: ignore[no-any-return]
    result = success_list(await build())
    response_cache.set(key, result, QUEUE_TTL_SECONDS)
    return result


def _public(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Queue rows for anyone: check-in notes are for the event's staff."""
    return [{**r, "notes": None} for r in rows]


SessionId = Annotated[str, Path(description="Session id.")]
READ: dict[int | str, dict[str, Any]] = {
    401: {"model": ErrorResponse, "description": "Missing or invalid token."},
    403: {"model": ErrorResponse, "description": "Lacks the scope on this event."},
}


@router.get(
    "/{session_id}/active",
    response_model=QueueListResponse,
    summary="Active queue",
    description=(
        "The active queue, position 1 first (position 1 is on the floor). "
        "Intentionally public: the dancer-facing session page polls it. "
        "Check-in notes are left out (null): staff read them through "
        "/active/staff. Cached for 3 s."
    ),
)
async def active_queue(
    session_id: SessionId, db: AsyncSession = Depends(get_db_session)
) -> dict[str, Any]:
    """List the active queue. Public, so without notes."""

    async def build() -> list[dict[str, Any]]:
        return _public(await list_queue(db, session_id, "active"))

    return await _cached(f"queue:{session_id}:active", build)


@router.get(
    "/{session_id}/active/staff",
    response_model=QueueListResponse,
    summary="Active queue, for the event's staff",
    description=(
        "Requires deejaytools.queue.read on the session's event. The active "
        "queue as the public route gives it, with each entry's check-in notes. "
        "Cached for 3 s."
    ),
    responses=READ,
)
async def active_queue_for_staff(
    session_id: SessionId,
    _caller: Caller = Depends(
        require_event_scope("deejaytools.queue.read", SESSION_EVENT)
    ),
    db: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """List the active queue, with notes."""
    return await _cached(
        f"queue:{session_id}:active-staff",
        lambda: list_queue(db, session_id, "active"),
    )


@router.get(
    "/{session_id}/waiting",
    response_model=WaitingListResponse,
    summary="Waiting queues",
    description=(
        "Priority entries then standard entries, each tagged with subQueue. "
        "Intentionally public: the dancer-facing session page polls it. "
        "Check-in notes are left out (null). Cached for 3 s."
    ),
)
async def waiting_queue(
    session_id: SessionId, db: AsyncSession = Depends(get_db_session)
) -> dict[str, Any]:
    """List both waiting queues. Public."""

    async def build() -> list[dict[str, Any]]:
        priority = await list_queue(db, session_id, "priority")
        standard = await list_queue(db, session_id, "non_priority")
        return [{**r, "subQueue": "priority"} for r in _public(priority)] + [
            {**r, "subQueue": "non_priority"} for r in _public(standard)
        ]

    return await _cached(f"queue:{session_id}:waiting", build)


@router.get(
    "/{session_id}/priority",
    response_model=QueueListResponse,
    summary="Priority queue",
    description=(
        "Requires deejaytools.queue.read on the session's event. The priority "
        "queue. Cached for 3 s."
    ),
    responses=READ,
)
async def priority_queue(
    session_id: SessionId,
    _caller: Caller = Depends(
        require_event_scope("deejaytools.queue.read", SESSION_EVENT)
    ),
    db: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """List the priority queue."""
    return await _cached(
        f"queue:{session_id}:priority", lambda: list_queue(db, session_id, "priority")
    )


@router.get(
    "/{session_id}/non-priority",
    response_model=QueueListResponse,
    summary="Standard queue",
    description=(
        "Requires deejaytools.queue.read on the session's event. The standard "
        "queue. Cached for 3 s."
    ),
    responses=READ,
)
async def non_priority_queue(
    session_id: SessionId,
    _caller: Caller = Depends(
        require_event_scope("deejaytools.queue.read", SESSION_EVENT)
    ),
    db: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """List the standard queue."""
    return await _cached(
        f"queue:{session_id}:non-priority",
        lambda: list_queue(db, session_id, "non_priority"),
    )
