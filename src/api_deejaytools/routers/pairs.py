"""``/v1/pairs`` (deejaytools-api docs/API.md, src/routes/pairs.ts).

One route, behind ``deejaytools.partners.write``: find or create the pair of
the caller and one of their partners, for the check-in flow.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import Caller, require_scope
from ..database import get_db_session
from ..errors import ErrorCode, ErrorResponse, Meta, api_error, success
from ..models import Pair, Partner
from ..validation import NonEmptyStr, ZodModel
from ..zod_types import zod_body

router = APIRouter(prefix="/v1/pairs", tags=["pairs"])


class FindOrCreateBody(ZodModel):
    """Body of ``POST /v1/pairs/find-or-create``."""

    partner_id: NonEmptyStr = Field(..., description="One of the caller's partners.")


class PairIdData(BaseModel):
    """A pair's id."""

    id: str = Field(..., description="Pair id.")


class PairIdResponse(BaseModel):
    """The pair found or created."""

    data: PairIdData = Field(..., description="The pair.")
    meta: Meta = Field(..., description="Response metadata.")


@router.post(
    "/find-or-create",
    response_model=PairIdResponse,
    summary="Find or create a pair",
    description=(
        "The pair of the caller (user A) and one of their partners: 200 when "
        "it exists, 201 when created. Requires deejaytools.partners.write."
    ),
    responses={
        201: {"model": PairIdResponse, "description": "Created."},
        401: {"model": ErrorResponse, "description": "Missing or invalid token."},
        403: {"model": ErrorResponse, "description": "Lacks the scope."},
        404: {"model": ErrorResponse, "description": "Not the caller's partner."},
    },
)
async def find_or_create_pair(
    caller: Caller = Depends(require_scope("deejaytools.partners.write")),
    body: FindOrCreateBody = Depends(zod_body(FindOrCreateBody)),
    session: AsyncSession = Depends(get_db_session),
) -> Any:
    """Return the caller's pair with a partner, creating it if needed."""
    partner = await session.execute(
        select(Partner.id)
        .where(Partner.id == body.partner_id, Partner.user_id == caller.user_id)
        .limit(1)
    )
    if partner.first() is None:
        raise api_error(404, ErrorCode.NOT_FOUND, "Partner not found")

    existing = (
        await session.execute(
            select(Pair.id)
            .where(
                Pair.user_a_id == caller.user_id,
                Pair.partner_b_id == body.partner_id,
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if existing is not None:
        return success({"id": existing})

    pair_id = str(uuid.uuid4())
    session.add(
        Pair(
            id=pair_id,
            user_a_id=caller.user_id,
            partner_b_id=body.partner_id,
            created_at=int(time.time() * 1000),
        )
    )
    await session.commit()
    return JSONResponse(status_code=201, content=success({"id": pair_id}))
