"""Authentication and authorization — an identity enforcement point.

Verification, the decision and the store come from the shared ``identity``
library (AUTH-004); this module is configuration and thin FastAPI adapters,
shaped like api-kaianolevine-com's ``auth.py``. Its answers are
deejaytools-api's (ADR-009):

    no or bad credential              401 UNAUTHORIZED   "Authentication required"
    valid credential, no principal    401 USER_NOT_SYNCED "Call POST /v1/auth/sync first"
    principal without the scope       403 FORBIDDEN      "Admin access required"

``require_scope(...)`` is the guard for every scoped route: verify, resolve,
authorize, emit audit, once per request. Every decision is audited, allow and
deny alike. The route-to-scope table is deejaytools-api ADR-007.

Public routes (API-008) — no credential required, none read except where
noted:

    GET  /health
    GET  /v1/events
    GET  /v1/events/{id}
    GET  /v1/sessions               a credential is read, never required
    GET  /v1/sessions/{id}          a credential is read, never required
    GET  /v1/queue/{session_id}/active
    GET  /v1/queue/{session_id}/waiting
    POST /v1/feedback

Operator: ``GET /internal/tick``, gated by TICK_SECRET and failing closed
when it is unset.

Authenticated-only — a verified credential, no scope (AUTH-003). This is the
whole list; a route added to it needs a reason of the same shape:

    POST /v1/auth/sync   where a person becomes a principal. It cannot
                         require one.
    GET  /v1/auth/me     reads only the caller's own record, including
                         whether they have a principal yet.

There are no machine callers (CD-030 is exempt), so the only verifier is
Clerk's, for the one issuer in settings.

Header parity (AUTH-002): callers send ``Authorization: Bearer <token>``, the
header common-python-utils' ``CommonPythonApiClient`` sends. Today the only
caller is the web app; a cog calling this API would use that client.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from functools import lru_cache
from typing import Literal

from fastapi import Depends, Header, Request
from identity.clerk import ClerkIssuer, ClerkVerifier
from identity.policy import authorize as decide
from identity.store import (
    Issuer,
    PrincipalRole,
    SqlAlchemyAuditSink,
    SqlAlchemyPrincipalStore,
    new_audit_event,
)
from identity.store import Principal as PrincipalRow
from identity.types import Principal, VerifiedSubject
from mini_app_polis.logger import LOG_START, LOG_WARNING, get_logger, with_log_prefix
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from .config import get_settings
from .database import get_db_session
from .errors import forbidden, unauthorized, user_not_synced
from .models import User

logger = get_logger()

ENFORCEMENT_POINT = "api-deejaytools"

ROLE_DANCER = "deejaytools-dancer"
ROLE_ADMIN = "deejaytools-admin"


# ---------------------------------------------------------------------------
# Verify
# ---------------------------------------------------------------------------


@lru_cache(maxsize=4)
def _verifier(issuer: str, jwks_url: str) -> ClerkVerifier:
    """Cached across requests: the JWKS cache lives inside the verifier."""
    return ClerkVerifier([ClerkIssuer(issuer=issuer, jwks_url=jwks_url)])


def get_verifier() -> ClerkVerifier | None:
    """The verifier for the configured issuer, or None when none is configured."""
    settings = get_settings()
    issuer, jwks_url = settings.clerk_issuer, settings.clerk_jwks_url
    if not (issuer and jwks_url):
        return None
    return _verifier(issuer, jwks_url)


async def verify_bearer(authorization: str | None) -> VerifiedSubject:
    """Verify ``Authorization: Bearer <Clerk session JWT>``. Raises 401 on any failure.

    As deejaytools-api: the scheme must be exactly ``Bearer `` and every
    failure — no header, bad token, untrusted issuer, unreachable JWKS,
    unconfigured issuer — is the same 401. The reason is logged with the
    verifier's own message, so an environment fault (an unreachable JWKS, a
    rotated key) does not read in the logs like ordinary bad tokens.
    """
    if not authorization or not authorization.startswith("Bearer "):
        logger.warning(with_log_prefix(LOG_WARNING, "auth_failed: missing_token"))
        raise unauthorized()
    verifier = get_verifier()
    if verifier is None:
        logger.warning(
            with_log_prefix(
                LOG_WARNING,
                "auth_failed: DEEJAYTOOLS_CLERK_ISSUER / DEEJAYTOOLS_CLERK_JWKS_URL unset",
            )
        )
        raise unauthorized()
    try:
        return await verifier.verify(authorization[len("Bearer ") :])
    except Exception as exc:  # noqa: BLE001 - every failure is a 401, as today
        logger.warning(with_log_prefix(LOG_WARNING, f"auth_failed: {exc!r}"))
        raise unauthorized() from exc


# ---------------------------------------------------------------------------
# Resolve
# ---------------------------------------------------------------------------


def _store(session: AsyncSession) -> SqlAlchemyPrincipalStore:
    return SqlAlchemyPrincipalStore(session, enforcement_point=ENFORCEMENT_POINT)


async def resolve_principal(
    subject: VerifiedSubject, session: AsyncSession
) -> Principal | None:
    """The caller's principal, or None if they have not been provisioned."""
    return await _store(session).resolve(subject)


