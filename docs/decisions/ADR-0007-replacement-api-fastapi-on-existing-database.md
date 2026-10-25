# ADR-0007: Replacement API: a FastAPI service on the existing database

**Ported from:** deejaytools-api ADR-0006 on 2026-10-25, when that repository was archived as [legacy-graveyard/deejaytools-api](https://github.com/legacy-graveyard/deejaytools-api/tree/main/docs/decisions). Numbers are shifted by one to follow this repository's ADR-0001; only titles, citations and links were changed. In the body, "this repository" and "this service" mean deejaytools-api.

Date: 2026-10-01

## Status

Accepted

## Context

This API is being replaced by a Python service. The decision to replace it
is made; this ADR records the shape of the replacement so the work starts
from settled ground.

Constraints:

- **The database stays.** Same Postgres instance, same tables, same rows.
  Only the service in front of it changes.
- **The web app (deejaytools-com) must keep working through the switch**
  without a coordinated release.
- **The ecosystem already has a Python API** (api-kaianolevine-com), a shared
  library (common-python-utils, `mini_app_polis`), the `identity` library,
  and ecosystem-standards. The new service follows them rather than porting
  this repo's local choices.

What this service does is specified in this repo: [API.md](https://github.com/legacy-graveyard/deejaytools-api/blob/main/docs/API.md),
[SCHEMA.md](https://github.com/legacy-graveyard/deejaytools-api/blob/main/docs/SCHEMA.md), [DRIVE.md](https://github.com/legacy-graveyard/deejaytools-api/blob/main/docs/DRIVE.md),
[AUDIO-TAGGING.md](https://github.com/legacy-graveyard/deejaytools-api/blob/main/docs/AUDIO-TAGGING.md), [ADR-0006](./ADR-0006-floor-trial-queue-model-as-built.md)
and [CONFORMANCE.md](https://github.com/legacy-graveyard/deejaytools-api/blob/main/docs/CONFORMANCE.md).

## Decision

1. **A new repository**, separate from this one and from
   api-kaianolevine-com. `api-deejaytools-com` follows the existing naming
   convention. Same stack and layout as api-kaianolevine-com: FastAPI,
   SQLAlchemy async with asyncpg, pydantic-settings, `uv`, Sentry,
   `mini_app_polis.logger`, Railway with `railway.json`.
2. **Same database, additive changes only until cutover is final.** The only
   schema changes before this repo is retired are new tables (identity, the
   migration tracker; see [ADR-0008](./ADR-0008-replacement-api-identity-authorization.md)
   and [ADR-0009](./ADR-0009-replacement-api-raw-sql-migrations.md)). Nothing this
   service reads or writes changes shape, so traffic can go back to this API
   at any point until it is retired.
3. **The wire contract is preserved for the cutover.** Paths, request and
   response shapes, status codes and error **codes** match API.md, including
   its quirks, so the web app does not change at the switch.
   [ADR-0010](./ADR-0010-replacement-api-contract-and-testing.md) has the details
   and the exceptions. Cleanup is a later, coordinated change on both sides.
4. **Known defects are not carried over.** Everything listed under "Known
   defects" in DRIVE.md and AUDIO-TAGGING.md is fixed in the new service, not
   reproduced.
5. **Cutover is a traffic switch, not a migration.**
   - Deploy the new service against the shared database with its scheduler
     **off**, and run the conformance suite against it on dev.
   - In a quiet window (not the days before an event), stop this API's
     scheduler, point the web app at the new service, then turn the new
     scheduler on. Never run both schedulers: both would advance sessions and
     drain the Drive job queue.
   - Uploads in progress at the moment of the switch are lost (their chunks
     are on this API's local disk). The quiet window makes that rare.
   - Rollback is the same switch in reverse, with the scheduler handover
     reversed.
6. **This repo is retired once the new service has run a full event without
   rollback.** Its drizzle migrations stop with it (ADR-0009).

## Consequences

- No data migration, no downtime beyond the switch, and a rollback that costs
  minutes.
- The schema cannot be cleaned up (dropping `users.role`, the unused
  `legacy_songs` table, the drizzle journal) until this repo is retired,
  because this API must keep working against it.
- Behavior the web app relies on but that is not in the docs only shows up
  when the web app's own contract suite runs against the new service on dev.
  That run is a cutover gate (ADR-0010).
- The replacement inherits the standards' requirements on authorization,
  which this API predates. That is ADR-0008, and it is the largest piece of
  new design in the rewrite.
