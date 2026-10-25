# ADR-0008: Replacement API: authorization through the identity library

**Ported from:** deejaytools-api ADR-0007 on 2026-10-25, when that repository was archived as [legacy-graveyard/deejaytools-api](https://github.com/legacy-graveyard/deejaytools-api/tree/main/docs/decisions). Numbers are shifted by one to follow this repository's ADR-0001; only titles, citations and links were changed. In the body, "this repository" and "this service" mean deejaytools-api.

Date: 2026-10-01

## Status

Accepted

## Context

This API authorizes with a boolean in disguise: `requireAuth` loads the
`users` row for the token's `sub`, and `requireAdmin` checks
`users.role === "admin"`. Two handlers also branch on that column to allow
an admin to act on another user's behalf (`on_behalf_of_user_id` on
`POST /v1/checkins` and `POST /v1/songs/upload/chunk`).

ecosystem-standards rules this out for any new `api-service`:

- **AUTH-003** — every non-public route names one
  `<domain>.<resource>.<action>` scope on its declaration; authority never
  comes from a boolean column; only a short, documented list of routes may
  be authenticated-only.
- **AUTH-004** — the decision itself comes from the shared `identity`
  library (verify, resolve, authorize, emit_audit, once per request), and
  every decision is audited.

api-kaianolevine-com made this move from `wcs_user_profiles.is_admin`
(its migration 023, `auth.py`). The identity store was designed for this
case: migration 023's header says deejaytools-com gets its own instance of
the same shape, sharing the schema and never the rows.

## Decision

### Store and issuer

- Install the identity principal store (the `identity_*` tables, same DDL as
  api-kaianolevine-com migration 023) in the deejaytools database, in a
  migration of the new service (ADR-0009).
- One trusted issuer: `https://clerk.deejaytools.com` with its JWKS URL, in
  `identity_issuers` and in settings (`ClerkIssuer`). No machine issuer:
  there are no machine callers (CD-030 stays exempt, as in ADR-0004).
- The enforcement point name is the repository name.

### Roles

| Role | Granted to | Scopes |
|------|-----------|--------|
| `deejaytools-dancer` | Every signed-in person, on first sync | the dancer scopes below |
| `deejaytools-admin` | Admins, in addition to `deejaytools-dancer` | the admin scopes below |

### Route classification

All 70 routes, from API.md. "Owner" means the handler also restricts rows to
the caller's own, as today; the scope says what *kind* of action is allowed,
the query still says *whose* rows.

