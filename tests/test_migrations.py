"""The migration runner, the baseline adoption path, and migration 002's seed."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from urllib.parse import urlparse, urlunparse

import asyncpg
import pytest

from tests.conftest import ROOT, TEST_DATABASE_URL, load_script

runner = load_script("apply_migrations")

SCRATCH_DB = "deejaytools_runner_test"


def _with_db(url: str, name: str) -> str:
    return urlunparse(urlparse(url)._replace(path=f"/{name}"))


@pytest.fixture
async def scratch() -> AsyncIterator[str]:
    """An empty database of its own, for runs that must start from nothing."""
    admin = await asyncpg.connect(_with_db(TEST_DATABASE_URL, "postgres"))
    try:
        await admin.execute(f'DROP DATABASE IF EXISTS "{SCRATCH_DB}"')
        await admin.execute(f'CREATE DATABASE "{SCRATCH_DB}"')
        yield _with_db(TEST_DATABASE_URL, SCRATCH_DB)
    finally:
        await admin.execute(f'DROP DATABASE IF EXISTS "{SCRATCH_DB}" WITH (FORCE)')
        await admin.close()


async def _build_like_production(url: str) -> None:
    """Apply deejaytools-api's drizzle history, as production has it."""
    drizzle = ROOT / "conformance" / "drizzle"
    journal = json.loads((drizzle / "meta" / "_journal.json").read_text())
    conn = await asyncpg.connect(url)
    try:
        for entry in sorted(journal["entries"], key=lambda e: e["idx"]):
            async with conn.transaction():
                await conn.execute((drizzle / f"{entry['tag']}.sql").read_text())
    finally:
        await conn.close()


async def test_test_database_is_built_from_every_migration(database: str) -> None:
    conn = await asyncpg.connect(database)
    try:
        applied = [
            r["filename"]
            for r in await conn.fetch(
                "SELECT filename FROM schema_migrations ORDER BY filename"
            )
        ]
    finally:
        await conn.close()
    files = sorted(p.name for p in (ROOT / "migrations").glob("[0-9]*_*.sql"))
    assert applied == files
    assert applied[:2] == ["001_baseline.sql", "002_identity_store.sql"]


async def test_rerun_applies_nothing(database: str) -> None:
    conn = await asyncpg.connect(database)
    try:
        before = await conn.fetchval("SELECT count(*) FROM schema_migrations")
        assert await runner._run(database, False, set()) == 0
        assert await conn.fetchval("SELECT count(*) FROM schema_migrations") == before
    finally:
        await conn.close()


async def test_production_adopts_baseline_without_running_it(scratch: str) -> None:
    """ADR-008 point 3: bootstrap marks 001 applied and runs everything after it."""
    await _build_like_production(scratch)
    exclude = {
        p.name
        for p in (ROOT / "migrations").glob("[0-9]*_*.sql")
        if p.name != "001_baseline.sql"
    }

    assert await runner._run(scratch, True, exclude) == 0

    conn = await asyncpg.connect(scratch)
    try:
        rows = {
            r["filename"]: r["duration_ms"]
            for r in await conn.fetch(
                "SELECT filename, duration_ms FROM schema_migrations"
            )
        }
        identity_tables = await conn.fetchval(
            "SELECT count(*) FROM pg_tables WHERE tablename LIKE 'identity_%'"
        )
        users_columns = await conn.fetchval(
            "SELECT count(*) FROM information_schema.columns WHERE table_name = 'users'"
        )
        issuers = await conn.fetchval("SELECT count(*) FROM identity_issuers")
    finally:
        await conn.close()
    assert rows["001_baseline.sql"] is None  # marked, never run
    assert rows["002_identity_store.sql"] is not None  # run
    assert identity_tables == 7
    assert users_columns == 8  # untouched
    # The issuer is per environment: the backfill and sync write it, not 002.
    assert issuers == 0


async def test_bootstrap_without_exclude_list_is_refused(scratch: str) -> None:
    with pytest.raises(RuntimeError, match="BOOTSTRAP_EXCLUDE"):
        await runner._run(scratch, True, set())
    conn = await asyncpg.connect(scratch)
    try:
        marked = await conn.fetchval("SELECT count(*) FROM schema_migrations")
    finally:
        await conn.close()
    assert marked == 0


async def test_bootstrap_with_unknown_exclude_is_refused(scratch: str) -> None:
    with pytest.raises(RuntimeError, match="not migrations"):
        await runner._run(scratch, True, {"002_identity_stor.sql"})


def test_no_default_exclude_list() -> None:
    assert runner._parse_bootstrap_exclude(None) == set()
    assert runner._parse_bootstrap_exclude(" a.sql , b.sql ") == {"a.sql", "b.sql"}


async def test_identity_store_seed_matches_adr_007(database: str) -> None:
    conn = await asyncpg.connect(database)
    try:
        scopes = await conn.fetch(
            "SELECT role_name, scope FROM identity_role_scopes ORDER BY scope"
        )
    finally:
        await conn.close()
    by_role: dict[str, set[str]] = {}
    for r in scopes:
        by_role.setdefault(r["role_name"], set()).add(r["scope"])
    assert set(by_role) == {"deejaytools-dancer", "deejaytools-admin"}
    assert len(by_role["deejaytools-dancer"]) == 14
    assert len(by_role["deejaytools-admin"]) == 14
    assert not by_role["deejaytools-dancer"] & by_role["deejaytools-admin"]
    assert "deejaytools.delegation.act" in by_role["deejaytools-admin"]
