"""Authentication and authorization: the identity library, on deejaytools-api's answers."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable

import asyncpg
import httpx
import pytest
import respx
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import Depends, FastAPI

from api_deejaytools.auth import Caller, require_scope
from api_deejaytools.errors import install_error_handlers, success
from tests.integration.conftest import (
    STAND_IN_CLERK,
    TEST_ISSUER,
    TEST_JWKS_URL,
    Clerk,
    bearer,
)

UNAUTHORIZED = {"error": {"code": "UNAUTHORIZED", "message": "Authentication required"}}
NOT_SYNCED = {
    "error": {"code": "USER_NOT_SYNCED", "message": "Call POST /v1/auth/sync first"}
}
FORBIDDEN = {"error": {"code": "FORBIDDEN", "message": "Admin access required"}}
JSON = {"Content-Type": "application/json"}


async def _sync(client: httpx.AsyncClient, token: str, **body) -> httpx.Response:
    return await client.post(
        "/v1/auth/sync",
        json={"email": "ada@example.com", **body},
        headers=bearer(token),
    )


async def _roles(db: asyncpg.Connection, subject: str) -> set[str]:
    rows = await db.fetch(
        "SELECT r.role_name FROM identity_principal_roles r "
        "JOIN identity_principals p ON p.id = r.principal_id WHERE p.subject = $1",
        subject,
    )
    return {r["role_name"] for r in rows}


# ---------------------------------------------------------------------------
# Verify
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "header",
    [
        None,
        "",
        "Bearer",
        "Bearer ",
        "bearer {token}",  # the scheme is case-sensitive, as in deejaytools-api
        "Token {token}",
        "Bearer not-a-jwt",
    ],
)
async def test_bad_credentials_are_401(
    client: httpx.AsyncClient, clerk: Clerk, header: str | None
) -> None:
    headers = {}
    if header is not None:
        headers["Authorization"] = header.format(token=clerk.token("user_x"))
    res = await client.get("/v1/auth/me", headers=headers)
    assert res.status_code == 401
    assert res.json() == UNAUTHORIZED


async def test_expired_token_is_401(client: httpx.AsyncClient, clerk: Clerk) -> None:
    res = await client.get(
        "/v1/auth/me", headers=bearer(clerk.token("user_x", expires_in=-60))
    )
    assert res.status_code == 401
    assert res.json() == UNAUTHORIZED


async def test_untrusted_issuer_is_401(client: httpx.AsyncClient, clerk: Clerk) -> None:
    token = clerk.token("user_x", iss="https://clerk.kaianolevine.com")
    res = await client.get("/v1/auth/me", headers=bearer(token))
    assert res.status_code == 401
    assert res.json() == UNAUTHORIZED


async def test_token_signed_by_another_key_is_401(
    client: httpx.AsyncClient, clerk: Clerk
) -> None:
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    res = await client.get(
        "/v1/auth/me", headers=bearer(clerk.token("user_x", key=other))
    )
    assert res.status_code == 401


async def test_unreachable_jwks_is_503_and_was_asked(
    client: httpx.AsyncClient,
) -> None:
    """An outage leaves the credential unjudged: 503, so the app keeps the session."""
    # Not the clerk fixture: its JWKS route would answer instead of this one.
    from api_deejaytools import auth

    auth._verifier.cache_clear()  # drop the cached JWKS
    with respx.mock() as router:
        route = router.get(TEST_JWKS_URL).mock(return_value=httpx.Response(503))
        res = await client.get(
            "/v1/auth/me", headers=bearer(STAND_IN_CLERK.token("user_x"))
        )
    auth._verifier.cache_clear()
    assert route.called
    assert res.status_code == 503
    assert res.json()["error"]["code"] == "AUTH_UNAVAILABLE"


# ---------------------------------------------------------------------------
# POST /v1/auth/sync
# ---------------------------------------------------------------------------


async def test_sync_creates_user_and_principal(
    client: httpx.AsyncClient,
    clerk: Clerk,
    db: asyncpg.Connection,
    new_user_id: Callable[[], str],
) -> None:
    uid = new_user_id()
    res = await _sync(client, clerk.token(uid), firstName="Ada", lastName="Lovelace")

    assert res.status_code == 200
    body = res.json()
    assert body["meta"] == {"version": "v1"}
    data = body["data"]
    assert set(data) == {
        "id",
        "email",
        "display_name",
        "first_name",
        "last_name",
        "role",
        "created_at",
        "updated_at",
    }
    assert data["id"] == uid
    assert data["email"] == "ada@example.com"
    assert (data["first_name"], data["last_name"], data["display_name"]) == (
        "Ada",
        "Lovelace",
        None,
    )
    assert data["role"] == "user"
    assert (
        isinstance(data["created_at"], int) and data["created_at"] > 1_700_000_000_000
    )

    row = await db.fetchrow("SELECT * FROM users WHERE id = $1", uid)
    assert row is not None
    assert await _roles(db, uid) == {"deejaytools-dancer"}
    issuer = await db.fetchrow("SELECT issuer, jwks_url FROM identity_issuers")
    assert (issuer["issuer"], issuer["jwks_url"]) == (TEST_ISSUER, TEST_JWKS_URL)


async def test_sync_again_updates_email_but_not_names(
    client: httpx.AsyncClient, clerk: Clerk, new_user_id: Callable[[], str]
) -> None:
    uid = new_user_id()
    first = (await _sync(client, clerk.token(uid), firstName="Ada")).json()["data"]
    res = await _sync(
        client, clerk.token(uid), email="ada@new.example.com", firstName="Changed"
    )

    data = res.json()["data"]
    assert res.status_code == 200
    assert data["email"] == "ada@new.example.com"
    assert data["first_name"] == "Ada"
    assert data["created_at"] == first["created_at"]
    assert data["updated_at"] >= first["updated_at"]


async def test_sync_email_owned_by_another_sign_in_is_409(
    client: httpx.AsyncClient, clerk: Clerk, new_user_id: Callable[[], str]
) -> None:
    assert (await _sync(client, clerk.token(new_user_id()))).status_code == 200

    res = await _sync(client, clerk.token(new_user_id()))

    assert res.status_code == 409
    assert res.json()["error"]["code"] == "EMAIL_BELONGS_TO_ANOTHER_ACCOUNT"


async def test_sync_validates_body_before_token(client: httpx.AsyncClient) -> None:
    res = await client.post("/v1/auth/sync", json={"email": "not-an-email"})
    assert res.status_code == 400
    assert res.json()["error"]["code"] == "VALIDATION_ERROR"


async def test_sync_rejects_null_optional_fields(
    client: httpx.AsyncClient, clerk: Clerk
) -> None:
    res = await _sync(client, clerk.token("user_x"), firstName=None)
    assert res.status_code == 400
    assert res.json()["error"]["code"] == "VALIDATION_ERROR"


async def test_sync_without_token_is_401(client: httpx.AsyncClient) -> None:
    res = await client.post("/v1/auth/sync", json={"email": "ada@example.com"})
    assert res.status_code == 401
    assert res.json() == UNAUTHORIZED


async def test_sync_never_grants_admin_on_first_provision(
    client: httpx.AsyncClient,
    clerk: Clerk,
    db: asyncpg.Connection,
    new_user_id: Callable[[], str],
) -> None:
    """A users row with no principal yet is provisioned as a dancer only."""
    uid = new_user_id()
    await db.execute(
        "INSERT INTO users (id, email, created_at, updated_at) "
        "VALUES ($1, 'ada@example.com', 1, 1)",
        uid,
    )

    res = await _sync(client, clerk.token(uid))

    assert res.json()["data"]["role"] == "user"
    assert await _roles(db, uid) == {"deejaytools-dancer"}


# ---------------------------------------------------------------------------
# GET /v1/auth/me
# ---------------------------------------------------------------------------


async def test_me_before_sync_is_user_not_synced(
    client: httpx.AsyncClient, clerk: Clerk
) -> None:
    res = await client.get("/v1/auth/me", headers=bearer(clerk.token("user_new")))
    assert res.status_code == 401
    assert res.json() == NOT_SYNCED


async def test_me_returns_the_synced_record(
    client: httpx.AsyncClient, clerk: Clerk, new_user_id: Callable[[], str]
) -> None:
    uid = new_user_id()
    synced = (await _sync(client, clerk.token(uid), firstName="Ada")).json()

    res = await client.get("/v1/auth/me", headers=bearer(clerk.token(uid)))

    assert res.status_code == 200
    # /me adds the caller's per-event staff roles (ADR-0012), none here.
    assert res.json() == {**synced, "data": {**synced["data"], "event_roles": []}}


async def test_me_role_comes_from_the_grant(
    client: httpx.AsyncClient,
    clerk: Clerk,
    db: asyncpg.Connection,
    new_user_id: Callable[[], str],
) -> None:
    uid = new_user_id()
    await _sync(client, clerk.token(uid))
    await db.execute(
        "INSERT INTO identity_principal_roles (principal_id, role_name) "
        "SELECT id, 'deejaytools-admin' FROM identity_principals WHERE subject = $1",
        uid,
    )

    res = await client.get("/v1/auth/me", headers=bearer(clerk.token(uid)))

    assert res.json()["data"]["role"] == "admin"


async def test_me_with_users_row_but_no_principal_is_a_user(
    client: httpx.AsyncClient,
    clerk: Clerk,
    db: asyncpg.Connection,
    new_user_id: Callable[[], str],
) -> None:
    """A users row the backfill has not reached yet."""
    uid = new_user_id()
    await db.execute(
        "INSERT INTO users (id, email, created_at, updated_at) "
        "VALUES ($1, 'ada@example.com', 1, 1)",
        uid,
    )

    res = await client.get("/v1/auth/me", headers=bearer(clerk.token(uid)))

    assert res.status_code == 200
    assert res.json()["data"]["role"] == "user"


# ---------------------------------------------------------------------------
# require_scope
# ---------------------------------------------------------------------------


@pytest.fixture
async def scoped(db: asyncpg.Connection) -> AsyncIterator[httpx.AsyncClient]:
    """A client on an app with one dancer-scoped and one admin-scoped route.

    Separate from the real app so no route exists only for tests. Its
    POST /v1/auth/sync is the real one.
    """
    from api_deejaytools.routers import auth as auth_router

    app = FastAPI()
    install_error_handlers(app)
    app.include_router(auth_router.router)

    @app.get("/dancer")
    async def dancer(
        caller: Caller = Depends(require_scope("deejaytools.songs.read")),
    ) -> dict:
        return success({"user_id": caller.user_id})

    @app.get("/admin")
    async def admin(
        caller: Caller = Depends(require_scope("deejaytools.users.read")),
    ) -> dict:
        return success({"user_id": caller.user_id})

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        yield c


async def _audit(db: asyncpg.Connection) -> list[asyncpg.Record]:
    return await db.fetch(
        "SELECT enforcement_point, subject, scope, allowed, reason "
        "FROM identity_audit_events ORDER BY occurred_at"
    )


async def test_scope_allowed_is_audited(
    scoped: httpx.AsyncClient,
    clerk: Clerk,
    db: asyncpg.Connection,
    new_user_id: Callable[[], str],
) -> None:
    uid = new_user_id()
    await _sync(scoped, clerk.token(uid))

    res = await scoped.get("/dancer", headers=bearer(clerk.token(uid)))

    assert res.status_code == 200
    assert res.json()["data"] == {"user_id": uid}
    [event] = await _audit(db)
    assert dict(event) == {
        "enforcement_point": "api-deejaytools",
        "subject": uid,
        "scope": "deejaytools.songs.read",
        "allowed": True,
        "reason": "granted_by_role",
    }


async def test_missing_scope_is_403_and_audited_with_the_real_scope(
    scoped: httpx.AsyncClient,
    clerk: Clerk,
    db: asyncpg.Connection,
    new_user_id: Callable[[], str],
) -> None:
    uid = new_user_id()
    await _sync(scoped, clerk.token(uid))

    res = await scoped.get("/admin", headers=bearer(clerk.token(uid)))

    assert res.status_code == 403
    assert res.json() == FORBIDDEN
    [event] = await _audit(db)
    assert (event["scope"], event["allowed"], event["reason"]) == (
        "deejaytools.users.read",
        False,
        "no_matching_scope",
    )


async def test_admin_grant_allows_admin_scope(
    scoped: httpx.AsyncClient,
    clerk: Clerk,
    db: asyncpg.Connection,
    new_user_id: Callable[[], str],
) -> None:
    uid = new_user_id()
    await _sync(scoped, clerk.token(uid))
    await db.execute(
        "INSERT INTO identity_principal_roles (principal_id, role_name) "
        "SELECT id, 'deejaytools-admin' FROM identity_principals WHERE subject = $1",
        uid,
    )

    res = await scoped.get("/admin", headers=bearer(clerk.token(uid)))

    assert res.status_code == 200


async def test_no_principal_is_user_not_synced_and_audited(
    scoped: httpx.AsyncClient, clerk: Clerk, db: asyncpg.Connection
) -> None:
    res = await scoped.get("/dancer", headers=bearer(clerk.token("user_unknown")))

    assert res.status_code == 401
    assert res.json() == NOT_SYNCED
    [event] = await _audit(db)
    assert (event["subject"], event["allowed"], event["reason"]) == (
        "user_unknown",
        False,
        "principal_not_found",
    )


async def test_suspended_principal_is_403(
    scoped: httpx.AsyncClient,
    clerk: Clerk,
    db: asyncpg.Connection,
    new_user_id: Callable[[], str],
) -> None:
    uid = new_user_id()
    await _sync(scoped, clerk.token(uid))
    await db.execute(
        "UPDATE identity_principals SET status = 'suspended' WHERE subject = $1", uid
    )

    res = await scoped.get("/dancer", headers=bearer(clerk.token(uid)))

    assert res.status_code == 403
    assert res.json() == FORBIDDEN


async def test_no_credential_is_401_without_an_audit_row(
    scoped: httpx.AsyncClient, db: asyncpg.Connection
) -> None:
    res = await scoped.get("/dancer")

    assert res.status_code == 401
    assert res.json() == UNAUTHORIZED
    assert await _audit(db) == []


async def test_unconfigured_issuer_rejects_every_token(
    client: httpx.AsyncClient, clerk: Clerk, monkeypatch: pytest.MonkeyPatch
) -> None:
    """As deejaytools-api: no issuer configured means every request is 401."""
    from api_deejaytools.config import get_settings

    monkeypatch.setattr(get_settings(), "DEEJAYTOOLS_CLERK_ISSUER", None)
    res = await client.get("/v1/auth/me", headers=bearer(clerk.token("user_x")))
    assert res.status_code == 401
    assert res.json() == UNAUTHORIZED


async def test_patch_me_trims_and_saves_names(
    client: httpx.AsyncClient, person: Callable
) -> None:
    ada = await person("ada")

    res = await client.patch(
        "/v1/auth/me",
        json={"firstName": "  Augusta ", "lastName": "King"},
        headers=ada.headers,
    )

    assert res.status_code == 200
    data = res.json()["data"]
    assert (data["first_name"], data["last_name"], data["role"]) == (
        "Augusta",
        "King",
        "user",
    )
    for body in ({"firstName": "   ", "lastName": "K"}, {"firstName": "A"}):
        bad = await client.patch("/v1/auth/me", json=body, headers=ada.headers)
        assert bad.json()["error"]["code"] == "VALIDATION_ERROR"


async def test_patch_me_needs_a_principal(
    client: httpx.AsyncClient, clerk: Clerk
) -> None:
    res = await client.patch(
        "/v1/auth/me",
        json={"firstName": "A", "lastName": "B"},
        headers=bearer(clerk.token("user_never_synced")),
    )
    assert res.status_code == 401
    assert res.json() == NOT_SYNCED


async def test_every_route_a_dancer_lacks_the_scope_for_is_403(
    client: httpx.AsyncClient,
    clerk: Clerk,
    db: asyncpg.Connection,
    new_user_id: Callable[[], str],
) -> None:
    """A newly signed-in user holds only the roles sync grants. Every scoped
    route whose scope those roles lack refuses them with the admin 403, before
    looking at the path, query or body. The granted scopes come from the
    seeded role-scope table, not a list here; every /v1/admin route must be
    among the refused, so an admin route given a dancer's scope fails too.
    """
    import importlib
    import inspect
    import pkgutil

    from fastapi.routing import APIRoute

    import api_deejaytools.routers as routers_pkg

    def route_scope(dependant: object) -> str | None:
        for dep in dependant.dependencies:  # type: ignore[attr-defined]
            try:
                scope = inspect.getclosurevars(dep.call).nonlocals.get("scope")
            except TypeError:
                scope = None
            if isinstance(scope, str):
                return scope
            found = route_scope(dep)
            if found:
                return found
        return None

    uid = new_user_id()
    assert (await _sync(client, clerk.token(uid))).status_code == 200
    granted = {
        r["scope"]
        for r in await db.fetch(
            "SELECT rs.scope FROM identity_role_scopes rs "
            "JOIN identity_principal_roles pr ON pr.role_name = rs.role_name "
            "JOIN identity_principals p ON p.id = pr.principal_id "
            "WHERE p.subject = $1",
            uid,
        )
    }
    assert granted  # not vacuous: sync grants the dancer role

    scoped_routes = [
        (route, scope)
        for info in pkgutil.iter_modules(routers_pkg.__path__)
        for route in importlib.import_module(
            f"api_deejaytools.routers.{info.name}"
        ).router.routes
        if isinstance(route, APIRoute)
        and (scope := route_scope(route.dependant)) is not None
    ]
    reachable_admin = [
        f"{route.path} ({scope})"
        for route, scope in scoped_routes
        if route.path.startswith("/v1/admin") and scope in granted
    ]
    assert reachable_admin == []
    routes = [(route, scope) for route, scope in scoped_routes if scope not in granted]
    assert len(routes) > 10  # not vacuous: the admin and manager routes

    headers = {**JSON, **bearer(clerk.token(uid))}
    wrong = []
    for route, scope in routes:
        path = route.path_format.replace("{", "").replace("}", "")
        for method in route.methods:
            res = await client.request(
                method,
                path + "?limit=nope&event_id=a&event_id=b",
                content=b'{"status": 5, "divisions": "x"}',
                headers=headers,
            )
            if res.status_code != 403 or res.json() != FORBIDDEN:
                wrong.append(f"{method} {route.path} ({scope}): {res.status_code}")
    assert wrong == []