**Public** (API-008, documented in the service's auth module):
`GET /health`, `GET /v1/events`, `GET /v1/events/:id`, `GET /v1/sessions`,
`GET /v1/sessions/:id`, `GET /v1/queue/:sessionId/active`,
`GET /v1/queue/:sessionId/waiting`, `POST /v1/feedback`.
The two session reads keep the optional-user behavior (extra fields for a
signed-in caller); a credential there is read, never required.

**Operator:** `GET /internal/tick`, gated by `TICK_SECRET`. Unlike this API,
the new service **fails closed**: unset secret, endpoint refuses.

**Authenticated-only** (verified credential, no scope; circular otherwise):

| Route | Why no scope |
|-------|--------------|
| `POST /v1/auth/sync` | Where a person becomes a principal. It cannot require one. |
| `GET /v1/auth/me` | Reads only the caller's own record, including whether they have a principal yet. |

**Dancer scopes** (`deejaytools-dancer`):

| Scope | Routes |
|-------|--------|
| `deejaytools.profile.write` | `PATCH /v1/auth/me` |
| `deejaytools.entities.read` | `GET /v1/events/:id/entities` |
| `deejaytools.checkins.read` | `GET /v1/checkins/mine` |
| `deejaytools.checkins.write` | `POST /v1/checkins`, `DELETE /v1/checkins/:id` |
| `deejaytools.submissions.read` | `GET /v1/event-song-submissions` |
| `deejaytools.submissions.write` | `POST`, `DELETE /v1/event-song-submissions[/:id]` |
| `deejaytools.partners.read` | `GET /v1/partners`, `GET /v1/partners/leading-pairs`, `GET /v1/partners/:id`, `GET /v1/partners/:id/associations` |
| `deejaytools.partners.write` | `POST`, `PATCH`, `DELETE /v1/partners[/:id]`, `POST /v1/pairs/find-or-create` |
| `deejaytools.teams.read` | `GET /v1/teams` |
| `deejaytools.teams.write` | `POST`, `PATCH`, `DELETE /v1/teams[/:id]` |
| `deejaytools.partnerships.read` | `GET /v1/managed-partnerships` |
| `deejaytools.partnerships.write` | `POST`, `PATCH`, `DELETE /v1/managed-partnerships[/:id]` |
| `deejaytools.songs.read` | `GET /v1/songs`, `GET /v1/songs/:id` |
| `deejaytools.songs.write` | `POST /v1/songs`, `PATCH`, `DELETE /v1/songs/:id`, `POST /v1/songs/upload/chunk` |

**Admin scopes** (`deejaytools-admin`):

| Scope | Routes |
|-------|--------|
| `deejaytools.events.write` | `POST /v1/events`, `PATCH`, `DELETE /v1/events/:id` |
| `deejaytools.sessions.write` | `POST /v1/sessions`, `PUT /v1/sessions/:id/divisions`, `PATCH /v1/sessions/:id/status`, `PATCH`, `DELETE /v1/sessions/:id` |
| `deejaytools.queue.read` | `GET /v1/queue/:sessionId/priority`, `GET /v1/queue/:sessionId/non-priority` |
| `deejaytools.queue.manage` | `POST /v1/queue/promote`, `/complete`, `/incomplete`, `/move-down`, `/withdraw` |
| `deejaytools.runs.read` | `GET /v1/runs` |
| `deejaytools.library.read` | `GET /v1/admin/songs` (every user's songs) |
| `deejaytools.entries.read` | `GET /v1/admin/event-song-submissions` (every submission for an event) |
| `deejaytools.users.read` | `GET /v1/admin/users`, `GET /v1/admin/users/:id/partners`, `GET /v1/admin/users/:id/event-song-submissions` |
| `deejaytools.users.write` | `PATCH /v1/admin/users/:id/role` |
| `deejaytools.drivejobs.read` | `GET /v1/admin/drive-jobs`, `GET /v1/admin/drive-jobs/summary` |
| `deejaytools.drivejobs.write` | `POST /v1/admin/drive-jobs/backfill-renames`, `POST /v1/admin/drive-jobs/:id/retry` |
| `deejaytools.testdata.read` | `GET /v1/admin/checkins/test` |
| `deejaytools.testdata.write` | `POST /v1/admin/checkins`, `DELETE /v1/admin/checkins/test` |
| `deejaytools.delegation.act` | Acting for another user: `on_behalf_of_user_id` on `POST /v1/checkins` and `POST /v1/songs/upload/chunk` (see below) |

That is 8 public, 1 operator, 2 authenticated-only, 30 dancer and 29 admin:
all 70.

### Acting on another user's behalf

The route keeps its dancer scope. When `on_behalf_of_user_id` is present the
handler makes a **second** decision, `deejaytools.delegation.act`, through the
same `identity` authorize-and-audit path, before reading the target user. The
target id is the *subject of the action*, never the caller's identity
(AUTH-003 point 3): the principal still comes only from the credential.

### Provisioning, `USER_NOT_SYNCED`, and `role` on the wire

- `POST /v1/auth/sync` upserts the `users` row as today **and** provisions
  the principal (`deejaytools-dancer`) in the same transaction.
- A verified credential with no principal answers what this API answers
  for a missing `users` row: **401 `USER_NOT_SYNCED`**,
  `Call POST /v1/auth/sync first`.
- The `role` field in `/v1/auth/me`, `/v1/auth/sync` and the admin user list
  is **derived**: `"admin"` if the principal holds `deejaytools-admin`, else
  `"user"`. The web app sees no change.
- `PATCH /v1/admin/users/:id/role` grants or revokes `deejaytools-admin`
  **and** writes `users.role` in the same transaction, so this API still sees
  the same admins if traffic is switched back. The self-demotion guard stays.
- Missing scope answers **403 `FORBIDDEN`**, `Admin access required`, as
  today (ADR-0010). The audit record carries the real scope.

### Backfill

The migration that installs the store (ADR-0009) also:

- inserts the issuer and the two roles with their scopes;
- creates a `human` principal for every `users` row (`subject = users.id`,
  which is already the Clerk `sub`);
- grants `deejaytools-dancer` to all of them, and `deejaytools-admin` to rows
  with `role = 'admin'` (`granted_by = 'migration_backfill'`).

It is idempotent (`ON CONFLICT DO NOTHING`). Anyone who signs up through
this API during a rollback window has a `users` row and no principal; the
backfill is re-run (a manual script with the same SQL) when traffic returns
to the new service. Their next sync would also provision them.

### Ownership (API-006)

Owned tables keep pattern 1: `user_id` holds the Clerk subject, which is the
principal's subject. `events`, `sessions`, `session_divisions`,
`event_division_run_limits`, `queue_entries`, `queue_events`, `runs` and
`drive_jobs` are shared operational data, written only under admin scopes or
by the scheduler. They get an API-006 exemption with that reason, as
api-kaianolevine-com did for its operator-internal table.

## Consequences

- Admin access becomes a grant in the identity store, reviewable and audited,
  instead of a column.
- Every allow and deny is audited. That is a new write on every request.
- `users.role` lives on as a mirror until this API is retired, then becomes
  droppable. Until then, changing admin access through SQL means changing it
  in both places; the admin endpoint does both.
- The scope table is the contract between routes and roles. A new route
  needs a row in it, enforced by the AUTH-003 evaluator check.
- Two scopes are checked inside handlers rather than on the route, for
  delegation only. Both go through the library, so they are audited like
  every other decision.
