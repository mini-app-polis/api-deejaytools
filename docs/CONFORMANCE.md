# Running the conformance suite

`conformance/` is a black-box contract suite (ADR-0010, Testing point 3):
TypeScript tests under Vitest that drive this service over real HTTP and
assert on its status codes, envelopes and stored rows. It never imports the
app; it needs a running server. Its route ledger
([`conformance/harness/route-ledger.ts`](../conformance/harness/route-ledger.ts))
fails the run unless every route in the fixed contract table
([`conformance/harness/routes.ts`](../conformance/harness/routes.ts)) was
called at least once. CI runs it in the `test` job of
[`.github/workflows/ci.yml`](../.github/workflows/ci.yml), after pytest,
and it gates like everything else.

Prerequisites: Python 3.11, [uv](https://docs.astral.sh/uv/), Node 22 or
later (pnpm through corepack, pinned by `conformance/package.json`), and a
local Postgres 16 reachable as `postgres:postgres@localhost:5432`. No Doppler:
everything the service needs here is set on the command line.

From the root of a fresh clone:

```bash
# 1. Dependencies
uv sync --all-extras
corepack enable
(cd conformance && pnpm install --frozen-lockfile)

# 2. A database the suite may wipe: local, name ending in _test
createdb -h localhost -U postgres conformance_test
export DEEJAYTOOLS_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/conformance_test

# 3. The service under test, with the stand-in Clerk, a tick secret and no
#    background scheduler
DEEJAYTOOLS_CLERK_JWKS_URL=http://127.0.0.1:4455/.well-known/jwks.json \
DEEJAYTOOLS_CLERK_ISSUER=https://clerk.integration.test \
TICK_SECRET=conformance-tick \
DISABLE_SCHEDULER=1 \
  uv run uvicorn src.api_deejaytools.main:app --port 3999 > target.log 2>&1 &
until curl -sf http://localhost:3999/health > /dev/null; do sleep 1; done

# 4. The suite
cd conformance
INTEGRATION_BASE_URL=http://localhost:3999 \
INTEGRATION_EXTRA_MIGRATIONS=../migrations \
INTEGRATION_TICK_SECRET=conformance-tick \
  pnpm test
```

`pnpm typecheck` (in `conformance/`) type-checks the suite without running
it; CI runs it before the suite.

Before any test runs, the suite's global setup
([`conformance/harness/global-setup.ts`](../conformance/harness/global-setup.ts))
drops and recreates the `public` schema, applies the frozen drizzle
migrations in `conformance/drizzle/` (which build the same schema as
`migrations/001_baseline.sql`), then every file in
`INTEGRATION_EXTRA_MIGRATIONS` except `001_*`. It starts the stand-in Clerk
key server, and stops with the setup it needs if `GET /health` on the target
does not answer 200. The service survives the schema being rebuilt under it
(no prepared-statement caching, see `src/api_deejaytools/database.py`), so it
can stay up across runs.

## Suite settings

| Env var | Default | Purpose |
|---------|---------|---------|
| `INTEGRATION_BASE_URL` | required | Origin of the server under test, no path (`http://localhost:3999`) |
| `DEEJAYTOOLS_DATABASE_URL`, else `DATABASE_URL` | required | The database the suite rebuilds, resets and seeds. Must be on a local host and named `*_test` |
| `INTEGRATION_EXTRA_MIGRATIONS` | unset (none applied) | Folder of migrations to apply after the drizzle baseline. Set it to `../migrations`: without it the identity tables do not exist and every authenticated call fails |
| `INTEGRATION_JWKS_PORT` | `4455` | Port of the stand-in Clerk JWKS server |
| `INTEGRATION_TICK_SECRET` | unset | Sent as `x-tick-secret` on `GET /internal/tick`; must equal the target's `TICK_SECRET` |

## What the target must be configured with

| Requirement | Why |
|-------------|-----|
| **The same database** as the suite's | The suite drives state through the API and also writes to the database directly: it grants `deejaytools-admin` in the identity store for admin actors, seeds songs, and truncates every table except `schema_migrations` and the identity seed tables (`identity_issuers`, `identity_roles`, `identity_role_scopes`) before each test. The target must read these rows rather than caching them. |
| **No migrations of its own** | The suite builds the schema itself (above). Start the service with uvicorn as shown, not through the Docker image, whose CMD runs `scripts/apply_migrations.py`. |
| **JWKS URL** `http://127.0.0.1:<INTEGRATION_JWKS_PORT>/.well-known/jwks.json` and **issuer** `https://clerk.integration.test` | Tokens are RS256 JWTs signed by a key the suite serves there, checked the way Clerk tokens are: signature (key chosen by `kid`), issuer and expiry. The key pair is kept in the OS temp directory (`deejaytools-integration-clerk-key.json`) and reused across runs, so a target that caches the JWKS keeps working. Deleting the file rotates the key under a new `kid`. |
| **No background scheduler** (`DISABLE_SCHEDULER=1`, or `ENVIRONMENT=test` as CI sets it) | Tests advance the queue with `GET /internal/tick` and assert on what one pass did. A background tick racing them makes queue assertions flaky. |
| **`TICK_SECRET` set**, and `INTEGRATION_TICK_SECRET` equal to it | `GET /internal/tick` refuses every call while `TICK_SECRET` is unset. The operator test checks it answers 403 without the header and 200 with it. |
| **No Google Drive credentials** | Without them every Drive call fails at once: the upload test relies on a failed build removing the song, and Drive jobs fail and retry harmlessly. With real credentials, tests would upload to that Drive. |
| **Client address from `X-Forwarded-For`** | The suite gives each request its own address as a single hop, which the service reads as the last hop, Railway's ([OPERATIONS.md](./OPERATIONS.md#client-address)), so the 300 requests/minute per-address limit is never hit. |
| **No Brevo key** | `POST /v1/feedback` would otherwise send real email. |

## What is and isn't covered

Covered: every route in the contract table — request validation, response
envelopes and status codes, auth, ownership rules, constraints and cascades,
queue admission and ordering through the tick, and the upload endpoint's
error paths and failure cleanup.

Not covered here, so covered by pytest (`tests/`) or checked separately:

- **Routes outside the contract table**, such as `GET /version`, the
  per-event staff routes and `GET /v1/queue/{session_id}/active/staff`. The
  ledger checks only the table in `routes.ts`.
- **The upload success path.** It needs Drive; pytest covers it against a
  fake Drive.
- **What Drive jobs do.** The suite never sees a successful copy, rename,
  deprecation or folder share. Spec: [DRIVE.md](./DRIVE.md#drive_jobs-queue).
- **Tag contents of uploaded files.** Spec: [AUDIO-TAGGING.md](./AUDIO-TAGGING.md).
- **Background scheduling** (interval, overlap guard, startup tick) and
  **response caching** (3 s queue reads, 5 s session reads).
- **Logging, Sentry and Discord notifications.**

**Response cache caveat.** The suite cannot empty a separate process's
response cache between tests, so up to 5 seconds of cached session reads can
carry from one test into the next. The suite passes with caching on; if a
failure goes away when caching is off, check this first.