def wire_role(principal: Principal | None) -> Literal["user", "admin"]:
    """The ``role`` the web app sees: derived from the grant, not the column."""
    return (
        "admin" if principal is not None and ROLE_ADMIN in principal.roles else "user"
    )


async def provision_principal(
    subject: VerifiedSubject, session: AsyncSession, *, users_role: str
) -> None:
    """Make sure a signed-in person is a principal with ``deejaytools-dancer``.

    Idempotent, and does not commit: ``POST /v1/auth/sync`` runs it in the
    same transaction as its ``users`` upsert.

    - The configured issuer row is written if missing. Each environment has
      its own Clerk instance, and the conformance suite rebuilds the schema
      under a running service, so the row cannot be relied on to come from a
      migration or from startup.
    - A principal created here also gets ``deejaytools-admin`` when the
      ``users`` row already says admin: someone promoted through
      deejaytools-api while traffic was rolled back. Same rule as the
      backfill (scripts/backfill_principals.py). Never re-applied to an
      existing principal, so admin access changes as a grant, not a column.
    - ``deejaytools-dancer`` is ensured on every call.
    """
    await ensure_principal(
        session,
        issuer=subject.issuer,
        subject=subject.subject,
        email=str(subject.claims.get("email") or "") or None,
        users_role=users_role,
        granted_by="auth_sync",
    )


async def ensure_principal(
    session: AsyncSession,
    *,
    issuer: str,
    subject: str,
    email: str | None,
    users_role: str,
    granted_by: str,
) -> uuid.UUID:
    """The principal for ``(issuer, subject)``, provisioned if missing.

    ``provision_principal``'s rules, for a subject known by its ids rather
    than by a verified credential: the issuer row is written if missing, a
    new principal also gets ``deejaytools-admin`` when ``users_role`` says
    admin, and ``deejaytools-dancer`` is ensured on every call, granted by
    ``granted_by``. Idempotent, and does not commit. Returns the principal id.

    Also used by ``PATCH /v1/admin/users/{id}/role`` for a target who signed
    up through deejaytools-api while traffic was rolled back, so the grant
    has a principal to go to.
    """
    settings = get_settings()
    await session.execute(
        insert(Issuer)
        .values(
            issuer=issuer,
            display_name="deejaytools (Clerk)",
            jwks_url=settings.clerk_jwks_url,
        )
        .on_conflict_do_nothing(index_elements=[Issuer.issuer])
    )
    created = (
        await session.execute(
            insert(PrincipalRow)
            .values(
                kind="human",
                issuer=issuer,
                subject=subject,
                display_name=email or "",
                email=email,
            )
            .on_conflict_do_nothing(
                index_elements=[PrincipalRow.issuer, PrincipalRow.subject]
            )
            .returning(PrincipalRow.id)
        )
    ).scalar_one_or_none()
    principal_id: uuid.UUID = (
        created
        or (
            await session.execute(
                select(PrincipalRow.id).where(
                    PrincipalRow.issuer == issuer,
                    PrincipalRow.subject == subject,
                )
            )
        ).scalar_one()
    )

    roles = [ROLE_DANCER]
    if created is not None and users_role == "admin":
        roles.append(ROLE_ADMIN)
    await session.execute(
        insert(PrincipalRole)
        .values(
            [
                {
                    "principal_id": principal_id,
                    "role_name": r,
                    "granted_by": granted_by,
                }
                for r in roles
            ]
        )
        .on_conflict_do_nothing(
            index_elements=[PrincipalRole.principal_id, PrincipalRole.role_name]
        )
    )
    if created is not None:
        logger.info(
            with_log_prefix(LOG_START, f"provisioned principal {subject} roles={roles}")
        )
    return principal_id


