# Design

The settled decisions are deejaytools-api's ADR-006 to ADR-009. This file
records how this repository carries them out, and the places where it had
to decide something they leave open.

## Shape

Copied from api-kaianolevine-com: FastAPI, SQLAlchemy async with asyncpg,
pydantic-settings, uv, Sentry, `mini_app_polis.logger`, the request-metrics
middleware, Railway with `railway.json`, and its raw-SQL migration runner.
Not its routes, and not its error style: this service answers in
deejaytools-api's codes and shapes (`errors.py`).

## Decisions made here

- **Configuration names.** Settings read the `DEEJAYTOOLS_`-prefixed names
  deejaytools-api reads. The shared Doppler config holds the other Clerk
  tenant's `CLERK_*` values, so the bare names would point this service at
  the wrong issuer.
- **The issuer is per environment.** Each environment has its own Clerk
  instance, so neither the issuer row nor the principal backfill is in
  migration 002 (ADR-007 put them there). `scripts/backfill_principals.py`
  writes both from settings on every deploy, and `POST /v1/auth/sync` writes
  the issuer row if it is missing (the conformance suite rebuilds the schema
  under a running service).
- **First provision mirrors a stored admin.** A principal created by sync
  for a `users` row that already says `admin` also gets `deejaytools-admin`,
  the backfill's rule. Never re-applied to an existing principal.
- **`users.role` decides admin at each deploy.** The backfill grants
  `deejaytools-admin` where `users.role = 'admin'` and removes it elsewhere,
  so a promotion or demotion made through deejaytools-api during a rollback
  carries over. Through this service both always move together.
- **`/internal/tick` fails closed** with `403 FORBIDDEN` "Admin access
  required" when `TICK_SECRET` is unset, the answer deejaytools-api gives
  for a wrong secret (ADR-007 leaves the status open).
- **Bootstrap needs an exclude list.** The runner has no default
  `BOOTSTRAP_EXCLUDE` (ADR-008) and refuses a bootstrap without one when
  there is more than one migration, which would otherwise mark the identity
  store applied without creating it.
- **Wire details.** `meta.version` is `"v1"`; errors carry `code` and
  `message` only; validation failures are `400 VALIDATION_ERROR`; a wrong
  method is `404 NOT_FOUND` as in Hono; `/v1/auth/sync` validates its body
  before its token; a missing scope answers `403 FORBIDDEN` "Admin access
  required" while the audit row records the real scope.
- **`/v1/auth/me`** answers `401 USER_NOT_SYNCED` when there is no `users`
  row, as today. A `users` row with no principal yet answers with
  `role: "user"`.

- **Request validation follows zod, not pydantic's defaults**
  (`validation.py`): no type coercion, optional fields refuse `null` unless
  zod said `.nullable()`, and email addresses use zod's own pattern
  (pydantic's refuses reserved domains such as `.test`). Event timezones are
  checked the way `Intl` checks them: any IANA name, case-insensitively,
  stored as sent.
- **Request limits** (`middleware.py`) are deejaytools-api's, keyed the same
  way. The deadline does not cancel the handler, as Node could not: it runs
  on and its writes land, only its response is discarded.
- **No prepared-statement caching** (`database.py`): the service survives
  its schema being rebuilt underneath it, which deejaytools-api's
  CONFORMANCE.md requires of a target, for one extra round trip per query.

## Testing

Tests build a real Postgres database from `migrations/` with the runner, so
the baseline is exercised on every run. `scripts/check_baseline.sh` checks
the baseline against drizzle's result in CI. The conformance suite
(deejaytools-api's integration suite, copied into `conformance/` with
ADR-009's three harness changes) runs in CI against this service; it may
fail until every route exists.
