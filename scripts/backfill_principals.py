"""Make every deejaytools user a principal in the identity store.

The backfill deejaytools-api ADR-007 describes, moved out of migration 002
because it depends on the environment: each environment has its own Clerk
instance, so the issuer the principals belong to comes from settings
(DEEJAYTOOLS_CLERK_ISSUER, DEEJAYTOOLS_CLERK_JWKS_URL), not from a file.

For the configured issuer it:
  - writes the issuer row;
  - creates a `human` principal for every `users` row that has none
    (subject = users.id, already the Clerk `sub`);
  - grants `deejaytools-dancer` to all of them, and `deejaytools-admin`
    to rows with role = 'admin' (granted_by = 'migration_backfill');
  - removes `deejaytools-admin` from principals whose users row says
    anything but 'admin'.

So users.role decides admin access at every deploy. Through this service
the two never disagree (PATCH /v1/admin/users/{id}/role writes both); the
revoke is for changes made through deejaytools-api while traffic was rolled
back, where only users.role moves, so a demotion there is not lost.

Idempotent, so it runs on every deploy, after scripts/apply_migrations.py and before the app. That
is also the re-run ADR-007 asks for after a rollback window: anyone who
signed up through deejaytools-api meanwhile is picked up by the next deploy
(and by their own next sync).

Runs as the runtime role: it writes rows, not schema. Exits non-zero on any
failure, so the deploy fails visibly rather than serving people who cannot
be authorized.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

import asyncpg

logger = logging.getLogger("backfill_principals")

GRANTED_BY = "migration_backfill"

ISSUER_SQL = """
INSERT INTO identity_issuers (issuer, display_name, jwks_url)
VALUES ($1, 'deejaytools (Clerk)', $2)
ON CONFLICT (issuer) DO NOTHING
"""

PRINCIPALS_SQL = """
INSERT INTO identity_principals (kind, issuer, subject, display_name, email, created_at)
SELECT 'human', $1, u.id, u.email, u.email, to_timestamp(u.created_at / 1000.0)
FROM users u
ON CONFLICT (issuer, subject) DO NOTHING
"""

GRANTS_SQL = """
INSERT INTO identity_principal_roles (principal_id, role_name, granted_by)
SELECT p.id, r.role_name, $2
FROM users u
JOIN identity_principals p ON p.issuer = $1 AND p.subject = u.id
CROSS JOIN LATERAL (
  SELECT 'deejaytools-dancer' AS role_name
  UNION ALL
  SELECT 'deejaytools-admin' WHERE u.role = 'admin'
) r
ON CONFLICT (principal_id, role_name) DO NOTHING
"""


REVOKE_SQL = """
DELETE FROM identity_principal_roles r
USING identity_principals p, users u
WHERE r.principal_id = p.id
  AND p.issuer = $1
  AND p.subject = u.id
  AND r.role_name = 'deejaytools-admin'
  AND u.role <> 'admin'
"""


def _normalize_database_url(raw_url: str) -> str:
    """Strip a SQLAlchemy driver suffix asyncpg does not accept."""
    if raw_url.startswith("postgresql+asyncpg://"):
        return raw_url.replace("postgresql+asyncpg://", "postgresql://", 1)
    return raw_url


async def backfill(
    conn: asyncpg.Connection, issuer: str, jwks_url: str
) -> dict[str, int]:
    """Apply the backfill in one transaction. Returns rows inserted per step."""
    async with conn.transaction():
        await conn.execute(ISSUER_SQL, issuer, jwks_url)
        principals = await conn.execute(PRINCIPALS_SQL, issuer)
        grants = await conn.execute(GRANTS_SQL, issuer, GRANTED_BY)
        revoked = await conn.execute(REVOKE_SQL, issuer)
    # asyncpg returns the command tag, e.g. "INSERT 0 12" or "DELETE 1".
    return {
        "principals": int(principals.split()[-1]),
        "grants": int(grants.split()[-1]),
        "revoked": int(revoked.split()[-1]),
    }


async def _run(database_url: str, issuer: str, jwks_url: str) -> None:
    conn = await asyncpg.connect(database_url)
    try:
        counts = await backfill(conn, issuer, jwks_url)
    finally:
        await conn.close()
    logger.info(
        "Backfill for %s: %d principal(s), %d grant(s) added, %d admin grant(s) revoked.",
        issuer,
        counts["principals"],
        counts["grants"],
        counts["revoked"],
    )


def main() -> int:
    """Entry point. Reads the database and issuer from the environment."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    database_url = os.environ.get("DEEJAYTOOLS_DATABASE_URL")
    issuer = os.environ.get("DEEJAYTOOLS_CLERK_ISSUER")
    jwks_url = os.environ.get("DEEJAYTOOLS_CLERK_JWKS_URL")
    missing = [
        name
        for name, value in (
            ("DEEJAYTOOLS_DATABASE_URL", database_url),
            ("DEEJAYTOOLS_CLERK_ISSUER", issuer),
            ("DEEJAYTOOLS_CLERK_JWKS_URL", jwks_url),
        )
        if not value
    ]
    if missing:
        logger.error("Not set: %s", ", ".join(missing))
        return 2
    assert database_url and issuer and jwks_url  # noqa: S101 - narrowed above
    try:
        asyncio.run(_run(_normalize_database_url(database_url), issuer, jwks_url))
    except Exception as exc:  # pragma: no cover - error path
        logger.exception("Backfill failed: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
