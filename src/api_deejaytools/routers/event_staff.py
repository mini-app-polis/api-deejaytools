"""``/v1/events/{id}/staff``: an event's staff roles (ADR-0012).

Who is on an event's staff as coordinator, DJ or event manager, and the
grant and revoke of those roles. Every route here is event-scoped: the scope
held through a role (an admin's) allows on any event, a grant of it on
``event:<id>`` allows on that event only. Listing needs
``deejaytools.staff.read``; granting and revoking need
``deejaytools.staff.write``, and a second decision on the change itself,
which keeps the event-manager role an admin's to give (``auth``'s
``authorize_staff_change``).
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import (
    STAFF_WRITE_SCOPE,
    Caller,
    EventRef,
    StaffRole,
    authorize_staff_change,
    require_event_scope,
)
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
from ..models import Event, User
from ..staff import grant_event_role, list_event_staff, revoke_event_role

router = APIRouter(prefix="/v1/events", tags=["event-staff"])

STAFF_READ_SCOPE = "deejaytools.staff.read"
THIS_EVENT = EventRef(source="path", name="id")


class StaffGrantData(BaseModel):
    """One person's role on an event's staff."""

    event_id: str = Field(..., description="Event id.")
    user_id: str = Field(..., description="users.id of the staff member.")
    role: StaffRole = Field(..., description="coordinator, dj or event-manager.")
    email: str = Field(..., description="The staff member's email.")
    first_name: str | None = Field(None, description="First name.")
    last_name: str | None = Field(None, description="Last name.")
    granted_by: str = Field(..., description="users.id of who granted it.")
    granted_at: int = Field(..., description="Granted, epoch ms.")


class StaffGrantResponse(BaseModel):
    """One staff grant."""

    data: StaffGrantData = Field(..., description="The grant.")
    meta: Meta = Field(..., description="Response metadata.")


class StaffListResponse(BaseModel):
    """An event's staff, by role, then oldest grant first."""

    data: list[StaffGrantData] = Field(..., description="The grants.")
    meta: ListMeta = Field(..., description="Response metadata.")


class RevokedData(BaseModel):
    """Confirmation of a revoke."""

    deleted: bool = Field(..., description="Always true.")


class RevokedResponse(BaseModel):
    """Revoke confirmation envelope."""

    data: RevokedData = Field(..., description="Confirmation.")
    meta: Meta = Field(..., description="Response metadata.")


EventId = Annotated[str, Path(description="Event id.")]
UserId = Annotated[str, Path(description="users.id of the staff member.")]
RoleName = Annotated[StaffRole, Path(description="coordinator, dj or event-manager.")]

AUTH: dict[int | str, dict[str, Any]] = {
    401: {"model": ErrorResponse, "description": "Missing or invalid token."},
    403: {"model": ErrorResponse, "description": "Lacks the scope on this event."},
}
CHANGE: dict[int | str, dict[str, Any]] = {
    **AUTH,
    400: {"model": ErrorResponse, "description": "Unknown role."},
    404: {"model": ErrorResponse, "description": "No such event or user."},
}


async def _require_event(session: AsyncSession, event_id: str) -> None:
    if await session.get(Event, event_id) is None:
        raise api_error(404, ErrorCode.NOT_FOUND, "Event not found")


async def _target_email(session: AsyncSession, user_id: str) -> str:
    email = (
        await session.execute(select(User.email).where(User.id == user_id).limit(1))
    ).scalar_one_or_none()
    if email is None:
        raise api_error(404, ErrorCode.NOT_FOUND, "User not found")
    return email


@router.get(
    "/{id}/staff",
    response_model=StaffListResponse,
    summary="An event's staff",
    description=(
        "Everyone holding a staff role on the event, by role. Requires "
        "deejaytools.staff.read on the event."
    ),
    responses={
        **AUTH,
        404: {"model": ErrorResponse, "description": "No such event."},
    },
)
async def list_staff(
    id: EventId,
    _caller: Caller = Depends(require_event_scope(STAFF_READ_SCOPE, THIS_EVENT)),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """List the event's staff."""
    await _require_event(session, id)
    return success_list(await list_event_staff(session, id))


@router.put(
    "/{id}/staff/{user_id}/{role}",
    response_model=StaffGrantResponse,
    summary="Grant a staff role on an event",
    description=(
        "Puts the user on the event's staff in the role, with the role's "
        "scopes on this event only. Idempotent: granting a role already held "
        "changes nothing. Requires deejaytools.staff.write on the event; "
        "event-manager is granted only by an admin."
    ),
    responses=CHANGE,
)
async def grant_staff(
    id: EventId,
    user_id: UserId,
    role: RoleName,
    request: Request,
    caller: Caller = Depends(require_event_scope(STAFF_WRITE_SCOPE, THIS_EVENT)),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Grant a staff role."""
    await authorize_staff_change(
        caller,
        event_id=id,
        user_id=user_id,
        role=role,
        session=session,
        request=request,
    )
    await _require_event(session, id)
    email = await _target_email(session, user_id)
    try:
        grant = await grant_event_role(
            session,
            event_id=id,
            user_id=user_id,
            email=email or None,
            role=role,
            granter=caller.user_id,
        )
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    return success(grant)


@router.delete(
    "/{id}/staff/{user_id}/{role}",
    response_model=RevokedResponse,
    summary="Revoke a staff role on an event",
    description=(
        "Takes the role on the event away from the user, with each of its "
        "scopes no other role they hold there carries. Idempotent. Requires "
        "deejaytools.staff.write on the event; event-manager is revoked only "
        "by an admin."
    ),
    responses=CHANGE,
)
async def revoke_staff(
    id: EventId,
    user_id: UserId,
    role: RoleName,
    request: Request,
    caller: Caller = Depends(require_event_scope(STAFF_WRITE_SCOPE, THIS_EVENT)),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Revoke a staff role."""
    await authorize_staff_change(
        caller,
        event_id=id,
        user_id=user_id,
        role=role,
        session=session,
        request=request,
    )
    await _require_event(session, id)
    await _target_email(session, user_id)
    try:
        await revoke_event_role(session, event_id=id, user_id=user_id, role=role)
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    return success({"deleted": True})
