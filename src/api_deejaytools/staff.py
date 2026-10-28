"""Per-event staff grants (ADR-0012): the staff list and its explicit grants.

A person is on an event's staff as ``coordinator``, ``dj`` or
``event-manager``: one ``event_staff_grants`` row each. A role's authority
on the event is the identity store's explicit grants, one
``(scope, event:<event_id>)`` pair per scope of the role's definition in
``identity_role_scopes``. The two are only ever written together, here, in
the caller's transaction: nothing in this module commits.
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Mapping
from typing import Any

from identity.store import ExplicitGrant
from identity.store import Principal as PrincipalRow
from identity.types import Role
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from .auth import STAFF_ROLES, ensure_principal, event_resource, load_roles
from .config import get_settings
from .models import EventStaffGrant, User


def role_scopes(role: str, roles: Mapping[str, Role]) -> frozenset[str]:
    """The scopes a staff role carries on its event, from the role table.

    An unknown role, or one whose definition is missing from the store,
    carries nothing.
    """
    definition = roles.get(STAFF_ROLES.get(role, ""))
    return definition.scopes if definition is not None else frozenset()


def scopes_to_remove(
    revoked: str, remaining: Iterable[str], roles: Mapping[str, Role]
) -> frozenset[str]:
    """The explicit grants a revoke removes.

    The revoked role's scopes, less any still carried by another role the
    same person holds on the same event.
    """
    kept: set[str] = set()
    for role in remaining:
        if role != revoked:
            kept |= role_scopes(role, roles)
    return role_scopes(revoked, roles) - kept


def grant_source(role: str, granter: str) -> str:
    """``granted_by`` on a derived explicit grant: the role and who granted it."""
    return f"{STAFF_ROLES[role]}:{granter}"


def _issuer() -> str:
    return get_settings().DEEJAYTOOLS_CLERK_ISSUER or ""


def map_grant(row: Any) -> dict[str, Any]:
    """A staff grant on the wire, with the person's name and email."""
    return {
        "event_id": row.event_id,
        "user_id": row.user_id,
        "role": row.role,
        "email": row.email,
        "first_name": row.first_name,
        "last_name": row.last_name,
        "granted_by": row.granted_by,
        "granted_at": row.granted_at,
    }


def _grant_columns() -> tuple[Any, ...]:
    return (
        EventStaffGrant.event_id,
        EventStaffGrant.user_id,
        EventStaffGrant.role,
        User.email,
        User.first_name,
        User.last_name,
        EventStaffGrant.granted_by,
        EventStaffGrant.granted_at,
    )


async def list_event_staff(
    session: AsyncSession, event_id: str
) -> list[dict[str, Any]]:
    """The event's staff, by role then by when they were granted."""
    rows = (
        await session.execute(
            select(*_grant_columns())
            .join(User, User.id == EventStaffGrant.user_id)
            .where(EventStaffGrant.event_id == event_id)
            .order_by(
                EventStaffGrant.role,
                EventStaffGrant.granted_at,
                EventStaffGrant.user_id,
            )
        )
    ).all()
    return [map_grant(r) for r in rows]


async def event_roles_for(session: AsyncSession, user_id: str) -> list[dict[str, str]]:
    """``event_roles`` for ``GET /v1/auth/me``: the person's staff roles."""
    rows = (
        await session.execute(
            select(EventStaffGrant.event_id, EventStaffGrant.role)
            .where(EventStaffGrant.user_id == user_id)
            .order_by(EventStaffGrant.event_id, EventStaffGrant.role)
        )
    ).all()
    return [{"event_id": r.event_id, "role": r.role} for r in rows]


async def grant_event_role(
    session: AsyncSession,
    *,
    event_id: str,
    user_id: str,
    email: str | None,
    role: str,
    granter: str,
) -> dict[str, Any]:
    """Put a person on the event's staff in ``role``. Idempotent.

    Writes the staff row and one explicit grant per scope of the role on
    ``event:<event_id>``. A grant already held keeps its original
    ``granted_by`` and ``granted_at``. The person is provisioned as a
    principal first if they have a ``users`` row but no principal yet, as
    the admin role change does.
    """
    principal_id = await ensure_principal(
        session, issuer=_issuer(), subject=user_id, email=email, granted_by=granter
    )
    await session.execute(
        insert(EventStaffGrant)
        .values(
            event_id=event_id,
            user_id=user_id,
            role=role,
            granted_by=granter,
            granted_at=int(time.time() * 1000),
        )
        .on_conflict_do_nothing(
            index_elements=[
                EventStaffGrant.event_id,
                EventStaffGrant.user_id,
                EventStaffGrant.role,
            ]
        )
    )
    scopes = sorted(role_scopes(role, await load_roles(session)))
    if scopes:
        await session.execute(
            insert(ExplicitGrant)
            .values(
                [
                    {
                        "principal_id": principal_id,
                        "scope": scope,
                        "resource": event_resource(event_id),
                        "granted_by": grant_source(role, granter),
                    }
                    for scope in scopes
                ]
            )
            .on_conflict_do_nothing(
                index_elements=[
                    ExplicitGrant.principal_id,
                    ExplicitGrant.scope,
                    ExplicitGrant.resource,
                ]
            )
        )
    row = (
        await session.execute(
            select(*_grant_columns())
            .join(User, User.id == EventStaffGrant.user_id)
            .where(
                EventStaffGrant.event_id == event_id,
                EventStaffGrant.user_id == user_id,
                EventStaffGrant.role == role,
            )
        )
    ).one()
    return map_grant(row)


async def revoke_event_role(
    session: AsyncSession, *, event_id: str, user_id: str, role: str
) -> bool:
    """Take ``role`` on the event away from a person. Idempotent.

    Deletes the staff row and every explicit grant of the role's scopes on
    ``event:<event_id>`` that no other role they still hold there carries.
    The explicit grants are cleaned up even when the row is already gone.
    Returns whether a staff row was deleted.
    """
    deleted = (
        await session.execute(
            delete(EventStaffGrant)
            .where(
                EventStaffGrant.event_id == event_id,
                EventStaffGrant.user_id == user_id,
                EventStaffGrant.role == role,
            )
            .returning(EventStaffGrant.role)
        )
    ).first()
    remaining = (
        await session.execute(
            select(EventStaffGrant.role).where(
                EventStaffGrant.event_id == event_id,
                EventStaffGrant.user_id == user_id,
            )
        )
    ).scalars()
    drop = scopes_to_remove(role, list(remaining), await load_roles(session))
    principal_id = (
        await session.execute(
            select(PrincipalRow.id).where(
                PrincipalRow.issuer == _issuer(), PrincipalRow.subject == user_id
            )
        )
    ).scalar_one_or_none()
    if drop and principal_id is not None:
        await session.execute(
            delete(ExplicitGrant).where(
                ExplicitGrant.principal_id == principal_id,
                ExplicitGrant.resource == event_resource(event_id),
                ExplicitGrant.scope.in_(sorted(drop)),
            )
        )
    return deleted is not None


async def purge_event_staff(session: AsyncSession, event_id: str) -> None:
    """For an event delete: its staff rows and every explicit grant on it."""
    await session.execute(
        delete(EventStaffGrant).where(EventStaffGrant.event_id == event_id)
    )
    await session.execute(
        delete(ExplicitGrant).where(ExplicitGrant.resource == event_resource(event_id))
    )
