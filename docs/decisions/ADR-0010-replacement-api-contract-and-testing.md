# ADR-0010: Replacement API: wire contract, reuse, and how it is tested

**Ported from:** deejaytools-api ADR-0009 on 2026-10-25, when that repository was archived as [legacy-graveyard/deejaytools-api](https://github.com/legacy-graveyard/deejaytools-api/tree/main/docs/decisions). Numbers are shifted by one to follow this repository's ADR-0001; only titles, citations and links were changed. In the body, "this repository" and "this service" mean deejaytools-api.

Date: 2026-10-01

## Status

Accepted

## Context

ADR-0007 keeps the web app unchanged through the cutover, so the new service
has to answer exactly as this API does. The ecosystem's Python conventions
differ from this API in places (error code casing, `/health` semantics,
SQLite-backed tests). This ADR settles each difference and says how
"answers exactly as this API does" gets checked.

## Decision

### Wire contract

- **Envelope:** `{ data, meta: { version, ... } }` and
  `{ error: { code, message } }` as [API.md](../API.md) describes, `meta.count` on lists.
  API-005 and XSTACK-002 are already satisfied by this shape.
- **Error codes are this API's, not the Python house style.** The new service
  answers `UNAUTHORIZED`, `USER_NOT_SYNCED`, `FORBIDDEN`, `NOT_FOUND`,
  `BAD_REQUEST`, `VALIDATION_ERROR`, `INTERNAL`, and the mixed-case ones [API.md](../API.md)
  lists (`conflict`, `forbidden` on self-demotion, `too_many_requests`,
  `request_timeout`, `payload_too_large`, `SONG_IN_ACTIVE_CHECKIN`, `CHUNK_*`,
  `UNSUPPORTED_FORMAT`), not api-kaianolevine-com's lowercase
  `unauthorized` / `forbidden`. The identity library's deny maps to
  `401 UNAUTHORIZED` / `403 FORBIDDEN`. Normalizing codes is a later change
  made together with the web app.
- **Messages:** fixed messages in [API.md](../API.md)'s error tables are reproduced word
  for word. Validation (`VALIDATION_ERROR`) messages are the exception: they
  come from zod today and will come from pydantic, so their text will
  differ. The code, status and envelope are the contract; the web app must
  not depend on validation message text (checked in the cutover gate below).
- **Unchanged middleware behavior:** 300 requests per minute per client IP
  from `X-Forwarded-For` on `/v1/*`, 11 MiB body limit (`413
  payload_too_large`), 30 s deadline (300 s on `/v1/songs/upload/*`, `503
  request_timeout`), CORS from `CORS_ORIGINS`, the 3 s / 5 s response cache.
- **Two deliberate differences:**
  - `GET /health` follows API-010: always `200 {"status":"ok"}`, liveness
    only. This API answers 503 when the database is down. Confirm nothing but
    Railway's healthcheck reads `/health` before relying on this.
  - `GET /internal/tick` fails closed when `TICK_SECRET` is unset (ADR-0008).
    It stays unversioned, with an API-004 exemption, because operators and the
    conformance suite call it at that path.

### Reuse from the ecosystem

| Need | Use | Note |
|------|-----|------|
| Layout, settings, logger, Sentry, `api_error`, Railway config | api-kaianolevine-com | Copy the shape, not the routes |
| Authorization | `identity` (`ClerkVerifier`, store, policy, audit) | ADR-0008 |
| Drive folders, copy, move, rename, upload, trash | `mini_app_polis.google.DriveFacade` | Synchronous: call it off the event loop. Add the missing reader-share call (`permissions.create`, no notification email) to common-python-utils with its own tests, rather than calling Drive directly here |
| Audio tagging | `mutagen`, written to [AUDIO-TAGGING.md](../AUDIO-TAGGING.md) | **Not** `mini_app_polis.mp3.Mp3Tagger`: it is MP3-only and path-based, and its routine title/artist builders produce different tags (artist carries version and season; no ProAm FollowerAm swap) |
| Migrations | api-kaianolevine-com's runner | ADR-0009 |

### Testing

1. **Unit and service tests run against Postgres, not SQLite.** The queue
   depends on `SELECT … FOR UPDATE`, the job queue on `FOR UPDATE SKIP
   LOCKED`, and the schema on Postgres enums and check constraints. CI gets
   a Postgres service container, as this repo's CI does.
2. **Golden cases are ported, not rewritten:** the fixtures in
   `src/services/tagger.test.ts` (every format, provenance, missing
   containers, fast-start offsets, 64-bit atoms, the WAV overrun cases) and
   the behavior cases in `src/services/driveJobs.test.ts` become pytest cases
   with the same inputs and expected fields.
3. **This repo's integration suite is the conformance suite** ([CONFORMANCE.md](../CONFORMANCE.md)):
   the new service's CI checks out this repo at a pinned commit and runs
   `pnpm test:integration` with `INTEGRATION_BASE_URL` pointed at the new
   service. Every route must be hit (the route ledger) and every assertion
   must pass. The run needs these harness changes first, made in this repo:
   - **Schema:** after building the database from `drizzle/`, apply the
     target's post-baseline migrations (an `INTEGRATION_EXTRA_MIGRATIONS`
     directory), so the identity tables and seed rows exist.
   - **Reset:** the per-test `TRUNCATE` must spare the identity seed tables
     (`identity_issuers`, `identity_roles`, `identity_role_scopes`) and the
     runner's `schema_migrations`. It still truncates principals, grants and
     audit events.
   - **Admins:** `actor({ admin: true })` sets `users.role` today. It must
     also grant `deejaytools-admin` to the actor's principal when the identity
     tables exist, or every admin-route test fails against the new service.
4. **Cutover gate:** on dev, with the new service behind the web app's dev
   URL, the web app's own contract suite passes and a manual pass covers
   upload, submission to an event with the event copy landing in Drive, and
   one floor-trial session end to end.

### Standards exemptions to carry in `evaluator.yaml`

- **API-011** (and API-003's Alembic clause): raw-SQL migration runner, as in
  api-kaianolevine-com.
- **API-006:** shared operational tables (ADR-0008).
- **API-004:** `GET /internal/tick`.
- **CD-030:** no machine callers (ADR-0004).

## Consequences

- The web app does not change at cutover. The casing cleanup and the
  validation-message question become one later, coordinated change.
- The rewrite has a mechanical definition of done: the conformance suite
  passes against it with every route exercised, plus the cutover gate.
- The harness changes in Testing point 3 are this repo's work and come
  before the new service's first conformance run.
- Postgres in CI makes the new service's tests slower than
  api-kaianolevine-com's, and is the only way to test the locking the queue
  and the job queue depend on.
