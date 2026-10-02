"""``/v1/admin/users`` (deejaytools-api docs/API.md, src/routes/admin-users.ts).

The admin user directory, role changes, and two per-user reads for the
"Upload For" flow. Reads are behind ``deejaytools.users.read``, the role
change behind ``deejaytools.users.write`` (ADR-007).

``role`` on the wire is derived from the identity store: ``"admin"`` when
the user's principal holds ``deejaytools-admin``, else ``"user"``. The
``users.role`` column is only a mirror kept for deejaytools-api; it is
written by the role change and never read here.
"""

from __future__ import annotations

import time
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Path
from identity.store import Principal as PrincipalRow
from identity.store import PrincipalRole
from mini_app_polis.logger import LOG_WARNING, get_logger, with_log_prefix
from pydantic import BaseModel, Field
from sqlalchemy import (
    ColumnElement,
    delete,
    exists,
    func,
    literal_column,
    not_,
    or_,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import ROLE_ADMIN, Caller, ensure_principal, require_scope
from ..config import get_settings
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
from ..models import Partner, Song, User
from ..submissions import fetch_user_submission_rows, map_submission_row
from ..validation import ZodModel
from ..zod_coerce import js_trim
from ..zod_types import QueryStr, zod_body, zod_query
from .event_song_submissions import SubmissionListResponse
from .partners import PartnerListResponse, map_partner

logger = get_logger()

router = APIRouter(prefix="/v1/admin/users", tags=["admin-users"])

READ_SCOPE = "deejaytools.users.read"
WRITE_SCOPE = "deejaytools.users.write"

Role = Literal["user", "admin"]


class ListQuery(ZodModel):
    """Query of ``GET /v1/admin/users``."""

    q: QueryStr | None = Field(
        None, description="Case-insensitive search across email, first and last name."
    )
    role: Role | None = Field(None, description="Only users with this role.")


class UpdateRoleBody(ZodModel):
    """Body of ``PATCH /v1/admin/users/{id}/role``."""

    role: Role = Field(..., description="The role to give the user.")


class SubmissionsQuery(ZodModel):
    """Query of ``GET /v1/admin/users/{id}/event-song-submissions``."""

    event_id: Annotated[QueryStr, Field(min_length=1)] = Field(
        ..., description="The event to list the user's submissions for."
    )


class AdminUserData(BaseModel):
    """A user as the admin directory returns it."""

    id: str = Field(..., description="Clerk user id.")
    email: str = Field(..., description="Email address.")
    first_name: str | None = Field(None, description="First name.")
    last_name: str | None = Field(None, description="Last name.")
    role: Role = Field(
        ..., description="'admin' when the user holds deejaytools-admin, else 'user'."
    )
    created_at: int = Field(..., description="Created, epoch ms.")
    song_count: int = Field(..., description="Songs not soft-deleted.")
    partner_count: int = Field(..., description="Partners of kind 'partner'.")


class AdminUserResponse(BaseModel):
    """One user."""

    data: AdminUserData = Field(..., description="The user.")
    meta: Meta = Field(..., description="Response metadata.")


class AdminUserListResponse(BaseModel):
    """Every user, oldest first."""

    data: list[AdminUserData] = Field(..., description="The users.")
    meta: ListMeta = Field(..., description="Response metadata.")


UserId = Annotated[str, Path(description="Target users.id.")]
AUTH: dict[int | str, dict[str, Any]] = {
    401: {"model": ErrorResponse, "description": "Missing or invalid token."},
    403: {"model": ErrorResponse, "description": "Lacks the scope."},
}


def _is_admin() -> ColumnElement[bool]:
    """Correlated: the ``users`` row's principal holds ``deejaytools-admin``."""
    return exists(
        select(literal_column("1"))
        .select_from(PrincipalRow)
        .join(PrincipalRole, PrincipalRole.principal_id == PrincipalRow.id)
        .where(
            PrincipalRow.issuer == get_settings().DEEJAYTOOLS_CLERK_ISSUER,
            PrincipalRow.subject == User.id,
            PrincipalRole.role_name == ROLE_ADMIN,
        )
    )


def _user_columns() -> tuple[Any, ...]:
    song_count = (
        select(func.count())
        .select_from(Song)
        .where(Song.user_id == User.id, Song.deleted_at.is_(None))
        .scalar_subquery()
    )
    partner_count = (
        select(func.count())
        .select_from(Partner)
        .where(Partner.user_id == User.id, Partner.kind == "partner")
        .scalar_subquery()
    )
    return (
        User.id,
        User.email,
        User.first_name,
        User.last_name,
        User.created_at,
        _is_admin().label("is_admin"),
        song_count.label("song_count"),
        partner_count.label("partner_count"),
    )


def _map_user(row: Any) -> dict[str, Any]:
    return {
        "id": row.id,
        "email": row.email,
        "first_name": row.first_name,
        "last_name": row.last_name,
        "role": "admin" if row.is_admin else "user",
        "created_at": row.created_at,
        "song_count": int(row.song_count or 0),
        "partner_count": int(row.partner_count or 0),
    }


async def _user_exists(session: AsyncSession, user_id: str) -> bool:
    found = (
        await session.execute(select(User.id).where(User.id == user_id).limit(1))
    ).first()
    return found is not None


@router.get(
    "",
    response_model=AdminUserListResponse,
    summary="List every user",
    description=(
        "Every account with song and partner counts, oldest first. Optional "
        "q searches email and names (case-insensitive); optional role filters "
        "on the role derived from the identity store. Requires "
        "deejaytools.users.read."
    ),
    responses={
        **AUTH,
        400: {"model": ErrorResponse, "description": "Invalid query."},
    },
)
async def list_users(
    _caller: Caller = Depends(require_scope(READ_SCOPE)),
    query: ListQuery = Depends(zod_query(ListQuery)),
    session: AsyncSession = Depends(get_db_session),
) -> Any:
    """List every user."""
    conditions: list[ColumnElement[bool]] = []
    if query.q and js_trim(query.q):
        term = f"%{js_trim(query.q)}%"
        conditions.append(
            or_(
                User.email.ilike(term),
                User.first_name.ilike(term),
                User.last_name.ilike(term),
            )
        )
    if query.role == "admin":
        conditions.append(_is_admin())
    elif query.role == "user":
        conditions.append(not_(_is_admin()))

    rows = (
        await session.execute(
            select(*_user_columns()).where(*conditions).order_by(User.created_at.asc())
        )
    ).all()
    return success_list([_map_user(r) for r in rows])


@router.patch(
    "/{id}/role",
    response_model=AdminUserResponse,
    summary="Promote or demote a user",
    description=(
        "Grants or revokes deejaytools-admin in the identity store and writes "
        "users.role, in one transaction. An admin cannot change their own "
        "role away from admin. Idempotent. Requires deejaytools.users.write."
    ),
    responses={
        **AUTH,
        400: {"model": ErrorResponse, "description": "Invalid body."},
        404: {"model": ErrorResponse, "description": "No such user."},
    },
)
async def update_role(
    id: UserId,
    caller: Caller = Depends(require_scope(WRITE_SCOPE)),
    body: UpdateRoleBody = Depends(zod_body(UpdateRoleBody)),
    session: AsyncSession = Depends(get_db_session),
) -> Any:
    """Promote or demote a user."""
    if id == caller.user_id and body.role != "admin":
        logger.warning(
            with_log_prefix(
                LOG_WARNING, f"admin_self_demote_blocked user={caller.user_id}"
            )
        )
        # Lower-case code, as deejaytools-api answers here.
        raise api_error(403, "forbidden", "You cannot change your own admin role.")

    target = (
        await session.execute(
            select(User.id, User.email, User.role).where(User.id == id).limit(1)
        )
    ).first()
    if target is None:
        raise api_error(404, ErrorCode.NOT_FOUND, "Not found")

    # One transaction: the grant in the identity store (authority here) and
    # the users.role mirror (authority in deejaytools-api), so either service
    # sees the same admins.
    try:
        await session.execute(
            update(User)
            .where(User.id == id)
            .values(role=body.role, updated_at=int(time.time() * 1000))
        )
        principal_id = await ensure_principal(
            session,
            issuer=get_settings().DEEJAYTOOLS_CLERK_ISSUER or "",
            subject=id,
            email=target.email or None,
            users_role=target.role,
            granted_by=caller.user_id,
        )
        if body.role == "admin":
            await session.execute(
                insert(PrincipalRole)
                .values(
                    principal_id=principal_id,
                    role_name=ROLE_ADMIN,
                    granted_by=caller.user_id,
                )
                .on_conflict_do_nothing(
                    index_elements=[PrincipalRole.principal_id, PrincipalRole.role_name]
                )
            )
        else:
            await session.execute(
                delete(PrincipalRole).where(
                    PrincipalRole.principal_id == principal_id,
                    PrincipalRole.role_name == ROLE_ADMIN,
                )
            )
        await session.commit()
    except Exception:
        await session.rollback()
        raise

    updated = (
        await session.execute(select(*_user_columns()).where(User.id == id).limit(1))
    ).one()
    return success(_map_user(updated))


@router.get(
    "/{id}/partners",
    response_model=PartnerListResponse,
    summary="A user's partners",
    description=(
        "The target user's partners of kind 'partner', by last then first "
        "name, for the Upload For flow. Requires deejaytools.users.read."
    ),
    responses={
        **AUTH,
        404: {"model": ErrorResponse, "description": "No such user."},
    },
)
async def list_user_partners(
    id: UserId,
    _caller: Caller = Depends(require_scope(READ_SCOPE)),
    session: AsyncSession = Depends(get_db_session),
) -> Any:
    """A user's partners."""
    if not await _user_exists(session, id):
        raise api_error(404, ErrorCode.NOT_FOUND, "User not found")
    rows = (
        (
            await session.execute(
                select(Partner)
                .where(Partner.user_id == id, Partner.kind == "partner")
                .order_by(Partner.last_name.asc(), Partner.first_name.asc())
            )
        )
        .scalars()
        .all()
    )
    return success_list([map_partner(r) for r in rows])


@router.get(
    "/{id}/event-song-submissions",
    response_model=SubmissionListResponse,
    summary="A user's submissions to an event",
    description=(
        "Submissions the target user made to one event (query event_id), "
        "newest first. Requires deejaytools.users.read."
    ),
    responses={
        **AUTH,
        400: {"model": ErrorResponse, "description": "Missing event_id."},
        404: {"model": ErrorResponse, "description": "No such user."},
    },
)
async def list_user_submissions(
    id: UserId,
    _caller: Caller = Depends(require_scope(READ_SCOPE)),
    query: SubmissionsQuery = Depends(zod_query(SubmissionsQuery)),
    session: AsyncSession = Depends(get_db_session),
) -> Any:
    """A user's submissions to an event."""
    if not await _user_exists(session, id):
        raise api_error(404, ErrorCode.NOT_FOUND, "User not found")
    rows = await fetch_user_submission_rows(session, id, event_id=query.event_id)
    return success_list([map_submission_row(r) for r in rows])
