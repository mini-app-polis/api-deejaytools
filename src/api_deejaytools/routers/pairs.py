"""``/v1/pairs`` (docs/API.md).

One route, behind ``deejaytools.partners.write``: find or create the pair of
the caller and one of their partners, for the check-in flow.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import Caller, require_scope
from ..clock import now_ms
from ..database import get_db_session
from ..errors import Envelope, ErrorCode, ErrorResponse, api_error, success
from ..models import Pair, Partner
from ..validation import NonEmptyStr, ZodModel
from ..zod_types import zod_body

router = APIRouter(prefix="/v1/pairs", tags=["pairs"])


class FindOrCreateBody(ZodModel):
    """Body of ``POST /v1/pairs/find-or-create``."""

    partner_id: NonEmptyStr = Field(
        ...,
        description=(
            "One of the caller's partners, or a partner row in another user's "
            "address book linked to the caller's account."
        ),
    )


class PairIdData(BaseModel):
    """A pair's id."""

    id: str = Field(..., description="Pair id.")


class PairIdResponse(Envelope[PairIdData]):
    """The pair found or created."""

    data: PairIdData = Field(..., description="The pair.")


@router.post(
    "/find-or-create",
    response_model=PairIdResponse,
    summary="Find or create a pair",
    description=(
        "The pair of the caller (user A) and one of their partners: 200 when "
        "it exists, 201 when created. When partner_id names a row in another "
        "user's address book linked to the caller's account, the pair is that "
        "owner's with the row (the pair a song shared with the caller checks "
        "in as). Requires deejaytools.partners.write."
    ),
    responses={
        201: {"model": PairIdResponse, "description": "Created."},
        401: {"model": ErrorResponse, "description": "Missing or invalid token."},
        403: {"model": ErrorResponse, "description": "Lacks the scope."},
        404: {
            "model": ErrorResponse,
            "description": "Neither the caller's partner nor linked to them.",
        },
    },
)
async def find_or_create_pair(
    caller: Caller = Depends(require_scope("deejaytools.partners.write")),
    body: FindOrCreateBody = Depends(zod_body(FindOrCreateBody)),
    session: AsyncSession = Depends(get_db_session),
) -> Any:
    """Return the caller's pair with a partner, or, for a partner row linked
    to the caller in its owner's book, the owner's pair with it; created if
    needed."""
    partner = (
        await session.execute(
            select(Partner.user_id)
            .where(
                Partner.id == body.partner_id,
                or_(
                    Partner.user_id == caller.user_id,
                    # Linked by account, as song_records.shared_with: the
                    # pair is the row's owner's. Never by email.
                    Partner.linked_user_id == caller.user_id,
                ),
            )
            .limit(1)
        )
    ).first()
    if partner is None:
        raise api_error(404, ErrorCode.NOT_FOUND, "Partner not found")
    leader_id = partner.user_id

    existing = await _pair_id(session, leader_id, body.partner_id)
    if existing is not None:
        return success({"id": existing})

    # A concurrent find-or-create of the same pair can insert it first: this
    # insert then does nothing on uq_pairs_user_partner, and the answer is
    # the pair that won.
    created = (
        await session.execute(
            insert(Pair)
            .values(
                id=str(uuid.uuid4()),
                user_a_id=leader_id,
                partner_b_id=body.partner_id,
                created_at=now_ms(),
            )
            .on_conflict_do_nothing(index_elements=[Pair.user_a_id, Pair.partner_b_id])
            .returning(Pair.id)
        )
    ).scalar_one_or_none()
    await session.commit()
    if created is None:
        return success({"id": await _pair_id(session, leader_id, body.partner_id)})
    return JSONResponse(status_code=201, content=success({"id": created}))


async def _pair_id(
    session: AsyncSession, leader_id: str, partner_id: str
) -> str | None:
    """The id of the pair of ``leader_id`` and ``partner_id``, if there is one."""
    return (
        await session.execute(
            select(Pair.id)
            .where(Pair.user_a_id == leader_id, Pair.partner_b_id == partner_id)
            .limit(1)
        )
    ).scalar_one_or_none()
