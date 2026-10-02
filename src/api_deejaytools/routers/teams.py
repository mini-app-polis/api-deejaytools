"""``/v1/teams`` (deejaytools-api docs/API.md, src/routes/teams.ts).

Team names a user competes under. Reads are behind ``deejaytools.teams.read``,
changes behind ``deejaytools.teams.write``; every route sees only the
caller's own teams. Names are unique per user (``uq_teams_user_identifier``).
"""

from __future__ import annotations

import time
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Response
from fastapi.responses import JSONResponse
from mini_app_polis.logger import LOG_FAILURE, get_logger, with_log_prefix
from pydantic import BaseModel, Field
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import Caller, require_scope
from ..database import get_db_session
from ..db_errors import is_unique_violation
from ..errors import (
    ApiError,
    ErrorCode,
    ErrorResponse,
    ListMeta,
    Meta,
    api_error,
    error_body,
    success,
    success_list,
)
from ..models import Team
from ..text import title_case_if_no_caps
from ..validation import ZodModel
from ..zod_types import trimmed, zod_body

logger = get_logger()

router = APIRouter(prefix="/v1/teams", tags=["teams"])


TeamIdentifier = Annotated[
    str,
    trimmed(
        min_length=1,
        max_length=100,
        pattern=r"^[A-Za-z0-9 ]+$",
        pattern_message="Team name may only contain letters, numbers, and spaces",
    ),
]


class TeamBody(ZodModel):
    """Body of ``POST /v1/teams`` and ``PATCH /v1/teams/{id}``."""

    identifier: TeamIdentifier = Field(
        ...,
        description=(
            "Team name: letters, numbers and spaces, 1-100 after trimming. "
            "Words with no capital get their first letter capitalized."
        ),
    )


class TeamData(BaseModel):
    """A team as the API returns it."""

    id: str = Field(..., description="Team id.")
    user_id: str = Field(..., description="users.id of the owner.")
    identifier: str = Field(..., description="Team name.")
    created_at: int = Field(..., description="Created, epoch ms.")
    updated_at: int = Field(..., description="Last updated, epoch ms.")


class TeamResponse(BaseModel):
    """One team."""

    data: TeamData = Field(..., description="The team.")
    meta: Meta = Field(..., description="Response metadata.")


class TeamListResponse(BaseModel):
    """The caller's teams, newest first."""

    data: list[TeamData] = Field(..., description="The teams.")
    meta: ListMeta = Field(..., description="Response metadata.")


def map_team(row: Team) -> dict[str, Any]:
    """A teams row on the wire."""
    return {
        "id": row.id,
        "user_id": row.user_id,
        "identifier": row.identifier,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _conflict() -> ApiError:
    return api_error(409, "conflict", "You already have a team with that name.")


async def _load_owned(session: AsyncSession, team_id: str, user_id: str) -> Team:
    row = (
        await session.execute(
            select(Team).where(Team.id == team_id, Team.user_id == user_id).limit(1)
        )
    ).scalar_one_or_none()
    if row is None:
        raise api_error(404, ErrorCode.NOT_FOUND, "Team not found")
    return row


TeamId = Annotated[str, Path(description="Team id.")]
AUTH: dict[int | str, dict[str, Any]] = {
    401: {"model": ErrorResponse, "description": "Missing or invalid token."},
    403: {"model": ErrorResponse, "description": "Lacks the scope."},
}
NOT_FOUND: dict[int | str, dict[str, Any]] = {
    404: {"model": ErrorResponse, "description": "No such team of the caller's."}
}
CONFLICT: dict[int | str, dict[str, Any]] = {
    409: {"model": ErrorResponse, "description": "The caller has that name already."}
}


@router.get(
    "",
    response_model=TeamListResponse,
    summary="List teams",
    description="The caller's teams, newest first. Requires deejaytools.teams.read.",
    responses=AUTH,
)
async def list_teams(
    caller: Caller = Depends(require_scope("deejaytools.teams.read")),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """List the caller's teams."""
    rows = (
        (
            await session.execute(
                select(Team)
                .where(Team.user_id == caller.user_id)
                .order_by(Team.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    return success_list([map_team(r) for r in rows])


@router.post(
    "",
    status_code=201,
    response_model=TeamResponse,
    summary="Create a team",
    description="Requires deejaytools.teams.write.",
    responses={**AUTH, **CONFLICT},
)
async def create_team(
    caller: Caller = Depends(require_scope("deejaytools.teams.write")),
    body: TeamBody = Depends(zod_body(TeamBody)),
    session: AsyncSession = Depends(get_db_session),
) -> Any:
    """Create a team."""
    now = int(time.time() * 1000)
    team_id = str(uuid.uuid4())
    identifier = title_case_if_no_caps(body.identifier)
    try:
        session.add(
            Team(
                id=team_id,
                user_id=caller.user_id,
                identifier=identifier,
                created_at=now,
                updated_at=now,
            )
        )
        await session.commit()
    except Exception as exc:  # noqa: BLE001 - answered as deejaytools-api does
        await session.rollback()
        if is_unique_violation(exc):
            raise _conflict() from exc
        logger.error(
            with_log_prefix(
                LOG_FAILURE,
                f"team_create_failed user={caller.user_id} "
                f"identifier={identifier!r}: {exc!r}",
            )
        )
        return JSONResponse(
            status_code=500,
            content=error_body(ErrorCode.INTERNAL, "Internal server error"),
        )
    row = await session.get(Team, team_id)
    assert row is not None
    return success(map_team(row))


@router.patch(
    "/{id}",
    response_model=TeamResponse,
    summary="Rename a team",
    description="Requires deejaytools.teams.write.",
    responses={**AUTH, **NOT_FOUND, **CONFLICT},
)
async def patch_team(
    id: TeamId,
    caller: Caller = Depends(require_scope("deejaytools.teams.write")),
    body: TeamBody = Depends(zod_body(TeamBody)),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Rename a team."""
    identifier = title_case_if_no_caps(body.identifier)
    await _load_owned(session, id, caller.user_id)
    try:
        await session.execute(
            update(Team)
            .where(Team.id == id, Team.user_id == caller.user_id)
            .values(identifier=identifier, updated_at=int(time.time() * 1000))
        )
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        if is_unique_violation(exc):
            raise _conflict() from exc
        raise
    row = await session.get(Team, id, populate_existing=True)
    assert row is not None
    return success(map_team(row))


@router.delete(
    "/{id}",
    response_model=None,  # 204: no body
    status_code=204,
    response_class=Response,
    summary="Delete a team",
    description="Requires deejaytools.teams.write.",
    responses={**AUTH, **NOT_FOUND},
)
async def delete_team(
    id: TeamId,
    caller: Caller = Depends(require_scope("deejaytools.teams.write")),
    session: AsyncSession = Depends(get_db_session),
) -> Response:
    """Delete a team."""
    await _load_owned(session, id, caller.user_id)
    await session.execute(
        delete(Team).where(Team.id == id, Team.user_id == caller.user_id)
    )
    await session.commit()
    return Response(status_code=204)
