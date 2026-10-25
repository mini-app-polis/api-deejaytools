# ADR-0009: Replacement API: numbered raw-SQL migrations from a baseline

**Ported from:** deejaytools-api ADR-0008 on 2026-10-25, when that repository was archived as [legacy-graveyard/deejaytools-api](https://github.com/legacy-graveyard/deejaytools-api/tree/main/docs/decisions). Numbers are shifted by one to follow this repository's ADR-0001; only titles, citations and links were changed. In the body, "this repository" and "this service" mean deejaytools-api.

Date: 2026-10-01

## Status

Accepted

## Context

This API owns the schema through drizzle (`drizzle/0000`–`0015`), applied at
deploy time (ADR-0002). The replacement keeps the database (ADR-0007), so the
schema's history needs a new owner. Rebuilding it, or re-running the
history against production, is out of the question.

api-kaianolevine-com manages its schema as numbered raw-SQL files under
`migrations/`, applied by `scripts/apply_migrations.py` from Railway's
`startCommand`. That script tracks applied files in `schema_migrations`,
serializes runs with an advisory lock, and can mark files already applied
without running them (`BOOTSTRAP_MIGRATIONS`). The choice and the runner are
api-kaianolevine-com ADR-0001 and ADR-0005, and API-011 carries an exemption for it.
ecosystem-standards API-003 names Alembic, but the raw-SQL runner is the
precedent in this ecosystem's one Python API.

## Decision

1. **Copy the runner** from api-kaianolevine-com into the new service, with
   one change: `DEFAULT_BOOTSTRAP_EXCLUDE` is empty. The default there names
   a file of that repo's own history.
2. **`001_baseline.sql` is the current schema**, exactly as drizzle's
   0000–0015 leave it: tables, enums (including `moved_within_queue`),
   constraints, indexes, and the drift SCHEMA.md records (for example
   `fk_checkins_session_division`, which exists in the database but not in
   `schema.ts`). Generate it from a database built by those migrations
   (`pg_dump --schema-only`), not by hand, and check it by diffing a database
   built from the baseline against one built by drizzle.
3. **Production adopts the baseline without running it:** the first deploy of
   the new service sets `BOOTSTRAP_MIGRATIONS=true` with
   `BOOTSTRAP_EXCLUDE` naming every file after the baseline. The runner records
   `001_baseline.sql` as applied and runs the rest. Remove the variable after
   that deploy.
4. **`002_identity_store.sql`** installs the identity tables and the backfill
   of ADR-0008. It is additive: this API ignores the new tables, so rollback
   still works.
5. **No other schema change until this API is retired.** Every migration
   before then is additive. Drops and renames (for example `users.role`,
   `legacy_songs`, the `drizzle` schema with its journal) wait for
   retirement.
6. **Drizzle is frozen now.** No new file goes into `drizzle/` in this repo.
   This API keeps running `drizzle-kit migrate` at deploy, which finds nothing
   to do. That is harmless and needs no change while both exist.
7. **The new service's test databases are built from `migrations/`**, so the
   baseline is exercised on every test run. The conformance suite (ADR-0010)
   builds its database from `drizzle/` and then applies the new service's
   post-baseline migrations, so a conformance run is also a check that the
   baseline and drizzle agree.

## Consequences

- One schema history going forward, in the new repo, in the same form as
  the ecosystem's other Python API.
- The new service carries the API-011 exemption, worded like
  api-kaianolevine-com's.
- The baseline is only as good as its comparison against drizzle's result.
  The diff in point 2 is required, not optional; it's how drift between
  `schema.ts` and the live database is caught before it becomes the new
  source of truth.
- A rollback to this API is safe as long as point 5 holds.
