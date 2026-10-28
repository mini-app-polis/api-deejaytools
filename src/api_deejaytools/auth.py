"""Authentication and authorization — an identity enforcement point.

Verification, the decision and the store come from the shared ``identity``
library (AUTH-004); this module is configuration and thin FastAPI adapters,
shaped like api-kaianolevine-com's ``auth.py``. Its answers are
deejaytools-api's (ADR-0010):

    no or bad credential              401 UNAUTHORIZED   "Authentication required"
    valid credential, no principal    401 USER_NOT_SYNCED "Call POST /v1/auth/sync first"
    principal without the scope       403 FORBIDDEN      "Admin access required"

``require_scope(...)`` is the guard for every scoped route: verify, resolve,
authorize, emit audit, once per request. Every decision is audited, allow and
deny alike. The route-to-scope table is ADR-0008.

``require_event_scope(scope, EventRef(...))`` is the same guard for a route
about one event (ADR-0012). The route says where the request names its event
(a path parameter or a body field) and whether that names the event itself,
a session or a queue entry; the guard resolves it to ``event:<event_id>`` and
decides with the caller's explicit grants on it. The scope held through a
role (an admin's) still allows anywhere; a per-event staff role allows only
on the event it was granted for.

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

Operator: ``GET /internal/tick``, gated by TICK_SECRET (or
TICK_SECRET_PREVIOUS during a rotation) and failing closed when TICK_SECRET is
unset.

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

import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Literal

from fastapi import Depends, Header, Request
from identity.clerk import ClerkIssuer, ClerkVerifier
from identity.errors import IssuerUnavailable
from identity.policy import authorize as decide
from identity.store import (
    Issuer,
    PrincipalRole,
    SqlAlchemyAuditSink,
    SqlAlchemyPrincipalStore,
    new_audit_event,
)
from identity.store import Principal as PrincipalRow
from identity.types import Principal, Role, VerifiedSubject
from mini_app_polis.logger import LOG_START, LOG_WARNING, get_logger, with_log_prefix
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from .config import get_settings
from .database import get_db_session
from .errors import api_error, forbidden, unauthorized, user_not_synced
from .models import QueueEntry, User
from .models import Session as FloorSession

logger = get_logger()

ENFORCEMENT_POINT = "api-deejaytools"

ROLE_DANCER = "deejaytools-dancer"
ROLE_ADMIN = "deejaytools-admin"

# Per-event staff roles (ADR-0012): the name on the wire and in
# event_staff_grants.role, and the identity role that defines its scopes.
# Granted on one event only, as explicit grants; never a principal role.
StaffRole = Literal["coordinator", "dj", "event-manager"]
STAFF_ROLES: dict[str, str] = {
    "coordinator": "deejaytools-event-coordinator",
    "dj": "deejaytools-event-dj",
    "event-manager": "deejaytools-event-manager",
}
EVENT_MANAGER = "event-manager"
STAFF_WRITE_SCOPE = "deejaytools.staff.write"


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
    """Verify ``Authorization: Bearer <Clerk session JWT>``.

    The scheme must be exactly ``Bearer ``. A credential that was judged and
    failed — no header, bad token, untrusted issuer, unconfigured issuer — is
    a 401. An issuer that could not be reached (``IssuerUnavailable``: its
    JWKS did not answer) is a 503: the credential was never judged, and a 401
    would make the web app sign the user out over an outage that heals on the
    next request. The reason is logged with the verifier's own message either
    way.
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
    except IssuerUnavailable as exc:
        logger.warning(with_log_prefix(LOG_WARNING, f"auth_unavailable: {exc!r}"))
        raise api_error(
            503, "AUTH_UNAVAILABLE", "Identity provider unreachable; retry shortly"
        ) from exc
    except Exception as exc:  # noqa: BLE001 - every judged failure is a 401
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
    """The ``role`` the web app sees, derived from the ``deejaytools-admin`` grant."""
    return (
        "admin" if principal is not None and ROLE_ADMIN in principal.roles else "user"
    )


