"""scripts/backfill_principals.py: every users row becomes a principal (ADR-0008)."""

from __future__ import annotations

import asyncpg
import pytest

from tests.integration.conftest import TEST_ISSUER, TEST_JWKS_URL, load_script

backfill_mod = load_script("backfill_principals")


async def _add_user(db: asyncpg.Connection, uid: str) -> None:
    await db.execute(
        "INSERT INTO users (id, email, created_at, updated_at) "
        "VALUES ($1, $2, 1710000000000, 1710000000000)",
        uid,
        f"{uid}@example.com",
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
    await _add_user(db, "user_other")

    counts = await backfill_mod.backfill(db, TEST_ISSUER, TEST_JWKS_URL)

    assert counts == {"principals": 2, "grants": 2}
    assert await _grants(db) == {
        "user_dancer": {"deejaytools-dancer"},
        "user_other": {"deejaytools-dancer"},
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
    await _add_user(db, "user_a")
    await backfill_mod.backfill(db, TEST_ISSUER, TEST_JWKS_URL)

    assert await backfill_mod.backfill(db, TEST_ISSUER, TEST_JWKS_URL) == {
        "principals": 0,
        "grants": 0,
    }


async def test_backfill_picks_up_users_added_later(db: asyncpg.Connection) -> None:
    """Someone with a users row but no principal yet is provisioned next deploy."""
    await _add_user(db, "user_before")
    await backfill_mod.backfill(db, TEST_ISSUER, TEST_JWKS_URL)
    await _add_user(db, "user_after")

    counts = await backfill_mod.backfill(db, TEST_ISSUER, TEST_JWKS_URL)

    assert counts == {"principals": 1, "grants": 1}
    assert "user_after" in await _grants(db)


async def test_backfill_leaves_existing_grants_alone(
    db: asyncpg.Connection,
) -> None:
    """An admin grant made outside the backfill is neither copied nor revoked."""
    await _add_user(db, "user_admin")
    await _add_user(db, "user_dancer")
    await backfill_mod.backfill(db, TEST_ISSUER, TEST_JWKS_URL)
    await db.execute(
        "INSERT INTO identity_principal_roles (principal_id, role_name, granted_by) "
        "SELECT id, 'deejaytools-admin', 'user_granter' FROM identity_principals "
        "WHERE subject = 'user_admin'"
    )

    counts = await backfill_mod.backfill(db, TEST_ISSUER, TEST_JWKS_URL)

    assert counts == {"principals": 0, "grants": 0}
    rows = await db.fetch(
        "SELECT p.subject, r.role_name, r.granted_by FROM identity_principal_roles r "
        "JOIN identity_principals p ON p.id = r.principal_id "
        "ORDER BY p.subject, r.role_name"
    )
    assert [tuple(r) for r in rows] == [
        ("user_admin", "deejaytools-admin", "user_granter"),
        ("user_admin", "deejaytools-dancer", "migration_backfill"),
        ("user_dancer", "deejaytools-dancer", "migration_backfill"),
    ]


def test_main_refuses_without_issuer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DEEJAYTOOLS_CLERK_ISSUER", raising=False)
    assert backfill_mod.main() == 2
