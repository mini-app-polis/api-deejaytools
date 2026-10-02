"""``/v1/partners`` (deejaytools-api docs/API.md, src/routes/partners.ts).

A user's address book of dance partners. Reads are behind
``deejaytools.partners.read``, changes behind ``deejaytools.partners.write``;
every route sees only the caller's own partners.
"""

from __future__ import annotations

import time
import uuid
from typing import Annotated, Any, ClassVar, Literal

from fastapi import APIRouter, Depends, Path, Response
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import Caller, require_scope
from ..database import get_db_session
from ..errors import (
    ErrorCode,
    ErrorResponse,
    ListMeta,
    Meta,
    api_error,
    success,
    success_list,
)
from ..models import Checkin, Pair, Partner, QueueEntry, Song
from ..validation import Email, NonEmptyStr, ZodModel
from ..zod_coerce import js_trim
from ..zod_types import zod_body

router = APIRouter(prefix="/v1/partners", tags=["partners"])


PartnerRole = Literal["leader", "follower"]


class CreatePartnerBody(ZodModel):
    """Body of ``POST /v1/partners``. Names are trimmed when stored."""

    first_name: NonEmptyStr = Field(..., description="First name.")
    last_name: NonEmptyStr = Field(..., description="Last name.")
    partner_role: PartnerRole = Field(..., description="The partner's role.")
    email: Email | None = Field(None, description="Partner's email, optional.")


class PatchPartnerBody(ZodModel):
    """Body of ``PATCH /v1/partners/{id}``. ``email`` may be null to clear it."""

    _NULLABLE: ClassVar[frozenset[str]] = frozenset({"email"})

    first_name: NonEmptyStr | None = Field(None, description="First name.")
    last_name: NonEmptyStr | None = Field(None, description="Last name.")
    partner_role: PartnerRole | None = Field(None, description="The partner's role.")
    email: Email | None = Field(None, description="Email; null clears it.")


class PartnerData(BaseModel):
    """A partner as the API returns it."""

    id: str = Field(..., description="Partner id.")
    user_id: str = Field(..., description="users.id of the owner.")
    first_name: str = Field(..., description="First name.")
    last_name: str = Field(..., description="Last name.")
    partner_role: str = Field(..., description="leader or follower.")
    email: str | None = Field(None, description="Email, if known.")
    linked_user_id: str | None = Field(None, description="Linked account, if any.")
    created_at: int = Field(..., description="Created, epoch ms.")
    updated_at: int = Field(..., description="Last updated, epoch ms.")
    display_name: str = Field(..., description="First and last name.")


class PartnerResponse(BaseModel):
    """One partner."""

    data: PartnerData = Field(..., description="The partner.")
    meta: Meta = Field(..., description="Response metadata.")


class PartnerListResponse(BaseModel):
    """The caller's partners, by last then first name."""

    data: list[PartnerData] = Field(..., description="The partners.")
    meta: ListMeta = Field(..., description="Response metadata.")


class LeadingPairData(BaseModel):
    """A pair the caller leads, for the check-in entity picker."""

    id: str = Field(..., description="Pair id.")
    partner_b_id: str | None = Field(None, description="Partner id; null if open.")
    display_name: str = Field(..., description="Partner's name, or 'Open slot'.")


class LeadingPairsResponse(BaseModel):
    """Pairs where the caller is user A."""

    data: list[LeadingPairData] = Field(..., description="The pairs.")
    meta: ListMeta = Field(..., description="Response metadata.")


class AssociationsData(BaseModel):
    """What a partner is used by, shown before a delete."""

    song_count: int = Field(..., description="The caller's songs with this partner.")
    has_active_checkin: bool = Field(
        ..., description="A pair with this partner has a live queue entry."
    )
    has_checkin_history: bool = Field(
        ..., description="A pair with this partner was ever checked in."
    )


class AssociationsResponse(BaseModel):
    """A partner's associations."""

    data: AssociationsData = Field(..., description="The associations.")
    meta: Meta = Field(..., description="Response metadata.")


def partner_display_name(first_name: str, last_name: str) -> str:
    """ "First Last", trimmed."""
    return js_trim(f"{first_name} {last_name}")


def map_partner(row: Partner) -> dict[str, Any]:
    """A partners row on the wire."""
    return {
        "id": row.id,
        "user_id": row.user_id,
        "first_name": row.first_name,
        "last_name": row.last_name,
        "partner_role": row.partner_role,
        "email": row.email,
        "linked_user_id": row.linked_user_id,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
        "display_name": partner_display_name(row.first_name, row.last_name),
    }


def _now_ms() -> int:
    return int(time.time() * 1000)