async def provision_principal(subject: VerifiedSubject, session: AsyncSession) -> None:
    """Make sure a signed-in person is a principal with ``deejaytools-dancer``.

    Idempotent, and does not commit: ``POST /v1/auth/sync`` runs it in the
    same transaction as its ``users`` upsert.

    - The configured issuer row is written if missing. Each environment has
      its own Clerk instance, and the conformance suite rebuilds the schema
      under a running service, so the row cannot be relied on to come from a
      migration or from startup.
    - A principal created here is never granted ``deejaytools-admin``:
      admin access is only granted through
      ``PATCH /v1/admin/users/{id}/role``.
    - ``deejaytools-dancer`` is ensured on every call.
    """
    await ensure_principal(
        session,
        issuer=subject.issuer,
        subject=subject.subject,
        email=str(subject.claims.get("email") or "") or None,
        granted_by="auth_sync",
    )


async def ensure_principal(
    session: AsyncSession,
    *,
    issuer: str,
    subject: str,
    email: str | None,
    granted_by: str,
) -> uuid.UUID:
    """The principal for ``(issuer, subject)``, provisioned if missing.

    ``provision_principal``'s rules, for a subject known by its ids rather
    than by a verified credential: the issuer row is written if missing and
    ``deejaytools-dancer`` is ensured on every call, granted by
    ``granted_by``. Idempotent, and does not commit. Returns the principal id.

    Also used by ``PATCH /v1/admin/users/{id}/role`` for a target who has a
    ``users`` row but no principal yet, so the grant has a principal to go
    to.
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
# The event a request is about (ADR-0012)
# ---------------------------------------------------------------------------

EVENT_RESOURCE_PREFIX = "event:"


def event_resource(event_id: str) -> str:
    """The identity resource an event's staff grants are held on."""
    return f"{EVENT_RESOURCE_PREFIX}{event_id}"


@dataclass(frozen=True, slots=True)
class EventRef:
    """Where a request names the event it is about.

    ``source`` is ``"path"`` (a path parameter) or ``"body"`` (a field of
    the JSON body); ``name`` is the parameter or field. ``kind`` says what
    the value is the id of: the event itself, a session of it, or a queue
    entry in one of its sessions. A session or queue entry is looked up to
    its event before the decision.
    """

    source: Literal["path", "body"]
    name: str
    kind: Literal["event", "session", "queue_entry"] = "event"


def ref_value(ref: EventRef, path_params: Mapping[str, Any], body: Any) -> str | None:
    """The id ``ref`` points at in a request, or None when it is not there.

    Only a non-empty string counts: anything else is the body validator's
    to refuse, and decides as if no event were named.
    """
    if ref.source == "path":
        raw = path_params.get(ref.name)
    else:
        raw = body.get(ref.name) if isinstance(body, dict) else None
    return raw if isinstance(raw, str) and raw else None


async def _json_body(request: Request) -> Any:
    """The request's JSON body, or None. Starlette caches the bytes, so the
    route's own body validator reads them again."""
    try:
        raw = await request.body()
        return json.loads(raw) if raw else None
    except ValueError:  # UnicodeDecodeError and JSONDecodeError alike
        return None


async def resolve_event_id(
    ref: EventRef, request: Request, session: AsyncSession
) -> str | None:
    """The id of the event a request names through ``ref``, or None.

    An event id is taken as named, not looked up: no grant can exist on an
    event that does not, and the handler answers 404 for it. A session or
    queue entry that does not exist, or a session with no event, resolves
    to None.
    """
    body = await _json_body(request) if ref.source == "body" else None
    value = ref_value(ref, request.path_params, body)
    if value is None or ref.kind == "event":
        return value
    if ref.kind == "session":
        query = select(FloorSession.event_id).where(FloorSession.id == value)
    else:
        query = (
            select(FloorSession.event_id)
            .join(QueueEntry, QueueEntry.session_id == FloorSession.id)
            .where(QueueEntry.id == value)
        )
    event_id: str | None = (await session.execute(query)).scalar_one_or_none()
    return event_id


# ---------------------------------------------------------------------------
# Authorize + audit
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Caller:
    """Who a scoped request is from, once it has been allowed."""

    principal: Principal
    subject: VerifiedSubject
    # Set by require_event_scope: the event the request was decided for,
    # when it named one that resolved.
    event_id: str | None = None

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
        return await _authorize_request(request, authorization, session, scope, None)

    return _dependency


