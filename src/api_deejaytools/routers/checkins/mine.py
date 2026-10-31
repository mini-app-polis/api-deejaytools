"""``GET /v1/checkins/mine``: the caller's live check-ins, and those of
pairs whose partner row is linked to the caller, with their place in line.

Behind ``deejaytools.checkins.read``.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import and_, desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from ...auth import Caller, require_scope
from ...database import get_db_session
from ...errors import Meta, success
from ...models import (
    Checkin,
    Event,
    ManagedPartnership,
    Pair,
    Partner,
    QueueEntry,
    Run,
    Session,
    Song,
    User,
)
from ...queue.labels import managed_label, pair_label
from .common import AUTH

router = APIRouter(prefix="/v1/checkins", tags=["checkins"])


class MyCheckinData(BaseModel):
    """One of the caller's live check-ins, with its place in line."""

    id: str = Field(..., description="Check-in id.")
    sessionId: str = Field(..., description="Session id.")
    eventName: str | None = Field(None, description="Event name, if any.")
    sessionName: str = Field(..., description="Session name.")
    sessionFloorTrialStartsAt: int = Field(..., description="Epoch ms.")
    sessionStatus: str = Field(..., description="The session's stored status.")
    eventTimezone: str | None = Field(None, description="Event timezone, if any.")
    divisionName: str = Field(..., description="Division.")
    entityPairId: str | None = Field(None, description="Pair entity.")
    entitySoloUserId: str | None = Field(None, description="Legacy solo entity.")
    entityManagedPartnershipId: str | None = Field(
        None, description="Managed partnership entity."
    )
    entityLabel: str = Field(..., description="Who is dancing; 'Solo' as a fallback.")
    songDisplayName: str | None = Field(None, description="Song display name.")
    songProcessedFilename: str | None = Field(None, description="Processed filename.")
    notes: str | None = Field(None, description="Check-in notes.")
    checkedInAt: int = Field(..., description="Epoch ms.")
    queueEntryId: str = Field(..., description="Live queue entry id.")
    queueType: str = Field(..., description="Queue it is in now.")
    queuePosition: int = Field(..., description="1-based position in that queue.")
    overallPosition: int = Field(
        ..., description="Position across active, then priority, then standard."
    )
    runCount: int = Field(..., description="Runs this entity has in the session.")
    canWithdraw: bool = Field(
        ...,
        description=(
            "True when the caller may withdraw it: they lead the pair, own the "
            "managed partnership or are the solo user. False for a pair led by "
            "someone else whose partner row is linked to the caller."
        ),
    )


class MyCheckinsResponse(BaseModel):
    """The caller's live check-ins, and their linked partners' pairs' check-ins,
    newest first. No meta.count."""

    data: list[MyCheckinData] = Field(..., description="The check-ins.")
    meta: Meta = Field(..., description="Response metadata.")


