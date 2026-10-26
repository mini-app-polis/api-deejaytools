> Ported from [deejaytools-api](https://github.com/legacy-graveyard/deejaytools-api)'s docs/CONFORMANCE.md when that repository was archived. "This service" and `src/` paths below mean deejaytools-api, whose contract api-deejaytools reproduces.

# Running the integration suite over HTTP

By default the integration suite (`pnpm test:integration`) imports the app and
calls it in-process. Set `INTEGRATION_BASE_URL` and the same tests send every
request over real HTTP to a server you started yourself instead. That server
can be this API or a reimplementation of it, in any language. The suite then
works as a black-box conformance check: the same requests, the same assertions,
and the same [route ledger](https://github.com/legacy-graveyard/deejaytools-api/tree/main/src/test/integration/route-ledger.ts), which
fails the run unless every route this API registers was called at least once.

```bash
# 1. Start the server under test (here: this API itself)
DEEJAYTOOLS_DATABASE_URL=postgres://postgres:postgres@localhost:5432/deejaytools_test \
DEEJAYTOOLS_CLERK_JWKS_URL=http://127.0.0.1:4455/.well-known/jwks.json \
DEEJAYTOOLS_CLERK_ISSUER=https://clerk.integration.test \
DISABLE_SCHEDULER=1 PORT=3999 \
  node --import tsx/esm src/index.ts

# 2. Run the suite against it
INTEGRATION_BASE_URL=http://localhost:3999 \
DATABASE_URL=postgres://postgres:postgres@localhost:5432/deejaytools_test \
  pnpm test:integration
```

The suite checks `GET /health` before any test runs and stops with the required
setup if the target does not answer 200.

## Suite settings

| Env var | Default | Purpose |
|---------|---------|---------|
| `INTEGRATION_BASE_URL` | unset → in-process | Origin of the server under test, no path (`http://localhost:3999`) |
| `DATABASE_URL` / `DEEJAYTOOLS_DATABASE_URL` | required | The database the suite resets and seeds. Must be local and named `*_test` |
| `INTEGRATION_JWKS_PORT` | `4455` over HTTP, a free port in-process | Port of the stand-in Clerk JWKS server |
| `INTEGRATION_TICK_SECRET` | unset | Sent as `x-tick-secret` on `GET /internal/tick`, for a target that has `TICK_SECRET` set |

## What the target must be configured with

| Requirement | Why |
|-------------|-----|
| **The same database** as the suite's `DATABASE_URL` | The suite drives state through the API and also writes to the database directly: it promotes admins (`UPDATE users SET role = 'admin'`), seeds songs, and truncates every table before each test. The target must read these rows rather than caching them. |
| **Schema from this repo's migrations** | At startup the suite drops the `public` schema and applies every migration in `drizzle/`. A reimplementation must work against that schema as-is and must not run migrations of its own against the test database. The target can keep running across suite runs; it has to survive the schema being rebuilt under it. |
| **JWKS URL** `http://127.0.0.1:<INTEGRATION_JWKS_PORT>/.well-known/jwks.json` and **issuer** `https://clerk.integration.test` | Tokens are RS256 JWTs signed by a key the suite serves there, checked the way Clerk tokens are: signature (key chosen by `kid`), issuer and expiry. The key pair is kept in the OS temp directory (`deejaytools-integration-clerk-key.json`) and reused across runs, so a target that caches the JWKS keeps working. Deleting the file rotates the key under a new `kid`. |
| **No background scheduler** (`DISABLE_SCHEDULER=1` here) | Tests advance the queue with `GET /internal/tick` and assert on what one pass did. A background tick racing them makes queue assertions flaky. |
| **`GET /internal/tick` open**, or `INTEGRATION_TICK_SECRET` set to the target's secret | As above. |
| **No Google Drive credentials** | Without them every Drive call fails at once: an upload's background step deletes the song row (a test relies on this) and Drive jobs fail and retry harmlessly. With real credentials, tests would upload to that Drive. |
| **Client address from `X-Forwarded-For`** | The suite gives each request its own address, so the 300 requests/minute per-IP limit is never hit. A target that keys rate limiting on the socket address will start answering 429. |
| **No Brevo key** | `POST /v1/feedback` would otherwise send real email. |

## What is and isn't covered over HTTP

Covered: every route's request validation, response envelopes and status codes,
auth, ownership rules, constraints and cascades, queue admission and ordering
through the tick, and the upload endpoint's error paths and failure cleanup.

Skipped over HTTP: the upload success path (`songs-upload.integration.test.ts`),
because it swaps the Drive calls for in-process stand-ins that cannot reach
another process. It still runs in-process.

Not covered in either mode, so check separately:

- **What Drive jobs do.** The suite never sees a successful copy, rename or
  deprecation. Spec: [DRIVE.md](./DRIVE.md#drive_jobs-queue).
- **Tag contents of uploaded files.** Spec: [AUDIO-TAGGING.md](./AUDIO-TAGGING.md);
  the fixtures in `src/services/tagger.test.ts` are the golden cases to port.
- **Background scheduling** (interval, overlap guard, startup tick) and
  **response caching** (3 s queue reads, 5 s session reads).
- **Logging and Sentry reporting.**

**Response cache caveat.** In-process, the suite empties the app's response cache
before each test. It cannot do that for a separate process, so a target that
caches as this API does carries up to 5 seconds of cached session reads from one
test into the next. The suite passes against this API with caching on. If a
target shows a failure that goes away when its caching is off, check this first.