def require_event_scope(scope: str, event: EventRef):
    """Build a FastAPI dependency enforcing one scope on the event a request names.

    ``require_scope``'s contract, with the request's event as the decision's
    resource: ``event`` says where the request names it. The caller is
    allowed when a role they hold carries the scope (anywhere), or when they
    hold an explicit grant of it on ``event:<event_id>`` (that event only).
    An event that cannot be resolved (not named, or an unknown session or
    queue entry) leaves the decision to roles alone; the handler then
    answers its own 404. The audit record names the resource.
    """

    async def _dependency(
        request: Request,
        authorization: str | None = Header(default=None, alias="Authorization"),
        session: AsyncSession = Depends(get_db_session),
    ) -> Caller:
        return await _authorize_request(request, authorization, session, scope, event)

    return _dependency


async def _authorize_request(
    request: Request,
    authorization: str | None,
    session: AsyncSession,
    scope: str,
    event: EventRef | None,
) -> Caller:
    """Verify, resolve, authorize and emit audit, for one scoped request."""
    subject = await verify_bearer(authorization)
    store = _store(session)
    principal = await store.resolve(subject)
    event_id = (
        await resolve_event_id(event, request, session) if event is not None else None
    )
    resource = event_resource(event_id) if event_id is not None else None
    grants = (
        await store.load_explicit_grants(principal.id)
        if resource is not None and principal is not None
        else None
    )
    decision = decide(
        principal,
        scope,
        await store.load_roles(),
        resource=resource,
        explicit_grants=grants,
    )

    await SqlAlchemyAuditSink(session).emit_audit(
        new_audit_event(
            enforcement_point=ENFORCEMENT_POINT,
            scope=scope,
            allowed=decision.allowed,
            reason=decision.reason,
            principal=principal,
            subject=subject,
            resource=resource,
            request_id=request.headers.get("X-Request-Id"),
        )
    )

    if principal is None:
        # deejaytools-api's answer for a valid token with no users row.
        raise user_not_synced()
    # The actor the Discord change feed names (services/notifications):
    # what the principal already holds, so no extra query.
    request.state.caller = caller_name(principal)
    if not decision.allowed:
        raise forbidden()
    return Caller(principal=principal, subject=subject, event_id=event_id)


def caller_name(principal: Principal) -> str:
    """Who the caller is, for the Discord feed (``request.state.caller``).

    The principal's subject — the Clerk user id, their ``users.id`` — and
    never its display name or email: provisioning sets the display name to
    the email, and the feed's channels are shared with the whole fleet. An
    operator who needs the person looks the id up behind auth.
    """
    return principal.subject


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
# Acting for another user (ADR-0008, "Acting on another user's behalf")
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


# ---------------------------------------------------------------------------
# Staff grants and revokes (ADR-0012)
# ---------------------------------------------------------------------------


async def load_roles(session: AsyncSession) -> dict[str, Role]:
    """The role table, as the decisions here see it."""
    return await _store(session).load_roles()


async def authorize_staff_change(
    caller: Caller,
    *,
    event_id: str,
    user_id: str,
    role: str,
    session: AsyncSession,
    request: Request,
) -> None:
    """The second decision a staff grant or revoke makes, on the change itself.

    The route has allowed ``deejaytools.staff.write`` on the event. This
    decides the particular change, through the same authorize-and-audit
    path: granting or revoking ``event-manager`` needs the scope through a
    role (an admin's), not through a grant on the event, so an event
    manager cannot make another. The audit record's resource names the
    change: ``event:<event_id>/staff/<user_id>/<role>``. A deny is 403
    FORBIDDEN, as for any missing scope. Call it before any write.
    """
    store = _store(session)
    roles = await store.load_roles()
    if role == EVENT_MANAGER:
        decision = decide(caller.principal, STAFF_WRITE_SCOPE, roles)
    else:
        decision = decide(
            caller.principal,
            STAFF_WRITE_SCOPE,
            roles,
            resource=event_resource(event_id),
            explicit_grants=await store.load_explicit_grants(caller.principal.id),
        )
    await SqlAlchemyAuditSink(session).emit_audit(
        new_audit_event(
            enforcement_point=ENFORCEMENT_POINT,
            scope=STAFF_WRITE_SCOPE,
            allowed=decision.allowed,
            reason=decision.reason,
            principal=caller.principal,
            subject=caller.subject,
            resource=f"{event_resource(event_id)}/staff/{user_id}/{role}",
            request_id=request.headers.get("X-Request-Id"),
        )
    )
    if not decision.allowed:
        raise forbidden()