@router.get(
    "/mine",
    response_model=MyCheckinsResponse,
    summary="My live check-ins",
    description=(
        "Requires deejaytools.checkins.read. The caller's check-ins that still "
        "have a queue entry (solo, pairs they lead, managed partnerships they "
        "own), and those of pairs led by someone else whose partner row in the "
        "leader's address book is linked to the caller, newest first, with "
        "queue position, overall position, run count and canWithdraw (false "
        "for the linked partner's view: only the leader withdraws)."
    ),
    responses=AUTH,
)
async def my_checkins(
    caller: Caller = Depends(require_scope("deejaytools.checkins.read")),
    db: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """List the caller's live check-ins and their linked partners'."""
    user_id = caller.user_id
    pair_user = aliased(User, name="pair_user")

    own_pair_ids = list(
        (await db.execute(select(Pair.id).where(Pair.user_a_id == user_id))).scalars()
    )
    # Pairs led by someone else whose partner row, in the leader's own
    # address book, is linked to the caller (as song_records.shared_with).
    linked_pair_ids = list(
        (
            await db.execute(
                select(Pair.id)
                .join(Partner, Partner.id == Pair.partner_b_id)
                .where(
                    Partner.linked_user_id == user_id,
                    Partner.user_id == Pair.user_a_id,
                    Pair.user_a_id != user_id,
                )
            )
        ).scalars()
    )
    pair_ids = own_pair_ids + linked_pair_ids
    managed_ids = list(
        (
            await db.execute(
                select(ManagedPartnership.id).where(
                    ManagedPartnership.user_id == user_id
                )
            )
        ).scalars()
    )

    # Ownership is read from queue_entries' entity columns: checkins.entity_*
    # can be stale on legacy rows.
    parts = [QueueEntry.entity_solo_user_id == user_id]
    if pair_ids:
        parts.append(QueueEntry.entity_pair_id.in_(pair_ids))
    if managed_ids:
        parts.append(QueueEntry.entity_managed_partnership_id.in_(managed_ids))

    rows = (
        await db.execute(
            select(
                Checkin.id,
                Checkin.session_id,
                Event.name.label("event_name"),
                Session.name.label("session_name"),
                Session.floor_trial_starts_at,
                Session.status.label("session_status"),
                Event.timezone.label("event_timezone"),
                Checkin.division_name,
                QueueEntry.entity_pair_id,
                QueueEntry.entity_solo_user_id,
                QueueEntry.entity_managed_partnership_id,
                Checkin.notes,
                Checkin.created_at,
                Song.display_name.label("song_display_name"),
                Song.processed_filename.label("song_processed_filename"),
                QueueEntry.id.label("queue_entry_id"),
                QueueEntry.queue_type,
                QueueEntry.position,
                pair_user.first_name.label("pair_user_first"),
                pair_user.last_name.label("pair_user_last"),
                Partner.first_name.label("partner_first"),
                Partner.last_name.label("partner_last"),
                Partner.kind.label("partner_kind"),
                Partner.partner_role.label("partner_role"),
                ManagedPartnership.leader_first_name,
                ManagedPartnership.leader_last_name,
                ManagedPartnership.follower_first_name,
                ManagedPartnership.follower_last_name,
            )
            .select_from(Checkin)
            .join(QueueEntry, QueueEntry.checkin_id == Checkin.id)
            .join(Session, Session.id == Checkin.session_id)
            .outerjoin(Event, Event.id == Session.event_id)
            .outerjoin(Song, Song.id == Checkin.song_id)
            .outerjoin(Pair, Pair.id == QueueEntry.entity_pair_id)
            .outerjoin(pair_user, pair_user.id == Pair.user_a_id)
            .outerjoin(Partner, Partner.id == Pair.partner_b_id)
            .outerjoin(
                ManagedPartnership,
                ManagedPartnership.id == QueueEntry.entity_managed_partnership_id,
            )
            .where(or_(*parts) if len(parts) > 1 else parts[0])
            .order_by(desc(Checkin.created_at))
        )
    ).all()

    session_ids = list(dict.fromkeys(r.session_id for r in rows))
    counts: dict[str, dict[str, int]] = {}
    run_counts: dict[str, int] = {}
    if session_ids:
        for sid, queue_type, n in (
            await db.execute(
                select(QueueEntry.session_id, QueueEntry.queue_type, func.count())
                .where(QueueEntry.session_id.in_(session_ids))
                .group_by(QueueEntry.session_id, QueueEntry.queue_type)
            )
        ).all():
            counts.setdefault(sid, {"active": 0, "priority": 0, "non_priority": 0})[
                queue_type
            ] = int(n)

        run_parts = [Run.entity_solo_user_id == user_id]
        if pair_ids:
            run_parts.append(Run.entity_pair_id.in_(pair_ids))
        if managed_ids:
            run_parts.append(Run.entity_managed_partnership_id.in_(managed_ids))
        for rc in (
            await db.execute(
                select(
                    Run.session_id,
                    Run.entity_pair_id,
                    Run.entity_solo_user_id,
                    Run.entity_managed_partnership_id,
                    func.count().label("n"),
                )
                .where(and_(Run.session_id.in_(session_ids), or_(*run_parts)))
                .group_by(
                    Run.session_id,
                    Run.entity_pair_id,
                    Run.entity_solo_user_id,
                    Run.entity_managed_partnership_id,
                )
            )
        ).all():
            key = (
                rc.entity_pair_id
                or rc.entity_solo_user_id
                or rc.entity_managed_partnership_id
            )
            if key:
                run_counts[f"{rc.session_id}:{key}"] = int(rc.n)

    def overall_position(session_id: str, queue_type: str, position: int) -> int:
        c = counts.get(session_id, {"active": 0, "priority": 0, "non_priority": 0})
        if queue_type == "active":
            return position
        if queue_type == "priority":
            return c["active"] + position
        return c["active"] + c["priority"] + position

    withdrawable = {user_id, *own_pair_ids, *managed_ids}
    data = []
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
        else:
            label = "Solo"
        key = (
            r.entity_pair_id or r.entity_solo_user_id or r.entity_managed_partnership_id
        )
        data.append(
            {
                "id": r.id,
                "sessionId": r.session_id,
                "eventName": r.event_name,
                "sessionName": r.session_name,
                "sessionFloorTrialStartsAt": r.floor_trial_starts_at,
                "sessionStatus": r.session_status,
                "eventTimezone": r.event_timezone,
                "divisionName": r.division_name,
                "entityPairId": r.entity_pair_id,
                "entitySoloUserId": r.entity_solo_user_id,
                "entityManagedPartnershipId": r.entity_managed_partnership_id,
                "entityLabel": label,
                "songDisplayName": r.song_display_name,
                "songProcessedFilename": r.song_processed_filename,
                "notes": r.notes,
                "checkedInAt": r.created_at,
                "queueEntryId": r.queue_entry_id,
                "queueType": r.queue_type,
                "queuePosition": r.position,
                "overallPosition": overall_position(
                    r.session_id, r.queue_type, r.position
                ),
                "runCount": run_counts.get(f"{r.session_id}:{key}", 0) if key else 0,
                "canWithdraw": key in withdrawable,
            }
        )
    return success(data)
