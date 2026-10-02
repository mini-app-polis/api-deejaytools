# Conformance suite

deejaytools-api's integration suite, driving this service over HTTP
(deejaytools-api ADR-009, Testing point 3). It is the mechanical definition
of done for the rewrite: every route in the contract is called (the route
ledger) and every assertion passes.

Copied from deejaytools-api at `be3fa1b` (`dev`, ADRs 006–009) and adapted
here, because that repo stays frozen:

| Change | Where |
|--------|-------|
| **Schema:** after drizzle's migrations, apply this service's post-baseline migrations (`INTEGRATION_EXTRA_MIGRATIONS`), so the identity tables and seed rows exist (ADR-009 harness change 1) | `harness/global-setup.ts` |
| **Reset:** spare `identity_issuers`, `identity_roles`, `identity_role_scopes` and `schema_migrations` (harness change 2) | `harness/harness.ts` |
| **Admins:** `actor({ admin: true })` also grants `deejaytools-admin` (harness change 3) | `harness/harness.ts` |
| HTTP only: no in-process app, a plain Postgres client for direct writes, and the route table fixed in `harness/routes.ts` (the 70 routes deejaytools-api registers) | `harness/` |
| The two upload tests that swap Drive for in-process stand-ins are dropped; they were already skipped over HTTP. The service's own tests cover that path | `tests/songs-upload.integration.test.ts` |

`drizzle/` is deejaytools-api's migration history, frozen (ADR-008 point 6).
It also feeds `scripts/check_baseline.sh`.

## Running it

Postgres with a local database named `*_test`, then the service pointed at
it with the stand-in Clerk and no background scheduler:

```bash
createdb conformance_test
export DEEJAYTOOLS_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/conformance_test
DEEJAYTOOLS_CLERK_JWKS_URL=http://127.0.0.1:4455/.well-known/jwks.json \
DEEJAYTOOLS_CLERK_ISSUER=https://clerk.integration.test \
TICK_SECRET=conformance-tick \
  uv run uvicorn src.api_deejaytools.main:app --port 3999 &

cd conformance
pnpm install
INTEGRATION_BASE_URL=http://localhost:3999 \
DATABASE_URL=$DEEJAYTOOLS_DATABASE_URL \
INTEGRATION_EXTRA_MIGRATIONS=../migrations \
INTEGRATION_TICK_SECRET=conformance-tick \
  pnpm test
```

The suite drops and rebuilds the schema before it runs, under the running
service; the service survives that (no prepared-statement caching, see
`src/api_deejaytools/database.py`), so it can stay up across runs.

CI runs this in the `test` job. Until every route exists it is allowed to
fail (`continue-on-error`); once it passes, that line comes out and it gates
like everything else.
