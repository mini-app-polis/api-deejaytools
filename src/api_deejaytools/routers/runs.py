"""``/v1/runs`` (deejaytools-api docs/API.md, src/routes/runs.ts).

Run history for the floor manager, behind ``deejaytools.runs.read``: each
completed run with display-ready labels for its song, entity and the admin
who completed it.
"""

from __future__ import annotations

import math
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, BeforeValidator, Field
from sqlalchemy import and_, desc, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from ..auth import Caller, require_scope
from ..database import get_db_session
from ..domain import full_name
from ..errors import ErrorResponse, ListMeta, success_list
from ..labels import build_structured_song_label, partnership_label
from ..models import Event, ManagedPartnership, Pair, Partner, Run, Session, Song, User
from ..queue.labels import managed_label, pair_label
from ..validation import ZodModel
from ..zod_coerce import coerce_number
from ..zod_types import QueryStr, zod_query

router = APIRouter(prefix="/v1/runs", tags=["runs"])

DEFAULT_LIMIT = 200


def _limit(raw: object) -> int:
    # z.coerce.number().int().min(1).max(500)
    value = coerce_number(raw)
    if math.isinf(value) or value != int(value):
        raise ValueError("Invalid input: expected int, received number")
    if value < 1:
        raise ValueError("Too small: expected number to be >=1")
    if value > 500:
        raise ValueError("Too big: expected number to be <=500")
    return int(value)


class ListRunsQuery(ZodModel):
    """Query of ``GET /v1/runs``."""

    session_id: QueryStr | None = Field(None, description="Only this session's runs.")
    event_id: QueryStr | None = Field(None, description="Only this event's runs.")
    limit: Annotated[int, BeforeValidator(_limit)] | None = Field(
        None, description="At most this many, 1 to 500; default 200."
    )


class RunData(BaseModel):
    """One completed run, flattened for display."""

    id: str = Field(..., description="Run id.")
    completed_at: int = Field(..., description="Epoch ms.")
    division_name: str = Field(..., description="Division.")
    session_id: str = Field(..., description="Session id.")
    session_floor_trial_starts_at: int | None = Field(
        None, description="Session's floor-trial start, epoch ms."
    )
    event_id: str | None = Field(None, description="Event id.")
    event_name: str | None = Field(None, description="Event name.")
    song_id: str = Field(..., description="Song id.")
    song_label: str = Field(..., description="Structured song label.")
    entity_label: str = Field(..., description="Who ran.")
    entity_key: str = Field(
        ..., description="managed:{id}, pair:{id}, solo:{userId} or unknown."
    )
    completed_by_label: str = Field(..., description="Admin who completed it.")


class RunListResponse(BaseModel):
    """Runs, most recent first."""

    data: list[RunData] = Field(..., description="The runs.")
    meta: ListMeta = Field(..., description="Response metadata.")


def _entity_key(r: Any) -> str:
    if r.entity_managed_partnership_id:
        return f"managed:{r.entity_managed_partnership_id}"
    if r.entity_pair_id:
        return f"pair:{r.entity_pair_id}"
    if r.entity_solo_user_id:
        return f"solo:{r.entity_solo_user_id}"
    return "unknown"


