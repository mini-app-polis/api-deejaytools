"""scripts/backfill_principals.py: every users row becomes a principal (ADR-007)."""

from __future__ import annotations

import asyncpg
import pytest

from tests.conftest import TEST_ISSUER, TEST_JWKS_URL, load_script

backfill_mod = load_script("backfill_principals")


async def _add_user(db: asyncpg.Connection, uid: str, role: str = "user") -> None:
    await db.execute(
        "INSERT INTO users (id, email, role, created_at, updated_at) "
        "VALUES ($1, $2, $3, 1710000000000, 1710000000000)",
        uid,
        f"{uid}@example.com",
        role,
    )


async def _grants(db: asyncpg.Connection) -> dict[str, set[str]]:
    rows = await db.fetch(
        "SELECT p.subject, r.role_name, r.granted_by FROM identity_principal_roles r "
        "JOIN identity_principals p ON p.id = r.principal_id"
    )
    out: dict[str, set[str]] = {}
    for r in rows:
        assert r["granted_by"] == "migration_backfill"
        out.setdefault(r["subject"], set()).add(r["role_name"])
    return out


async def test_backfill_provisions_every_user(db: asyncpg.Connection) -> None:
    await _add_user(db, "user_dancer")
    await _add_user(db, "user_admin", role="admin")

    counts = await backfill_mod.backfill(db, TEST_ISSUER, TEST_JWKS_URL)

    assert counts == {"principals": 2, "grants": 3}
    assert await _grants(db) == {
        "user_dancer": {"deejaytools-dancer"},
        "user_admin": {"deejaytools-dancer", "deejaytools-admin"},
    }
    issuer = await db.fetchrow("SELECT * FROM identity_issuers")
    assert (issuer["issuer"], issuer["jwks_url"]) == (TEST_ISSUER, TEST_JWKS_URL)
    principal = await db.fetchrow(
        "SELECT kind, issuer, email, created_at FROM identity_principals "
        "WHERE subject = 'user_dancer'"
    )
    assert principal["kind"] == "human"
    assert principal["issuer"] == TEST_ISSUER
    assert principal["email"] == "user_dancer@example.com"
    assert int(principal["created_at"].timestamp() * 1000) == 1710000000000


async def test_backfill_is_idempotent(db: asyncpg.Connection) -> None:
    await _add_user(db, "user_a", role="admin")
    await backfill_mod.backfill(db, TEST_ISSUER, TEST_JWKS_URL)

    assert await backfill_mod.backfill(db, TEST_ISSUER, TEST_JWKS_URL) == {
        "principals": 0,
        "grants": 0,
    }


async def test_backfill_picks_up_users_added_later(db: asyncpg.Connection) -> None:
    """The rollback case: a user who signed up through deejaytools-api meanwhile."""
    await _add_user(db, "user_before")
    await backfill_mod.backfill(db, TEST_ISSUER, TEST_JWKS_URL)
    await _add_user(db, "user_during_rollback")

    counts = await backfill_mod.backfill(db, TEST_ISSUER, TEST_JWKS_URL)

    assert counts == {"principals": 1, "grants": 1}
    assert "user_during_rollback" in await _grants(db)


def test_main_refuses_without_issuer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DEEJAYTOOLS_CLERK_ISSUER", raising=False)
    assert backfill_mod.main() == 2