# ---------------------------------------------------------------------------
# Authorize + audit
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Caller:
    """Who a scoped request is from, once it has been allowed."""

    principal: Principal
    subject: VerifiedSubject

    @property
    def user_id(self) -> str:
        """The ``users.id`` of the caller: their Clerk subject (API-006 pattern 1)."""
        return self.subject.subject


def require_scope(scope: str):
    """Build a FastAPI dependency enforcing one scope.

    Runs the whole contract once per request: verify, resolve, authorize,
    emit audit. The audit event is written for allow and deny alike, with
    the real scope, even though a deny answers deejaytools-api's generic
    "Admin access required".
    """

    async def _dependency(
        request: Request,
        authorization: str | None = Header(default=None, alias="Authorization"),
        session: AsyncSession = Depends(get_db_session),
    ) -> Caller:
        subject = await verify_bearer(authorization)
        store = _store(session)
        principal = await store.resolve(subject)
        decision = decide(principal, scope, await store.load_roles())

        await SqlAlchemyAuditSink(session).emit_audit(
            new_audit_event(
                enforcement_point=ENFORCEMENT_POINT,
                scope=scope,
                allowed=decision.allowed,
                reason=decision.reason,
                principal=principal,
                subject=subject,
                request_id=request.headers.get("X-Request-Id"),
            )
        )

        if principal is None:
            # deejaytools-api's answer for a valid token with no users row.
            raise user_not_synced()
        if not decision.allowed:
            raise forbidden()
        return Caller(principal=principal, subject=subject)

    return _dependency


# ---------------------------------------------------------------------------
# Optional caller, for the two public session reads
# ---------------------------------------------------------------------------


async def optional_synced_user_id(
    authorization: str | None, session: AsyncSession
) -> str | None:
    """The caller's users.id when they send a valid token and have synced.

    Used only by GET /v1/sessions and GET /v1/sessions/{id}, which are
    public: a credential is read, never required. No token, a bad token or
    an unsynced caller all mean anonymous, with no error (deejaytools-api
    lib/optional-user.ts). No scope is checked and no decision is audited:
    nothing is authorized here, the caller only learns about their own
    check-ins.
    """
    if not authorization or not authorization.startswith("Bearer "):
        return None
    verifier = get_verifier()
    if verifier is None:
        return None
    try:
        subject = await verifier.verify(authorization[len("Bearer ") :])
    except Exception:  # noqa: BLE001 - anonymous on any failure, as today
        return None
    row = await session.get(User, subject.subject)
    return row.id if row is not None else None


# ---------------------------------------------------------------------------
# Acting for another user (ADR-007, "Acting on another user's behalf")
# ---------------------------------------------------------------------------

DELEGATION_SCOPE = "deejaytools.delegation.act"


async def authorize_delegation(
    caller: Caller, target_user_id: str, session: AsyncSession, request: Request
) -> None:
    """The second decision a handler makes when ``on_behalf_of_user_id`` is sent.

    The route's own scope has already allowed the caller to act at all; this
    decides whether they may act for someone else, through the same
    authorize-and-audit path. The target is the subject of the action, never
    the caller's identity: it is recorded as the audit event's resource. A
    deny answers what deejaytools-api answers to a non-admin here, 403
    FORBIDDEN "Admin access required". Call it before reading the target.
    """
    store = _store(session)
    decision = decide(caller.principal, DELEGATION_SCOPE, await store.load_roles())
    await SqlAlchemyAuditSink(session).emit_audit(
        new_audit_event(
            enforcement_point=ENFORCEMENT_POINT,
            scope=DELEGATION_SCOPE,
            allowed=decision.allowed,
            reason=decision.reason,
            principal=caller.principal,
            subject=caller.subject,
            resource=target_user_id,
            request_id=request.headers.get("X-Request-Id"),
        )
    )
    if not decision.allowed:
        raise forbidden()