async def _load_owned(session: AsyncSession, partner_id: str, user_id: str) -> Partner:
    row = (
        await session.execute(
            select(Partner)
            .where(Partner.id == partner_id, Partner.user_id == user_id)
            .limit(1)
        )
    ).scalar_one_or_none()
    if row is None:
        raise api_error(404, ErrorCode.NOT_FOUND, "Partner not found")
    return row


async def _has_active_checkin(session: AsyncSession, partner_id: str) -> bool:
    """A check-in of a pair with this partner that still has a queue entry."""
    hit = await session.execute(
        select(Checkin.id)
        .join(Pair, Pair.id == Checkin.entity_pair_id)
        .join(QueueEntry, QueueEntry.checkin_id == Checkin.id)
        .where(Pair.partner_b_id == partner_id)
        .limit(1)
    )
    return hit.first() is not None


PartnerId = Annotated[str, Path(description="Partner id.")]
AUTH: dict[int | str, dict[str, Any]] = {
    401: {"model": ErrorResponse, "description": "Missing or invalid token."},
    403: {"model": ErrorResponse, "description": "Lacks the scope."},
}
NOT_FOUND: dict[int | str, dict[str, Any]] = {
    404: {"model": ErrorResponse, "description": "No such partner of the caller's."}
}


@router.get(
    "/leading-pairs",
    response_model=LeadingPairsResponse,
    summary="Pairs the caller leads",
    description=(
        "Pairs where the caller is user A and the partner is a real partner "
        "(not a placeholder), or the slot is open. Requires "
        "deejaytools.partners.read."
    ),
    responses=AUTH,
)
async def leading_pairs(
    caller: Caller = Depends(require_scope("deejaytools.partners.read")),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """List the caller's leading pairs."""
    rows = (
        await session.execute(
            select(Pair.id, Pair.partner_b_id, Partner.first_name, Partner.last_name)
            .select_from(Pair)
            .outerjoin(Partner, Partner.id == Pair.partner_b_id)
            .where(
                Pair.user_a_id == caller.user_id,
                or_(Pair.partner_b_id.is_(None), Partner.kind == "partner"),
            )
        )
    ).all()
    return success_list(
        [
            {
                "id": r.id,
                "partner_b_id": r.partner_b_id,
                "display_name": (
                    partner_display_name(r.first_name or "", r.last_name or "")
                    if r.partner_b_id
                    else "Open slot"
                ),
            }
            for r in rows
        ]
    )


@router.get(
    "",
    response_model=PartnerListResponse,
    summary="List partners",
    description=(
        "The caller's partners (kind 'partner' only; placeholders are left "
        "out), by last then first name. Requires deejaytools.partners.read."
    ),
    responses=AUTH,
)
async def list_partners(
    caller: Caller = Depends(require_scope("deejaytools.partners.read")),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """List the caller's partners."""
    rows = (
        (
            await session.execute(
                select(Partner)
                .where(Partner.user_id == caller.user_id, Partner.kind == "partner")
                .order_by(Partner.last_name.asc(), Partner.first_name.asc())
            )
        )
        .scalars()
        .all()
    )
    return success_list([map_partner(r) for r in rows])


@router.post(
    "",
    status_code=201,
    response_model=PartnerResponse,
    summary="Create a partner",
    description=(
        "Names are trimmed; a blank email is stored as null. Requires "
        "deejaytools.partners.write."
    ),
    responses=AUTH,
)
async def create_partner(
    caller: Caller = Depends(require_scope("deejaytools.partners.write")),
    body: CreatePartnerBody = Depends(zod_body(CreatePartnerBody)),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Create a partner."""
    now = _now_ms()
    partner_id = str(uuid.uuid4())
    session.add(
        Partner(
            id=partner_id,
            user_id=caller.user_id,
            first_name=js_trim(body.first_name),
            last_name=js_trim(body.last_name),
            partner_role=body.partner_role,
            email=js_trim(body.email or "") or None,
            created_at=now,
            updated_at=now,
        )
    )
    await session.commit()
    row = await session.get(Partner, partner_id, populate_existing=True)
    assert row is not None
    return success(map_partner(row))


@router.get(
    "/{id}/associations",
    response_model=AssociationsResponse,
    summary="A partner's associations",
    description=(
        "Song count and check-in usage, read before a delete. Requires "
        "deejaytools.partners.read."
    ),
    responses={**AUTH, **NOT_FOUND},
)
async def partner_associations(
    id: PartnerId,
    caller: Caller = Depends(require_scope("deejaytools.partners.read")),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Count what uses a partner."""
    await _load_owned(session, id, caller.user_id)
    # Soft-deleted songs count too, as in deejaytools-api.
    song_count = (
        await session.execute(
            select(func.count())
            .select_from(Song)
            .where(Song.partner_id == id, Song.user_id == caller.user_id)
        )
    ).scalar_one()
    has_active = await _has_active_checkin(session, id)
    # Completed and withdrawn check-ins stay in the checkins table.
    history = await session.execute(
        select(Checkin.id)
        .join(Pair, Pair.id == Checkin.entity_pair_id)
        .where(Pair.partner_b_id == id)
        .limit(1)
    )
    return success(
        {
            "song_count": int(song_count or 0),
            "has_active_checkin": has_active,
            "has_checkin_history": history.first() is not None,
        }
    )


@router.get(
    "/{id}",
    response_model=PartnerResponse,
    summary="Get a partner",
    description="One of the caller's partners. Requires deejaytools.partners.read.",
    responses={**AUTH, **NOT_FOUND},
)
async def get_partner(
    id: PartnerId,
    caller: Caller = Depends(require_scope("deejaytools.partners.read")),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Get one partner."""
    return success(map_partner(await _load_owned(session, id, caller.user_id)))


@router.patch(
    "/{id}",
    response_model=PartnerResponse,
    summary="Update a partner",
    description=(
        "Only the fields sent change; names are trimmed and may not be blank. "
        "With nothing to change the partner is returned untouched. Requires "
        "deejaytools.partners.write."
    ),
    responses={
        **AUTH,
        **NOT_FOUND,
        400: {"model": ErrorResponse, "description": "A name trims to empty."},
    },
)
async def patch_partner(
    id: PartnerId,
    caller: Caller = Depends(require_scope("deejaytools.partners.write")),
    body: PatchPartnerBody = Depends(zod_body(PatchPartnerBody)),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Update a partner."""
    row = await _load_owned(session, id, caller.user_id)
    sent = body.model_fields_set
    if body.first_name is not None and not js_trim(body.first_name):
        raise api_error(400, ErrorCode.BAD_REQUEST, "first_name cannot be empty")
    if body.last_name is not None and not js_trim(body.last_name):
        raise api_error(400, ErrorCode.BAD_REQUEST, "last_name cannot be empty")
    if not sent & {"first_name", "last_name", "partner_role", "email"}:
        return success(map_partner(row))

    if body.first_name is not None:
        row.first_name = js_trim(body.first_name)
    if body.last_name is not None:
        row.last_name = js_trim(body.last_name)
    if body.partner_role is not None:
        row.partner_role = body.partner_role
    if "email" in sent:
        row.email = js_trim(body.email or "") or None
    row.updated_at = _now_ms()
    await session.commit()
    return success(map_partner(row))


@router.delete(
    "/{id}",
    response_model=None,  # 204: no body
    status_code=204,
    response_class=Response,
    summary="Delete a partner",
    description=(
        "Refused while a pair with this partner has a live queue entry. "
        "Detaches the caller's songs from the partner; pairs with check-in "
        "history keep their row with the partner cleared, other pairs are "
        "deleted. Requires deejaytools.partners.write."
    ),
    responses={
        **AUTH,
        **NOT_FOUND,
        409: {"model": ErrorResponse, "description": "Active check-in."},
    },
)
async def delete_partner(
    id: PartnerId,
    caller: Caller = Depends(require_scope("deejaytools.partners.write")),
    session: AsyncSession = Depends(get_db_session),
) -> Response:
    """Delete a partner."""
    await _load_owned(session, id, caller.user_id)
    if await _has_active_checkin(session, id):
        raise api_error(
            409,
            "PARTNER_IN_ACTIVE_CHECKIN",
            "This partner is linked to a pair with an active check-in. "
            "Complete or withdraw the check-in first.",
        )

    pair_ids = list(
        (
            await session.execute(select(Pair.id).where(Pair.partner_b_id == id))
        ).scalars()
    )
    historic: set[str] = set()
    if pair_ids:
        historic = {
            pid
            for pid in (
                await session.execute(
                    select(Checkin.entity_pair_id)
                    .where(Checkin.entity_pair_id.in_(pair_ids))
                    .group_by(Checkin.entity_pair_id)
                )
            ).scalars()
            if pid
        }

    # One transaction, committed below.
    await session.execute(
        update(Song)
        .where(Song.partner_id == id, Song.user_id == caller.user_id)
        .values(partner_id=None)
    )
    # Check-ins reference pairs ON DELETE RESTRICT: a pair with history keeps
    # its row with the partner cleared; the rest go.
    to_orphan = [pid for pid in pair_ids if pid in historic]
    if to_orphan:
        await session.execute(
            update(Pair).where(Pair.id.in_(to_orphan)).values(partner_b_id=None)
        )
    to_delete = [pid for pid in pair_ids if pid not in historic]
    if to_delete:
        await session.execute(delete(Pair).where(Pair.id.in_(to_delete)))
    await session.execute(
        delete(Partner).where(Partner.id == id, Partner.user_id == caller.user_id)
    )
    await session.commit()
    return Response(status_code=204)
