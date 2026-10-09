# api-deejaytools

The FastAPI service behind [deejaytools.com](https://deejaytools.com): events,
floor-trial sessions and their queues, check-ins, songs and their Google Drive
files. It replaces `deejaytools-api` (Hono/Node) **on the same database**,
answering the web app exactly as that service does, so the web app does not
change at the switch.

The decisions behind it are in deejaytools-api's `docs/decisions/`:
ADR-006 (the replacement), ADR-007 (authorization), ADR-008 (migrations) and
ADR-009 (wire contract and testing). The behavior spec is deejaytools-api's
`docs/API.md`, `SCHEMA.md`, `DRIVE.md`, `AUDIO-TAGGING.md` and ADR-005. See
[docs/DESIGN.md](docs/DESIGN.md) for how this repo is laid out against them.

## Inputs and outputs

- **In:** HTTPS requests from the deejaytools.com web app, authenticated with
  Clerk session JWTs from this environment's Clerk instance.
- **Out:** JSON in deejaytools-api's envelope —
  `{ data, meta: { version } }` / `{ error: { code, message } }` — and rows
  in the shared deejaytools Postgres database.

## Endpoints

Every route of deejaytools-api's `docs/API.md`, at the same path, with the
scope ADR-007 maps it to (`require_scope(...)` in
`src/api_deejaytools/auth.py`). "public" routes need no token.

| Route | Auth |
|-------|------|
| `GET /` | public — redirects to `/docs` (Swagger UI); the OpenAPI schema is at `/openapi.json` |
| `GET /health` | public — always `200 {"status":"ok"}`, liveness only |
| `GET /version` | public — package version and deployed commit, for the post-deploy smoke test (not a deejaytools-api route) |
| `GET /internal/tick` | `x-tick-secret` header matching `TICK_SECRET`; `403` when unset |
| `POST /v1/auth/sync` | authenticated-only — upserts the `users` row and provisions the principal |
| `GET /v1/auth/me` | authenticated-only — the caller's record, `role` derived from their grants |
| `PATCH /v1/auth/me` | `deejaytools.profile.write` |
| `GET /v1/events`, `GET /v1/events/{id}` | public |
| `GET /v1/events/{id}/entities` | `deejaytools.entities.read` |
| `POST /v1/events`, `PATCH`/`DELETE /v1/events/{id}` | `deejaytools.events.write` |
| `GET /v1/sessions`, `GET /v1/sessions/{id}` | public; a valid synced caller also gets their check-in fields |
| `POST /v1/sessions`, `PATCH`/`DELETE /v1/sessions/{id}`, `PUT /v1/sessions/{id}/divisions`, `PATCH /v1/sessions/{id}/status` | `deejaytools.sessions.write` |
| `GET /v1/partners`, `GET /v1/partners/leading-pairs`, `GET /v1/partners/{id}`, `GET /v1/partners/{id}/associations` | `deejaytools.partners.read` |
| `POST /v1/partners`, `PATCH`/`DELETE /v1/partners/{id}`, `POST /v1/pairs/find-or-create` | `deejaytools.partners.write` |
| `GET /v1/teams` | `deejaytools.teams.read` |
| `POST /v1/teams`, `PATCH`/`DELETE /v1/teams/{id}` | `deejaytools.teams.write` |
| `GET /v1/managed-partnerships` | `deejaytools.partnerships.read` |
| `POST /v1/managed-partnerships`, `PATCH`/`DELETE /v1/managed-partnerships/{id}` | `deejaytools.partnerships.write` |
| `GET /v1/event-song-submissions` | `deejaytools.submissions.read` |
| `POST /v1/event-song-submissions`, `DELETE /v1/event-song-submissions/{id}` | `deejaytools.submissions.write` |
| `GET /v1/songs`, `GET /v1/songs/{id}` | `deejaytools.songs.read` |
| `POST /v1/songs`, `POST /v1/songs/upload/chunk`, `PATCH`/`DELETE /v1/songs/{id}` | `deejaytools.songs.write`; acting for another user also needs `deejaytools.delegation.act` |
| `POST /v1/checkins`, `DELETE /v1/checkins/{id}` | `deejaytools.checkins.write`; on behalf of another user also `deejaytools.delegation.act` |
| `GET /v1/checkins/mine` | `deejaytools.checkins.read` |
| `GET /v1/queue/{session_id}/active`, `GET /v1/queue/{session_id}/waiting` | public |
| `GET /v1/queue/{session_id}/priority`, `GET /v1/queue/{session_id}/non-priority` | `deejaytools.queue.read` |
| `POST /v1/queue/promote`, `/complete`, `/incomplete`, `/move-down`, `/withdraw` | `deejaytools.queue.manage` |
| `GET /v1/runs` | `deejaytools.runs.read` |
| `POST /v1/feedback` | public |
| `GET /v1/admin/users`, `GET /v1/admin/users/{id}/partners`, `GET /v1/admin/users/{id}/event-song-submissions` | `deejaytools.users.read` |
| `PATCH /v1/admin/users/{id}/role` | `deejaytools.users.write` |
| `GET /v1/admin/songs` | `deejaytools.library.read` |
| `GET /v1/admin/event-song-submissions` | `deejaytools.entries.read` |
| `GET /v1/admin/drive-jobs`, `GET /v1/admin/drive-jobs/summary` | `deejaytools.drivejobs.read` |
| `POST /v1/admin/drive-jobs/{id}/retry`, `POST /v1/admin/drive-jobs/backfill-renames` | `deejaytools.drivejobs.write` |
| `POST /v1/admin/checkins`, `DELETE /v1/admin/checkins/test` | `deejaytools.testdata.write` |
| `GET /v1/admin/checkins/test` | `deejaytools.testdata.read` |

Requests are limited as deejaytools-api limits them
(`src/api_deejaytools/middleware.py`): 11 MiB bodies, 300 requests a minute
per client address on `/v1/*`, and a 30 s deadline (300 s for uploads).

The full contract is checked by the conformance suite
([conformance/README.md](conformance/README.md)). Background work (session
statuses, queue fill, song builds, Drive jobs) runs on the in-process
scheduler every `TICK_INTERVAL_MS`, or once per `GET /internal/tick`.

## Running locally

Prerequisites: Python 3.11, [uv](https://docs.astral.sh/uv/), Postgres 16,
and the [Doppler CLI](https://docs.doppler.com/docs/install-cli). Secrets come
from Doppler's shared `dev` config — nothing reads a `.env` file, and local
runs never use `prd`. `DEEJAYTOOLS_DATABASE_URL` is not in Doppler: set it in
the shell for your local database. `doppler run` passes it through because
Doppler holds no value for that name (if it did, Doppler's would win). The
same goes for `DISABLE_SCHEDULER=1`, which keeps the scheduler — and its Drive
uploads to the real folder — off locally.

```bash
brew install gnupg dopplerhq/cli/doppler && doppler login   # once per machine
doppler setup                   # once per clone: reads doppler.yaml
uv sync --all-extras
uv run pre-commit install
uv run check-doppler-keys       # every required .env.example name is in dev
export DEEJAYTOOLS_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/deejaytools

# Schema: apply migrations/ to an empty database, then provision principals
doppler run -- uv run python scripts/apply_migrations.py
doppler run -- uv run python scripts/backfill_principals.py

DISABLE_SCHEDULER=1 doppler run -- uv run uvicorn src.api_deejaytools.main:app --reload --port 3001
```

Tests run against a real local Postgres database whose name ends in `_test`
(the fixtures drop its `public` schema):

```bash
createdb deejaytools_test
TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/deejaytools_test \
  uv run pytest --cov=src
uv run pre-commit run --all-files
uv run ruff check src tests scripts && uv run ruff format src tests scripts
uv run mypy src/
PGURL=postgres://postgres:postgres@localhost:5432 bash scripts/check_baseline.sh
```

## Database and migrations

The database is deejaytools-api's, in production, as it is. Until
deejaytools-api is retired, schema changes are **additive only** (ADR-008), so
traffic can be switched back to it at any time.

- `migrations/001_baseline.sql` is the schema drizzle's `0000`–`0015` build,
  generated by `REGENERATE=1 scripts/check_baseline.sh`, never by hand.
  `conformance/drizzle/` is a frozen copy of those drizzle migrations; CI
  rebuilds both and fails on any difference.
- `migrations/002_identity_store.sql` adds the identity tables, roles and
  scopes. It changes no existing table.
- `migrations/003_song_uploads.sql` adds `song_uploads`, where an uploaded
  song waits until its Drive build finishes. deejaytools-api never reads it.
- `migrations/004_song_upload_uploader.sql` adds a nullable
  `song_uploads.uploaded_by_user_id`, so the "song added" notification can
  name both people when a song was uploaded on someone's behalf.
- `scripts/apply_migrations.py` applies pending files on every deploy,
  tracked in `schema_migrations`, before the app starts.
- `scripts/backfill_principals.py` runs next, on every deploy: it writes this
  environment's issuer row, makes every `users` row a principal, and keeps
  `deejaytools-admin` in step with `users.role`. Idempotent.

**First deploy against an existing database** (once per environment): set
`BOOTSTRAP_MIGRATIONS=true` and `BOOTSTRAP_EXCLUDE` to every migration after
the baseline (today:
`002_identity_store.sql,003_song_uploads.sql,004_song_upload_uploader.sql`).
The runner records `001_baseline.sql` as applied without running it and
runs the rest. Remove
both variables after that deploy. A bootstrap without `BOOTSTRAP_EXCLUDE` is
refused.

## Deployment

Railway, from `railway.json`: migrations, then the principal backfill, then
uvicorn. Configuration comes from the shared Doppler config under the
`DEEJAYTOOLS_` names deejaytools-api also reads; see `.env.example`.
Discord notifications (`DISCORD_WEBHOOK_URL`, optionally
`DISCORD_WEBHOOK_URL_ERRORS` and `DISCORD_WEBHOOK_URL_ACTIVITY`; off when
unset) are described in [docs/OPERATIONS.md](docs/OPERATIONS.md), with
switching traffic back to deejaytools-api and retrying failed song builds.

## Versioning

semantic-release on `main` from Conventional Commits. It writes
`src/api_deejaytools/_version.py` and `CHANGELOG.md`; neither is edited by
hand.
