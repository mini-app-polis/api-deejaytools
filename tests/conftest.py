"""Test fixtures: real Postgres, a stand-in Clerk, and an HTTP client on the app.

Tests run against Postgres, never SQLite (deejaytools-api ADR-009): the
schema relies on Postgres enums and check constraints, and the queue and job
queue on row locks. The database is built once per run the way production's
history is: by scripts/apply_migrations.py from migrations/, so the baseline
is exercised on every run (ADR-008 point 7).

TEST_DATABASE_URL names the database. It must be local and end in _test:
the fixtures drop its public schema.
"""

from __future__ import annotations

import importlib.util
import json
import os
import time
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import asyncpg
import httpx
import jwt
import pytest
import respx
from cryptography.hazmat.primitives.asymmetric import rsa

ROOT = Path(__file__).resolve().parent.parent

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/deejaytools_test",
)
TEST_ISSUER = "https://clerk.test.local"
TEST_JWKS_URL = f"{TEST_ISSUER}/.well-known/jwks.json"


def _guard(url: str) -> None:
    parsed = urlparse(url)
    if parsed.hostname not in {"localhost", "127.0.0.1"} or not parsed.path.endswith(
        "_test"
    ):
        raise RuntimeError(
            f"Refusing to run tests against {parsed.hostname}{parsed.path}: "
            "TEST_DATABASE_URL must be local and its database named *_test."
        )


_guard(TEST_DATABASE_URL)

# Settings is built at app import; set what it reads first.
os.environ["DEEJAYTOOLS_DATABASE_URL"] = TEST_DATABASE_URL
os.environ["DEEJAYTOOLS_CLERK_ISSUER"] = TEST_ISSUER
os.environ["DEEJAYTOOLS_CLERK_JWKS_URL"] = TEST_JWKS_URL
os.environ["DEEJAYTOOLS_CORS_ORIGINS"] = "http://localhost:5173"
os.environ["ENVIRONMENT"] = "test"
os.environ.pop("SENTRY_DSN_API_DEEJAYTOOLS", None)

from api_deejaytools.main import app  # noqa: E402

# Seed rows migration 002 writes; spared by the per-test reset, as the
# conformance harness spares them (ADR-009).
SPARED_TABLES = {"schema_migrations", "identity_roles", "identity_role_scopes"}


def load_script(name: str) -> Any:
    """Import a module from scripts/ (they are not a package)."""
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def build_schema(url: str) -> None:
    """Empty the database, then apply migrations/ with the real runner."""
    conn = await asyncpg.connect(url)
    try:
        await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    finally:
        await conn.close()
    runner = load_script("apply_migrations")
    assert await runner._run(url, False, set()) == 0


@pytest.fixture(scope="session")
async def database() -> AsyncIterator[str]:
    """The test database, built from migrations/ once per run."""
    await build_schema(TEST_DATABASE_URL)
    yield TEST_DATABASE_URL


@pytest.fixture
async def db(database: str) -> AsyncIterator[asyncpg.Connection]:
    """A direct connection, after emptying every table but the seed tables."""
    conn = await asyncpg.connect(database)
    tables = [
        r["tablename"]
        for r in await conn.fetch(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
        )
        if r["tablename"] not in SPARED_TABLES
    ]
    quoted = ", ".join(f'"{t}"' for t in tables)
    await conn.execute(f"TRUNCATE {quoted} RESTART IDENTITY CASCADE")
    try:
        yield conn
    finally:
        await conn.close()


class Clerk:
    """A stand-in Clerk instance: one RSA key served as JWKS, and a token minter."""

    def __init__(self) -> None:
        self.kid = "test-key"
        self._key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        public = jwt.algorithms.RSAAlgorithm.to_jwk(self._key.public_key())
        self.jwks = {"keys": [{**json.loads(public), "kid": self.kid, "alg": "RS256"}]}

    def token(
        self,
        sub: str,
        *,
        iss: str = TEST_ISSUER,
        expires_in: int = 300,
        kid: str | None = None,
        key: Any = None,
        **claims: Any,
    ) -> str:
        """A session JWT for ``sub``, signed by this instance unless told otherwise."""
        now = int(time.time())
        payload = {
            "sub": sub,
            "iss": iss,
            "iat": now,
            "exp": now + expires_in,
            **claims,
        }
        return jwt.encode(
            payload,
            key or self._key,
            algorithm="RS256",
            headers={"kid": kid or self.kid},
        )


STAND_IN_CLERK = Clerk()


@pytest.fixture
def clerk() -> Iterator[Clerk]:
    """The stand-in Clerk, with its JWKS URL mocked for the test."""
    with respx.mock(assert_all_called=False) as router:
        router.get(TEST_JWKS_URL).mock(
            return_value=httpx.Response(200, json=STAND_IN_CLERK.jwks)
        )
        yield STAND_IN_CLERK


@pytest.fixture
async def client(db: asyncpg.Connection) -> AsyncIterator[httpx.AsyncClient]:
    """An HTTP client on the app, over a freshly emptied database."""
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def bearer(token: str) -> dict[str, str]:
    """An Authorization header for ``token``."""
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def new_user_id() -> Callable[[], str]:
    """Clerk-shaped user ids, unique per call."""
    return lambda: f"user_{uuid.uuid4().hex[:24]}"


class Person:
    """A synced caller: their users.id and a ready Authorization header."""

    def __init__(self, user_id: str, token: str) -> None:
        self.id = user_id
        self.headers = bearer(token)


@pytest.fixture
def person(
    client: httpx.AsyncClient,
    clerk: Clerk,
    db: asyncpg.Connection,
    new_user_id: Callable[[], str],
) -> Callable[..., Any]:
    """Make a synced person, optionally an admin (granted in the store)."""

    async def make(name: str = "ada", *, admin: bool = False) -> Person:
        uid = new_user_id()
        token = clerk.token(uid)
        res = await client.post(
            "/v1/auth/sync",
            json={"email": f"{name}.{uid}@example.test", "firstName": name.title()},
            headers=bearer(token),
        )
        assert res.status_code == 200, res.text
        if admin:
            await db.execute(
                "INSERT INTO identity_principal_roles (principal_id, role_name) "
                "SELECT id, 'deejaytools-admin' FROM identity_principals "
                "WHERE subject = $1",
                uid,
            )
        return Person(uid, token)

    return make


@pytest.fixture(autouse=True)
def _empty_response_cache() -> None:
    """Each test starts with no cached session or queue reads."""
    from api_deejaytools.cache import response_cache

    response_cache.invalidate_prefix("")
