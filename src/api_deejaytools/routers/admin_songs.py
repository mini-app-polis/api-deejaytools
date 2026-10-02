"""``/v1/admin/songs`` (deejaytools-api docs/API.md, src/routes/admin-songs.ts).

The admin directory of every user's songs, behind
``deejaytools.library.read`` (ADR-007).
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import ColumnElement, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from ..auth import Caller, require_scope
from ..database import get_db_session
from ..domain import full_name, partnership_display
from ..errors import ErrorResponse, ListMeta, success_list
from ..labels import build_structured_song_label
from ..models import Partner, Song, User
from ..song_records import is_legacy_song
from ..validation import ZodModel
from ..zod_coerce import js_trim
from ..zod_types import QueryStr, zod_query

router = APIRouter(prefix="/v1/admin/songs", tags=["admin-songs"])


class ListQuery(ZodModel):
    """Query of ``GET /v1/admin/songs``."""

    q: QueryStr | None = Field(
        None,
        description=(
            "Case-insensitive search across song fields and owner and partner names."
        ),
    )
    include_deleted: Literal["true", "false"] | None = Field(
        None, description="'true' includes soft-deleted songs."
    )


class AdminSongOwner(BaseModel):
    """The uploading user."""

    id: str = Field(..., description="users.id, or '' if the user is gone.")
    email: str = Field(..., description="Email, or '' if the user is gone.")
    full_name: str | None = Field(None, description="First and last name.")


class AdminSongPartner(BaseModel):
    """The song's partner record."""

    id: str = Field(..., description="Partner id.")
    full_name: str | None = Field(None, description="First and last name.")
    linked_user_email: str | None = Field(
        None, description="Email of the partner's own account, if linked."
    )


class AdminSongData(BaseModel):
    """A song as the admin directory returns it."""

    id: str = Field(..., description="Song id.")
    song_label: str = Field(..., description="Structured song label.")
    display_name: str | None = Field(None, description="Display name.")
    division: str | None = Field(None, description="Division.")
    routine_name: str | None = Field(None, description="Routine name.")
    personal_descriptor: str | None = Field(None, description="Personal descriptor.")
    season_year: str | None = Field(None, description="Season year.")
    is_legacy: bool = Field(..., description="From the removed claim-legacy flow.")
    created_at: int = Field(..., description="Created, epoch ms.")
    deleted_at: int | None = Field(None, description="Soft-deleted, epoch ms.")
    owner: AdminSongOwner = Field(..., description="The uploading user.")
    partner: AdminSongPartner | None = Field(None, description="The partner, if any.")


class AdminSongListResponse(BaseModel):
    """Every song, newest first."""

    data: list[AdminSongData] = Field(..., description="The songs.")
    meta: ListMeta = Field(..., description="Response metadata.")


@router.get(
    "",
    response_model=AdminSongListResponse,
    summary="List every song",
    description=(
        "Every user's songs, newest first, with owner and partner. Soft-deleted "
        "songs only with include_deleted=true. Requires deejaytools.library.read."
    ),
    responses={
        401: {"model": ErrorResponse, "description": "Missing or invalid token."},
        403: {"model": ErrorResponse, "description": "Lacks the scope."},
        400: {"model": ErrorResponse, "description": "Invalid query."},
    },
)
async def list_songs(
    _caller: Caller = Depends(require_scope("deejaytools.library.read")),
    query: ListQuery = Depends(zod_query(ListQuery)),
    session: AsyncSession = Depends(get_db_session),
) -> Any:
    """List every song."""
    owner = aliased(User, name="song_owner")
    linked = aliased(User, name="partner_linked_user")

    conditions: list[ColumnElement[bool]] = []
    if query.include_deleted != "true":
        conditions.append(Song.deleted_at.is_(None))
    if query.q and js_trim(query.q):
        term = f"%{js_trim(query.q)}%"
        conditions.append(
            or_(
                Song.display_name.ilike(term),
                Song.processed_filename.ilike(term),
                Song.division.ilike(term),
                Song.routine_name.ilike(term),
                Song.personal_descriptor.ilike(term),
                Song.season_year.ilike(term),
                owner.first_name.ilike(term),
                owner.last_name.ilike(term),
                owner.email.ilike(term),
                Partner.first_name.ilike(term),
                Partner.last_name.ilike(term),
            )
        )

    rows = (
        await session.execute(
            select(
                Song.id,
                Song.display_name,
                Song.processed_filename,
                Song.division,
                Song.routine_name,
                Song.personal_descriptor,
                Song.season_year,
                Song.created_at,
                Song.deleted_at,
                owner.id.label("owner_id"),
                owner.email.label("owner_email"),
                owner.first_name.label("owner_first"),
                owner.last_name.label("owner_last"),
                Partner.id.label("partner_id"),
                Partner.first_name.label("partner_first"),
                Partner.last_name.label("partner_last"),
                Partner.kind.label("partner_kind"),
                linked.email.label("partner_linked_user_email"),
            )
            .select_from(Song)
            .outerjoin(owner, owner.id == Song.user_id)
            .outerjoin(Partner, Partner.id == Song.partner_id)
            .outerjoin(linked, linked.id == Partner.linked_user_id)
            .where(*conditions)
            .order_by(Song.created_at.desc())
        )
    ).all()

    data = []
    for r in rows:
        owner_name = full_name(r.owner_first, r.owner_last)
        partner_name = full_name(r.partner_first, r.partner_last)
        partnership = partnership_display(owner_name, partner_name, r.partner_kind)
        data.append(
            {
                "id": r.id,
                "song_label": build_structured_song_label(
                    partnership=partnership,
                    division=r.division,
                    season_year=r.season_year,
                    routine_name=r.routine_name,
                    processed_filename=r.processed_filename,
                    display_name=r.display_name,
                    song_id=r.id,
                ),
                "display_name": r.display_name,
                "division": r.division,
                "routine_name": r.routine_name,
                "personal_descriptor": r.personal_descriptor,
                "season_year": r.season_year,
                "is_legacy": is_legacy_song(r.processed_filename),
                "created_at": r.created_at,
                "deleted_at": r.deleted_at,
                "owner": {
                    "id": r.owner_id or "",
                    "email": r.owner_email or "",
                    "full_name": owner_name or None,
                },
                "partner": (
                    {
                        "id": r.partner_id,
                        "full_name": partner_name or None,
                        "linked_user_email": r.partner_linked_user_email,
                    }
                    if r.partner_id
                    else None
                ),
            }
        )
    return success_list(data)