@router.get(
    "",
    response_model=RunListResponse,
    summary="Run history",
    description=(
        "Requires deejaytools.runs.read. Completed runs, most recent first, "
        "optionally for one session or event, with song, entity and "
        "completed-by labels. limit defaults to 200, at most 500."
    ),
    responses={
        401: {"model": ErrorResponse, "description": "Missing or invalid token."},
        403: {"model": ErrorResponse, "description": "Lacks deejaytools.runs.read."},
    },
)
async def list_runs(
    _caller: Caller = Depends(require_scope("deejaytools.runs.read")),
    query: ListRunsQuery = Depends(zod_query(ListRunsQuery)),
    db: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """List runs."""
    pair_user = aliased(User, name="pair_user")
    solo_user = aliased(User, name="solo_user")
    completed_by = aliased(User, name="completed_by")
    song_owner = aliased(User, name="song_owner")
    song_partner = aliased(Partner, name="song_partner")
    song_managed = aliased(ManagedPartnership, name="song_managed_partnership")

    filters = []
    if query.session_id:
        filters.append(Run.session_id == query.session_id)
    if query.event_id:
        filters.append(Run.event_id == query.event_id)

    stmt = (
        select(
            Run.id,
            Run.completed_at,
            Run.division_name,
            Run.entity_pair_id,
            Run.entity_solo_user_id,
            Run.entity_managed_partnership_id,
            Run.session_id,
            Session.floor_trial_starts_at,
            Run.event_id,
            Event.name.label("event_name"),
            Run.song_id,
            Song.display_name.label("song_display_name"),
            Song.processed_filename.label("song_processed_filename"),
            Song.division.label("song_division"),
            Song.season_year.label("song_season_year"),
            Song.routine_name.label("song_routine_name"),
            song_owner.first_name.label("song_owner_first"),
            song_owner.last_name.label("song_owner_last"),
            song_partner.first_name.label("song_partner_first"),
            song_partner.last_name.label("song_partner_last"),
            song_partner.kind.label("song_partner_kind"),
            song_managed.leader_first_name.label("song_managed_leader_first"),
            song_managed.leader_last_name.label("song_managed_leader_last"),
            song_managed.follower_first_name.label("song_managed_follower_first"),
            song_managed.follower_last_name.label("song_managed_follower_last"),
            pair_user.first_name.label("pair_user_first"),
            pair_user.last_name.label("pair_user_last"),
            Partner.first_name.label("partner_first"),
            Partner.last_name.label("partner_last"),
            Partner.kind.label("partner_kind"),
            solo_user.first_name.label("solo_first"),
            solo_user.last_name.label("solo_last"),
            ManagedPartnership.leader_first_name,
            ManagedPartnership.leader_last_name,
            ManagedPartnership.follower_first_name,
            ManagedPartnership.follower_last_name,
            completed_by.first_name.label("completed_by_first"),
            completed_by.last_name.label("completed_by_last"),
        )
        .select_from(Run)
        .outerjoin(Session, Session.id == Run.session_id)
        .outerjoin(Event, Event.id == Run.event_id)
        .outerjoin(Song, Song.id == Run.song_id)
        .outerjoin(song_owner, song_owner.id == Song.user_id)
        .outerjoin(song_partner, song_partner.id == Song.partner_id)
        .outerjoin(song_managed, song_managed.id == Song.managed_partnership_id)
        .outerjoin(Pair, Pair.id == Run.entity_pair_id)
        .outerjoin(pair_user, pair_user.id == Pair.user_a_id)
        .outerjoin(Partner, Partner.id == Pair.partner_b_id)
        .outerjoin(solo_user, solo_user.id == Run.entity_solo_user_id)
        .outerjoin(
            ManagedPartnership,
            ManagedPartnership.id == Run.entity_managed_partnership_id,
        )
        .outerjoin(completed_by, completed_by.id == Run.completed_by_user_id)
        .order_by(desc(Run.completed_at))
        .limit(query.limit if query.limit is not None else DEFAULT_LIMIT)
    )
    if filters:
        stmt = stmt.where(and_(*filters))
    rows = (await db.execute(stmt)).all()

    data = []
    for r in rows:
        if r.leader_first_name is not None:
            entity_label = managed_label(
                r.leader_first_name,
                r.leader_last_name,
                r.follower_first_name,
                r.follower_last_name,
            )
        elif r.pair_user_first or r.pair_user_last:
            entity_label = (
                pair_label(
                    r.pair_user_first,
                    r.pair_user_last,
                    r.partner_first,
                    r.partner_last,
                    r.partner_kind,
                )
                or "Pair"
            )
        elif r.solo_first or r.solo_last:
            entity_label = full_name(r.solo_first, r.solo_last)
        else:
            entity_label = "—"

        song_partnership = partnership_label(
            managed_leader_first=r.song_managed_leader_first,
            managed_leader_last=r.song_managed_leader_last,
            managed_follower_first=r.song_managed_follower_first,
            managed_follower_last=r.song_managed_follower_last,
            owner_first=r.song_owner_first,
            owner_last=r.song_owner_last,
            partner_first=r.song_partner_first,
            partner_last=r.song_partner_last,
            partner_kind=r.song_partner_kind,
        )
        data.append(
            {
                "id": r.id,
                "completed_at": r.completed_at,
                "division_name": r.division_name,
                "session_id": r.session_id,
                "session_floor_trial_starts_at": r.floor_trial_starts_at,
                "event_id": r.event_id,
                "event_name": r.event_name,
                "song_id": r.song_id,
                "song_label": build_structured_song_label(
                    partnership=song_partnership,
                    division=r.song_division,
                    season_year=r.song_season_year,
                    routine_name=r.song_routine_name,
                    processed_filename=r.song_processed_filename,
                    display_name=r.song_display_name,
                    song_id=r.song_id,
                ),
                "entity_label": entity_label,
                "entity_key": _entity_key(r),
                "completed_by_label": full_name(
                    r.completed_by_first, r.completed_by_last
                )
                or "Admin",
            }
        )
    return success_list(data)
