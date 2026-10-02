"""``/v1/auth`` — sync and the caller's own record (deejaytools-api docs/API.md).

Both routes here are authenticated-only (see ``auth``'s module docstring):
they verify the credential and require no scope.
"""

from __future__ import annotations

import time
from typing import Any, Literal

from fastapi import APIRouter, Depends, Header
from mini_app_polis.logger import LOG_WARNING, get_logger, with_log_prefix
from pydantic import BaseModel, EmailStr, Field, model_validator
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import provision_principal, resolve_principal, verify_bearer, wire_role
from ..database import get_db_session
from ..errors import (
    ErrorCode,
    ErrorResponse,
    Meta,
    api_error,
    success,
    user_not_synced,
)
from ..models import User

logger = get_logger()

router = APIRouter(prefix="/v1/auth", tags=["auth"])


class SyncBody(BaseModel):
    """Body of ``POST /v1/auth/sync``. Field names are the web app's (camelCase)."""

    email: EmailStr = Field(..., description="The caller's email address.")
    firstName: str | None = Field(None, description="First name, at creation only.")
    lastName: str | None = Field(None, description="Last name, at creation only.")
    displayName: str | None = Field(None, description="Display name, at creation only.")

    @model_validator(mode="before")
    @classmethod
    def reject_nulls(cls, data: Any) -> Any:
        """Optional means absent, not null, as zod's ``.optional()`` had it."""
        if isinstance(data, dict):
            nulls = [
                k
                for k in ("firstName", "lastName", "displayName")
                if k in data and data[k] is None
            ]
            if nulls:
                raise ValueError(f"{', '.join(nulls)} must be a string when present")
        return data


class UserData(BaseModel):
    """A user as ``/v1/auth`` returns it."""

    id: str = Field(..., description="Clerk user id (the JWT sub).")
    email: str = Field(..., description="Email address.")
    display_name: str | None = Field(None, description="Display name.")
    first_name: str | None = Field(None, description="First name.")
    last_name: str | None = Field(None, description="Last name.")
    role: Literal["user", "admin"] = Field(
        ..., description="'admin' when the caller holds deejaytools-admin."
    )
    created_at: int = Field(..., description="Created, epoch milliseconds.")
    updated_at: int = Field(..., description="Last updated, epoch milliseconds.")


class UserResponse(BaseModel):
    """Success envelope carrying one user."""

    data: UserData = Field(..., description="The caller's record.")
    meta: Meta = Field(..., description="Response metadata.")


def _user_data(row: User, role: Literal["user", "admin"]) -> dict[str, Any]:
    return {
        "id": row.id,
        "email": row.email,
        "display_name": row.display_name,
        "first_name": row.first_name,
        "last_name": row.last_name,
        "role": role,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _is_unique_violation(exc: IntegrityError, constraint: str) -> bool:
    cause = getattr(exc.orig, "__cause__", None)
    return getattr(cause, "constraint_name", None) == constraint


@router.post(
    "/sync",
    response_model=UserResponse,
    summary="Create or refresh the caller's user record",
    description=(
        "Upserts the caller's users row and provisions their principal "
        "(deejaytools-dancer) in one transaction. On conflict only email and "
        "updated_at change; names are user-managed after creation. "
        "Authenticated-only: a verified credential, no scope."
    ),
    responses={
        401: {"model": ErrorResponse, "description": "Missing or invalid token."},
        409: {
            "model": ErrorResponse,
            "description": "The email belongs to another sign-in's users row.",
        },
    },
)
async def sync(
    body: SyncBody,
    authorization: str | None = Header(default=None, alias="Authorization"),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Upsert the users row and provision the principal.

    The body is validated before the token is checked, so a bad body answers
    400 even without a token — the order deejaytools-api has.
    """
    subject = await verify_bearer(authorization)
    now = int(time.time() * 1000)
    try:
        await session.execute(
            insert(User)
            .values(
                id=subject.subject,
                email=body.email,
                first_name=body.firstName,
                last_name=body.lastName,
                display_name=body.displayName,
                role="user",
                created_at=now,
                updated_at=now,
            )
            # Names are user-managed after account creation; sync must not
            # overwrite them.
            .on_conflict_do_update(
                index_elements=[User.id],
                set_={"email": body.email, "updated_at": now},
            )
        )
    except IntegrityError as exc:
        await session.rollback()
        # The email already belongs to another users row: a Clerk account
        # deleted and re-created, or an email changed in Clerk to one an old
        # row still holds. Refused rather than handed the other row's data.
        if _is_unique_violation(exc, "users_email_unique"):
            logger.warning(
                with_log_prefix(
                    LOG_WARNING, f"auth_sync_email_conflict user={subject.subject}"
                )
            )
            raise api_error(
                409,
                ErrorCode.EMAIL_BELONGS_TO_ANOTHER_ACCOUNT,
                "This email address is already linked to a different sign-in. "
                "Contact an organizer to have your account moved to this sign-in.",
            ) from exc
        raise

    row = await session.get(User, subject.subject)
    if row is None:
        raise api_error(500, ErrorCode.INTERNAL, "Failed to load user")
    await provision_principal(subject, session, users_role=row.role)
    await session.commit()

    principal = await resolve_principal(subject, session)
    return success(_user_data(row, wire_role(principal)))


@router.get(
    "/me",
    response_model=UserResponse,
    summary="The caller's user record",
    description=(
        "Returns the caller's users row, with role derived from their "
        "principal's grants. A valid token with no users row answers 401 "
        "USER_NOT_SYNCED. Authenticated-only: a verified credential, no scope."
    ),
    responses={
        401: {
            "model": ErrorResponse,
            "description": "Missing or invalid token, or not yet synced.",
        },
    },
)
async def me(
    authorization: str | None = Header(default=None, alias="Authorization"),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Return the caller's record."""
    subject = await verify_bearer(authorization)
    row = await session.get(User, subject.subject)
    if row is None:
        raise user_not_synced()
    principal = await resolve_principal(subject, session)
    return success(_user_data(row, wire_role(principal)))
