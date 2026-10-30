# API endpoint reference

Mounting and global middleware: [`src/api_deejaytools/main.py`](../src/api_deejaytools/main.py) and [`middleware.py`](../src/api_deejaytools/middleware.py). Route handlers: [`src/api_deejaytools/routers/`](../src/api_deejaytools/routers/). The live OpenAPI schema is served at `/openapi.json` (Swagger UI at `/docs`).

Request bodies below are written as Zod schemas: that notation is the validation contract, and `src/api_deejaytools/validation.py`, `zod_types.py` and `zod_coerce.py` implement its semantics (trimming, coercion, messages).

Base URL in production is deployment-specific (Railway). All versioned routes live under `/v1`.

---

## Conventions

### Response envelope

Success responses (`success()` / `success_list()` in `src/api_deejaytools/errors.py`):

```json
{
  "data": { },
  "meta": { "version": "…", "count": 42 }
}
```

- `success(payload)` — single object in `data`; `meta` includes `version`.
- `success_list(array)` — array in `data`; `meta` includes `version` and `count`.

Failure responses:

```json
{
  "error": {
    "code": "BAD_REQUEST",
    "message": "Human-readable explanation"
  }
}
```

Validation failures return **400** with `code: "VALIDATION_ERROR"` and a message summarizing field errors ([ADR-0003](decisions/ADR-0003-validation-envelope-shape.md)).

Unhandled exceptions return **500** `INTERNAL` with a generic message.

### Authentication

Send a Clerk session JWT:

```
Authorization: Bearer <clerk_session_token>
```

Three distinct failure modes on protected routes:

| Status | Code | Meaning |
|--------|------|---------|
| 401 | `UNAUTHORIZED` | Missing `Authorization` header, non-`Bearer` prefix, or JWT verification failed |
| 401 | `USER_NOT_SYNCED` | JWT valid but no row in `users` — client must call `POST /v1/auth/sync` first |
| 403 | `FORBIDDEN` | The caller's principal lacks the route's scope; message **"Admin access required"** |

Every protected route requires one permission scope (`require_scope(...)` in [`src/api_deejaytools/auth.py`](../src/api_deejaytools/auth.py); the route-to-scope table is [ADR-0008](decisions/ADR-0008-replacement-api-identity-authorization.md) and the [README](../README.md#endpoints)). Each route's **Auth** line below names its scope. A scope marked **on the event** is decided for the event the request names (`require_event_scope(...)`): a global role carrying it (`deejaytools-admin`) allows on every event, and a per-event staff role (coordinator, DJ, event manager; [ADR-0012](decisions/ADR-0012-per-event-staff-roles.md)) allows on the event it was granted for only. "Admin" below means a caller holding `deejaytools-admin`.

For a caller who holds a scope only through a per-event staff role, a request that names an event through a session or queue entry the API cannot find answers **403** "Admin access required", not 404: with no event to decide on, the decision falls to roles. An admin gets the 404. This is deliberate: staff cannot use the answer to learn which ids exist.

`POST /v1/auth/sync` and `GET /v1/auth/me` are authenticated-only: a valid JWT, no scope. Sync is where the `users` row and the principal are created.

### Optional-user pattern

`GET /v1/sessions` and `GET /v1/sessions/:id` call `optional_synced_user_id()` ([`src/api_deejaytools/auth.py`](../src/api_deejaytools/auth.py)):

- No token, invalid token, or unsynced user → treated as anonymous; no error.
- Valid token + synced user → extra fields are added.

| Endpoint | Extra fields when authenticated |
|----------|----------------------------------|
| `GET /v1/sessions` | `has_active_checkin: boolean` on each session row |
| `GET /v1/sessions/:id` | `has_active_checkin: boolean`, `active_checkin_division: string` when in queue |

Anonymous responses **omit** these keys entirely (not `null`).

### Rate limiting and timeouts

Applied in [`src/api_deejaytools/middleware.py`](../src/api_deejaytools/middleware.py):

| Middleware | Scope | Limit |
|------------|-------|-------|
| Body limit | `*` | 11 MiB max body; **413** `payload_too_large` |
| Rate limit | `/v1/*` only | 300 requests / minute per client address (the first `X-Forwarded-For` hop); **429** `too_many_requests` |
| Deadline | `/v1/*` only | **30 s** default; **300 s** for paths starting with `/v1/songs/upload/`; **503** `request_timeout` |

**Exempt from rate limiting and the deadline:** `/health`, `/version`, `/internal/tick` (unversioned).

CORS: origins from `DEEJAYTOOLS_CORS_ORIGINS` (comma-separated), default `http://localhost:5173`. Allowed headers: `Authorization`, `Content-Type`.

### Frontend polling intervals

| Page | Interval | Endpoints touched |
|------|----------|-------------------|
| `SessionDetailPage` | **60 s** | `GET /v1/sessions/:id`, `GET /v1/queue/:id/active`, `GET /v1/queue/:id/waiting` |
| `ManagerPage` (Active Sessions tab) | **8 s** | `GET /v1/queue/:sessionId/active`, `…/priority`, `…/non-priority` |
| `SessionInfoHeader` | 30 s | Local clock only (no API) |
| `FloorTrialsPage` | none | Loads `GET /v1/sessions` + `GET /v1/events` once on mount |

Queue read responses are cached server-side for **3 s** (`QUEUE_TTL_SECONDS` in `src/api_deejaytools/cache.py`); session list/detail shared data for **5 s** (`SESSION_TTL_SECONDS`).

### Shared error rows

Many authenticated endpoints also return:

In this table, the third column is the **trigger** (when the error fires), not always the verbatim JSON `error.message`. Per-endpoint tables below use the **Message** column for exact text where it is fixed.

| Status | Code | Trigger |
|--------|------|---------|
| 401 | `UNAUTHORIZED` | Missing/invalid token |
| 401 | `USER_NOT_SYNCED` | Valid token, no principal (not yet synced) |
| 403 | `FORBIDDEN` | The caller lacks the route's scope (on the event, where event-scoped) |
| 400 | `VALIDATION_ERROR` | Body or query validation failed |
| 429 | `too_many_requests` | Rate limit exceeded. Please slow down. |
| 503 | `request_timeout` | Handler exceeded deadline |

Unless noted, assume these apply to every scoped endpoint below.

---

## App-level routes (`routers/meta.py`, `routers/internal.py`)

### GET /health

**Auth:** public

**Purpose:** Liveness probe for Railway and uptime monitors. No database query: it answers while the process serves. `HEAD /health` answers the same without a body.

**Path params / Query params:** none

**Request body:** none

**Response:** Not wrapped in `{ data, meta }`.

```json
{ "status": "ok" }
```

Always **200**; there are no error responses.

---

### GET /version

**Auth:** public

**Purpose:** The running build: package version and the commit the deploy was built from. Called by the post-deploy smoke test (`.github/workflows/deployed.yml`, which waits for `commit` to be the deploy it was triggered by) and by the web app's `useApiVersion` hook.

**Path params / Query params:** none

**Request body:** none

**Response:** Not wrapped in `{ data, meta }`.

```json
{ "version": "…", "commit": "…" }
```

`commit` is `RAILWAY_GIT_COMMIT_SHA`, `null` outside Railway.

**Errors:** none.

---

### GET /internal/tick

**Auth:** `x-tick-secret` header equal to `TICK_SECRET` (or `TICK_SECRET_PREVIOUS` while a rotation overlaps), compared in constant time

**Purpose:** Manual trigger for one scheduler pass (`run_tick()` in `services/scheduler.py`): session status advance, active-queue auto-fill, starting due song builds in the background, then one batch of the Drive job queue ([DRIVE.md](./DRIVE.md#processing-process_drive_jobs-once-per-tick)). Responds after the pass finishes; errors inside the pass are logged, not returned.

**Path params / Query params:** none

**Request body:** none

**Response:**

```json
{
  "data": { "ticked": true },
  "meta": { "version": "…" }
}
```

**Errors:**

| Status | Code | Message | Trigger |
|--------|------|---------|---------|
| 403 | `FORBIDDEN` | Admin access required | `x-tick-secret` missing or matching neither secret, or `TICK_SECRET` unset |

When `TICK_SECRET` is **unset** (an empty value counts as unset), the route refuses every call; `TICK_SECRET_PREVIOUS` alone never opens it.

---

## `routers/auth.py` — `/v1/auth`

### POST /v1/auth/sync

**Auth:** authenticated-only — valid Bearer JWT, no scope (creates the user if missing)

**Purpose:** Upsert the Clerk user into `users` after first sign-in, and provision their principal with the `deejaytools-dancer` role, in one transaction.

**Path params / Query params:** none

**Request body:**

```typescript
const syncBody = z.object({
  email: z.string().email(),
  firstName: z.string().optional(),
  lastName: z.string().optional(),
  displayName: z.string().optional(),
});
```

**Response:**

```json
{
  "data": {
    "id": "user_2abc…",
    "email": "dancer@example.com",
    "display_name": null,
    "first_name": "Ada",
    "last_name": "Lovelace",
    "role": "user",
    "created_at": 1710000000000,
    "updated_at": 1710000000000
  },
  "meta": { "version": "…" }
}
```

`role` is `admin` when the caller's principal holds `deejaytools-admin`, else `user`; it is derived from the identity store, not stored on `users`.

**Errors:**

| Status | Code | Message | Trigger |
|--------|------|---------|---------|
| 400 | `VALIDATION_ERROR` | … | Invalid JSON body (checked before the token) |
| 401 | `UNAUTHORIZED` | Authentication required | Missing or invalid JWT |
| 409 | `EMAIL_BELONGS_TO_ANOTHER_ACCOUNT` | This email address is already linked to a different sign-in. Contact an organizer to have your account moved to this sign-in. | The email is held by another `users` row |
| 500 | `INTERNAL` | Failed to load user | User row missing after upsert |

**Email:** the stored email is the session token's email claim (`email`, then `primary_email_address`, then `email_address`). The body's `email` is used only when the token carries none (and that fallback is logged as `auth_sync_email_from_body`); it is still required and validated.

On conflict, only `email` and `updated_at` are updated — `firstName` / `lastName` / `displayName` are **not** overwritten.

---

### GET /v1/auth/me

**Auth:** authenticated-only — valid Bearer JWT, no scope

**Purpose:** Return the authenticated user's profile.

**Path params / Query params:** none

**Request body:** none

**Response:** Same shape as `POST /sync` `data`, plus `event_roles`: the caller's per-event staff roles, `[{ "event_id": "…", "role": "coordinator" | "dj" | "event-manager" }]`, by event, empty when none ([ADR-0012](./decisions/ADR-0012-per-event-staff-roles.md)). `role` stays `user` or `admin`.

**Errors:**

| Status | Code | Message | Trigger |
|--------|------|---------|---------|
| 401 | `USER_NOT_SYNCED` | Call POST /v1/auth/sync first | Valid token, no `users` row |

---

### PATCH /v1/auth/me

**Auth:** `deejaytools.profile.write`

**Purpose:** Update the caller's first and last name.

**Path params / Query params:** none

**Request body:**

```typescript
const updateProfileBody = z.object({
  firstName: z.string().trim().min(1).max(100),
  lastName: z.string().trim().min(1).max(100),
});
```

**Response:** Same shape as `POST /sync` `data` — **without** `event_roles`.

**Errors:**

| Status | Code | Message | Trigger |
|--------|------|---------|---------|
| 404 | `NOT_FOUND` | User not found | Missing row |
| 500 | `INTERNAL` | … | DB update failed |

---

## `routers/events.py` — `/v1/events`

Helper schemas used by create/patch (same file):

```typescript
const dateString = z.string().regex(/^\d{4}-\d{2}-\d{2}$/, "Must be YYYY-MM-DD");

const ianaTimezone = z
  .string()
  .min(1)
  .refine(
    (tz) => {
      try {
        Intl.DateTimeFormat(undefined, { timeZone: tz });
        return true;
      } catch {
        return false;
      }
    },
    { message: "Must be a valid IANA timezone (e.g. 'America/Chicago')" }
  );
```

### GET /v1/events

**Auth:** public

**Purpose:** List all events, newest `start_date` first.

**Path params / Query params:** none

**Request body:** none

**Response:**

```json
{
  "data": [
    {
      "id": "evt_…",
      "name": "Rising Star Classic 2026",
      "start_date": "2026-03-01",
      "end_date": "2026-03-03",
      "timezone": "America/Chicago",
      "season_year": "2026",
      "status": "upcoming",
      "created_by": "user_…",
      "created_at": 1710000000000,
      "updated_at": 1710000000000
    }
  ],
  "meta": { "version": "…", "count": 1 }
}
```

`status` is derived at response time (`upcoming` | `active` | `completed`), not stored. `season_year` is the stored season, or derived from `start_date` (October 1 rollover, [ADR-0011](decisions/ADR-0011-season-year-rolls-over-october-1.md)) when the row has none; never `null`.

**Errors:** none (empty list is valid).

---

### GET /v1/events/:id

**Auth:** public

**Purpose:** Fetch one event by id.

**Path params:** `id` — event UUID

**Query params:** none

**Request body:** none

**Response:** Single event object (same fields as list item) in `data`.

**Errors:**

| Status | Code | Message | Trigger |
|--------|------|---------|---------|
| 404 | `NOT_FOUND` | Event not found | Unknown id |

---

### GET /v1/events/:id/entities

**Auth:** `deejaytools.entities.read`

**Purpose:** List the competing entities that have at least one song submitted to
this event, grouped by division — the read behind the "Entered" section of the
event page.

Unlike the event itself this is **not public**: who is entered is
competitor-visible information. The response carries **no song identity** — no
title, routine name, filename or song id — by design. Multiple songs from the
same entity in the same division collapse into `song_count`.

Entities are grouped by song entity (managed partnership → partner →
solo owner). A division comes from the submission's per-event override when set,
otherwise from the song; submissions with neither fall into `"Unspecified"`.

**Path params:** `id` — event UUID

**Query params / Request body:** none

**Response:**

```json
{
  "data": [
    {
      "division": "Classic",
      "entities": [
        { "entity_key": "pt:…", "label": "Alex Kim & Jo Ruiz", "song_count": 2 },
        { "entity_key": "us:…", "label": "Sam Lee", "song_count": 1 }
      ]
    },
    {
      "division": "Teams",
      "entities": [{ "entity_key": "pt:…", "label": "team2026", "song_count": 1 }]
    }
  ],
  "meta": { "version": "…", "count": 2 }
}
```

`entity_key` is prefixed by source table (`mp:` managed partnership, `pt:`
partner, `us:` solo user) so ids from different tables can never collide.
Divisions are in `DIVISIONS` display order, with unknown names and
`"Unspecified"` last; entities within a division sort by `label`.

An entity entered in two divisions appears once in each — the same competitor,
two rows. Callers wanting a headcount should count distinct `entity_key`s rather
than summing group sizes.

**Errors:**

| Status | Code | Message | Trigger |
|--------|------|---------|---------|
| 401 | `UNAUTHORIZED` | … | Missing/invalid token |
| 404 | `NOT_FOUND` | Event not found | Unknown id |
| 500 | `INTERNAL` | … | Query failed |

---

### POST /v1/events

**Auth:** `deejaytools.events.write`, through a role only: a new event names no event to hold a grant on, so an event manager cannot create events

**Purpose:** Create an event.

**Path params / Query params:** none

**Request body:**

```typescript
const createEvent = z.object({
  name: z.string().min(1),
  start_date: dateString,
  end_date: dateString,
  /** IANA timezone for this event. All session times are displayed in this zone. */
  timezone: ianaTimezone.default("America/Chicago"),
  /** Omit to derive from start_date (October 1 rollover). */
  season_year: z.string().regex(/^\d{4}$/, "Must be a 4-digit year").optional(),
});
```

**Response:** **201** — created event in `data`.

**Errors:**

| Status | Code | Message | Trigger |
|--------|------|---------|---------|
| 400 | `BAD_REQUEST` | start_date must be on or before end_date | Date ordering |
| 400 | `VALIDATION_ERROR` | … | Invalid body |

---

### PATCH /v1/events/:id

**Auth:** `deejaytools.events.write` on the event in the path: held through a role (an admin's, on any event) or granted on the event (its event manager). An unknown id, through a role only

**Purpose:** Partially update an event. `season_year` is not recomputed when `start_date` changes. A change that moves the event's Drive folder (`<root>/<season_year>/Events/<name>/`: its name or season) queues a `share_event_folder` Drive job for each of the event's DJs and event managers, so they can read the new folder ([DRIVE.md](./DRIVE.md#job-kinds)).

**Path params:** `id`

**Query params:** none

**Request body:**

```typescript
const patchEvent = z.object({
  name: z.string().min(1).optional(),
  start_date: dateString.optional(),
  end_date: dateString.optional(),
  timezone: ianaTimezone.optional(),
  season_year: z.string().regex(/^\d{4}$/, "Must be a 4-digit year").optional(),
});
```

**Response:** Updated event in `data`.

**Errors:**

| Status | Code | Message | Trigger |
|--------|------|---------|---------|
| 404 | `NOT_FOUND` | Event not found | Unknown id |
| 400 | `BAD_REQUEST` | start_date must be on or before end_date | Combined dates invalid |

---

### DELETE /v1/events/:id

**Auth:** `deejaytools.events.write` on the event in the path: held through a role (an admin's, on any event) or granted on the event (its event manager). An unknown id, through a role only

**Purpose:** Delete an event and cascade-delete its sessions and all floor-trial data, its song submissions, its run limits, and its staff roles with their explicit grants (queueing an `unshare_event_folder` Drive job for each DJ and event manager). After commit, a `trash` Drive job is enqueued for each submission's event copy ([DRIVE.md](./DRIVE.md#where-jobs-are-enqueued)).

**Path params:** `id`

**Query params:** none

**Request body:** none

**Response:**

```json
{ "data": { "deleted": true }, "meta": { "version": "…" } }
```

**Errors:**

| Status | Code | Message | Trigger |
|--------|------|---------|---------|
| 404 | `NOT_FOUND` | Event not found | Unknown id |

---

## Event staff — `/v1/events/:id/staff`

Per-event staff roles ([ADR-0012](./decisions/ADR-0012-per-event-staff-roles.md)). A role is `coordinator`, `dj` or `event-manager`, held on one event. These routes are event-scoped: allowed by the scope held through a role (an admin's, on any event) or granted on the event in the path.

A staff grant on the wire:

```json
{
  "event_id": "…",
  "user_id": "user_2abc…",
  "role": "dj",
  "email": "dj@example.com",
  "first_name": "Ada",
  "last_name": "Lovelace",
  "granted_by": "user_2admin…",
  "granted_at": 1710000000000
}
```

### GET /v1/events/:id/staff

**Auth:** `deejaytools.staff.read` on the event

**Purpose:** The event's staff, ordered by role, then oldest grant first.

**Response:** `success_list` of staff grants. `email` is masked (`k•••@example.com`: the first character of the local part, then `•••@` and the whole domain) for every caller but an admin; admins see it whole.

**Errors:**

| Status | Code | Message | Trigger |
|--------|------|---------|---------|
| 403 | `FORBIDDEN` | Admin access required | Scope not held on this event |
| 404 | `NOT_FOUND` | Event not found | Unknown id |

---

### PUT /v1/events/:id/staff/:user_id/:role

**Auth:** `deejaytools.staff.write` on the event. Granting `event-manager` needs the scope through a role (admin).

**Purpose:** Grant the role on the event: the staff row plus the role's scopes as explicit grants on `event:<id>`. A person's first `dj` or `event-manager` role on the event also queues a `share_event_folder` Drive job, which shares the event's Drive folder with their email as a reader ([DRIVE.md](./DRIVE.md#job-kinds)). Idempotent: a grant already held is returned unchanged.

**Request body:** none

**Response:** `success` with the staff grant; `email` masked for non-admin callers, as in `GET`.

**Errors:**

| Status | Code | Message | Trigger |
|--------|------|---------|---------|
| 400 | `VALIDATION_ERROR` | … | `role` is not one of the three |
| 403 | `FORBIDDEN` | Admin access required | Scope not held on this event, or `event-manager` without it through a role |
| 404 | `NOT_FOUND` | Event not found | Unknown event id |
| 404 | `NOT_FOUND` | User not found | No `users` row for `user_id` |

---

### DELETE /v1/events/:id/staff/:user_id/:role

**Auth:** as `PUT`.

**Purpose:** Revoke the role on the event, with each of its explicit grants no other role the person still holds on the event carries. Revoking their last `dj` or `event-manager` role on the event queues an `unshare_event_folder` Drive job. Idempotent.

**Response:**

```json
{ "data": { "deleted": true }, "meta": { "version": "…" } }
```

**Errors:** as `PUT`.

---

### GET /v1/events/:id/drive-folder

The event's shared Drive folder, for the people it is shared with ([ADR-0012](./decisions/ADR-0012-per-event-staff-roles.md), "The DJ's Drive folder").

**Auth:** `deejaytools.queue.read` on the event: the event's DJs and event managers (granted on the event in the path) and admins (through their role). A coordinator or a dancer does not hold it.

**Purpose:** The link the DJ portal's Drive tab opens. The folder is the one recorded in `file_id` by the newest completed `share_event_folder` job for the event ([DRIVE.md](./DRIVE.md#job-kinds)); a rename moves the folder and queues a new share, so the newest completed one is current.

**Path params:** `id` — event id

**Request body:** none

**Response:**

```json
{
  "data": {
    "folder_id": "1AbC…",
    "url": "https://drive.google.com/drive/folders/1AbC…"
  },
  "meta": { "version": "…" }
}
```

`folder_id` and `url` are both `null` until a share has recorded a folder.

**Errors:**

| Status | Code | Message | Trigger |
|--------|------|---------|---------|
| 401 | `UNAUTHORIZED` | … | Missing or invalid token |
| 403 | `FORBIDDEN` | Admin access required | Scope not held on this event |
| 404 | `NOT_FOUND` | Event not found | Unknown id |

---

## `routers/sessions.py` — `/v1/sessions`

Shared schemas:

```typescript
const divisionItemSchema = z.object({
  division_name: z.string(),
  is_priority: z.boolean().optional(),
  sort_order: z.number().int().optional(),
  priority_run_limit: z.number().int().min(0).optional(),
});

const createSessionBody = z.object({
  event_id: z.string().optional(),
  name: z.string().min(1),
  date: z.string().optional(),
  checkin_opens_at: z.number(),
  floor_trial_starts_at: z.number(),
  floor_trial_ends_at: z.number(),
  active_priority_max: z.number().int().min(0).optional(),
  active_non_priority_max: z.number().int().min(0).optional(),
  divisions: z.array(divisionItemSchema),
});

const patchSessionBody = z.object({
  name: z.string().min(1).optional(),
  date: z.string().nullable().optional(),
  event_id: z.string().nullable().optional(),
  checkin_opens_at: z.number().optional(),
  floor_trial_starts_at: z.number().optional(),
  floor_trial_ends_at: z.number().optional(),
  active_priority_max: z.number().int().min(0).optional(),
  active_non_priority_max: z.number().int().min(0).optional(),
});

const putDivisionsBody = z.object({
  divisions: z.array(divisionItemSchema),
});

const listQuery = z.object({
  event_id: z.string().optional(),
});

const statusBody = z.object({
  status: SessionStatusSchema,
});
```

Session status values:

```typescript
export const SessionStatusSchema = z.enum([
  "scheduled",
  "checkin_open",
  "in_progress",
  "completed",
  "cancelled",
]);
```

### GET /v1/sessions

**Auth:** optional-user

**Purpose:** List sessions with divisions, queue depths, and optional per-user check-in flag.

**Path params:** none

**Query params:**

| Param | Type | Notes |
|-------|------|-------|
| `event_id` | string | Optional filter |

**Request body:** none

**Response:**

```json
{
  "data": [
    {
      "id": "sess_…",
      "event_id": "evt_…",
      "name": "Saturday AM Floor Trials",
      "date": "2026-03-01",
      "checkin_opens_at": 1710000000000,
      "floor_trial_starts_at": 1710003600000,
      "floor_trial_ends_at": 1710010800000,
      "active_priority_max": 6,
      "active_non_priority_max": 4,
      "status": "checkin_open",
      "created_by": "user_…",
      "created_at": 1710000000000,
      "event_timezone": "America/Chicago",
      "divisions": [
        {
          "id": "…",
          "division_name": "Classic",
          "is_priority": true,
          "sort_order": 0,
          "priority_run_limit": 2
        }
      ],
      "queue_depth": { "priority": 3, "non_priority": 1, "active": 2 },
      "has_active_checkin": true
    }
  ],
  "meta": { "version": "…", "count": 1 }
}
```

`status` is derived from wall clock unless DB status is `cancelled`. `has_active_checkin` only when authenticated.

**Errors:**

| Status | Code | Message | Trigger |
|--------|------|---------|---------|
| 400 | `VALIDATION_ERROR` | … | Invalid query |

---

### GET /v1/sessions/:id

**Auth:** optional-user

**Purpose:** Fetch one session with event name, divisions, queue depth, optional user check-in state.

**Path params:** `id`

**Query params:** none

**Request body:** none

**Response:** Session object as in list, plus:

```json
{
  "event_name": "Rising Star Classic 2026",
  "has_active_checkin": true,
  "active_checkin_division": "Classic"
}
```

**Errors:**

| Status | Code | Message | Trigger |
|--------|------|---------|---------|
| 404 | `NOT_FOUND` | Session not found | Unknown id |

---

### POST /v1/sessions

**Auth:** `deejaytools.sessions.write` on the body's `event_id`: held through a role (an admin's, on any event) or granted on the event (its DJ or event manager). Without `event_id`, through a role only

**Purpose:** Create a session and its division rows.

**Path params / Query params:** none

**Request body:** `createSessionBody` (above)

**Response:** **201** — full session + `divisions` + `queue_depth` in `data`.

**Errors:**

| Status | Code | Message | Trigger |
|--------|------|---------|---------|
| 400 | `BAD_REQUEST` | Missing or invalid required fields… | Zero/negative timestamps or empty name |
| 400 | `BAD_REQUEST` | floor_trial_starts_at must be after checkin_opens_at | Time ordering |
| 400 | `BAD_REQUEST` | floor_trial_ends_at must be after floor_trial_starts_at | Time ordering |
| 400 | `BAD_REQUEST` | active_non_priority_max must be <= active_priority_max | Cap constraint |
| 400 | `BAD_REQUEST` | Session floor-trial window overlaps… | Overlap in same event |
| 400 | `BAD_REQUEST` | Session starts (…) before event start date… | Outside event date range |

---

### PUT /v1/sessions/:id/divisions

**Auth:** `deejaytools.sessions.write` on the session's event: held through a role (an admin's, on any event) or granted on the event (its DJ or event manager). A session with no event, or an unknown id, through a role only

**Purpose:** Upsert session divisions (priority flags and run limits).

**Path params:** `id`

**Query params:** none

**Request body:** `putDivisionsBody`

**Response:** Updated session + divisions + queue_depth.

**Errors:**

| Status | Code | Message | Trigger |
|--------|------|---------|---------|
| 404 | `NOT_FOUND` | Session not found | Unknown id |
| 500 | `DIVISIONS_UPDATE_FAILED` | Failed to update divisions | Transaction failed (always this message; the cause is logged, never returned) |

---

### PATCH /v1/sessions/:id/status

**Auth:** `deejaytools.sessions.write` on the session's event: held through a role (an admin's, on any event) or granted on the event (its DJ or event manager). A session with no event, or an unknown id, through a role only

**Purpose:** Manually set persisted session status (e.g. `cancelled`).

**Path params:** `id`

**Query params:** none

**Request body:** `statusBody`

**Response:** Updated session + divisions + queue_depth.

**Errors:**

| Status | Code | Message | Trigger |
|--------|------|---------|---------|
| 404 | `NOT_FOUND` | Session not found | Unknown id |

---

### PATCH /v1/sessions/:id

**Auth:** `deejaytools.sessions.write` on the session's event: held through a role (an admin's, on any event) or granted on the event (its DJ or event manager). Changing `event_id` also needs the scope on the event it moves to (`null`, out of every event: through a role only); both decisions are audited

**Purpose:** Update session metadata and time window.

**Path params:** `id`

**Query params:** none

**Request body:** `patchSessionBody`

**Response:** Updated session + divisions + queue_depth.

**Errors:** Same validation family as `POST /v1/sessions` plus 404 not found.

---

### DELETE /v1/sessions/:id

**Auth:** `deejaytools.sessions.write` on the session's event: held through a role (an admin's, on any event) or granted on the event (its DJ or event manager). A session with no event, or an unknown id, through a role only

**Purpose:** Delete session and all queue/check-in/run data.

**Path params:** `id`

**Query params:** none

**Request body:** none

**Response:** `{ "data": { "deleted": true }, … }`

**Errors:**

| Status | Code | Message | Trigger |
|--------|------|---------|---------|
| 404 | `NOT_FOUND` | Session not found | Unknown id |

---

## `routers/checkins.py` — `/v1/checkins`

### POST /v1/checkins

**Auth:** `deejaytools.checkins.write`. `on_behalf_of_user_id` also needs `deejaytools.delegation.act` on the session's event: through a role (admins, for anyone) or granted on that event (its DJ or event manager), and then only for a person **connected to that event** — on a song submitted to it (submitter, song owner, or the account linked to the song's partner), checked in at or queued in one of its sessions (solo dancer, either side of the pair, or the managed partnership's owner), or on its staff. For anyone else the decision falls back to roles alone, so a DJ or event manager gets 403

**Purpose:** Check in an entity for a session; creates `checkins`, `queue_entries`, and `queue_events` rows.

A song shared with the caller as its linked partner may be checked in too, unless it belongs to a managed partnership. Its entity is the owner's pair with that partner (created if missing), whichever partner checks in, so the two cannot both hold an entry; `entityPairId` must name that pair or one the caller leads.

A couple holds one live entry per session, whichever partner's pair it is. A partner row stands for an account when its `linked_user_id` is that account or, with no link, when its `email` matches the account's `users.email` (trimmed, case-insensitive). While a pair led by A whose partner row stands for B has a live entry in the session, a pair led by B whose partner row stands for A is refused with the single-entry 409, and the other way round. This applies to every check-in that ends with a pair entity (own pair, shared song, on behalf). The email match is used for this rule only, never for access to songs or check-ins.

**Path params / Query params:** none

**Request body:**

```typescript
export const createCheckinBodySchema = z
  .object({
    sessionId: z.string().min(1),
    divisionName: z.string().min(1),
    entityPairId: z.string().nullish(),
    entitySoloUserId: z.string().nullish(),
    entityManagedPartnershipId: z.string().nullish(),
    on_behalf_of_user_id: z.string().trim().nullish(),
    songId: z.string().min(1),
    notes: z.string().nullish(),
  })
  .refine(
    (b) => {
      const entityCount = [b.entityPairId, b.entitySoloUserId, b.entityManagedPartnershipId].filter(
        Boolean
      ).length;
      // On-behalf check-ins let the server derive the entity from the song.
      if (b.on_behalf_of_user_id) return entityCount === 0;
      return entityCount === 1;
    },
    { message: "Exactly one entity must be provided (or none when checking in on behalf)" }
  );
```

**Response:** **201**

```json
{
  "data": {
    "id": "chk_…",
    "sessionId": "sess_…",
    "divisionName": "Classic",
    "initialQueue": "priority"
  },
  "meta": { "version": "…" }
}
```

**Errors:**

| Status | Code | Message | Trigger |
|--------|------|---------|---------|
| 403 | `FORBIDDEN` | Admin access required | `on_behalf_of_user_id` without `deejaytools.delegation.act` on the session's event, or for a person not connected to that event (unless held through a role) |
| 400 | `BAD_REQUEST` | Target user not found | Invalid on-behalf user |
| 404 | `NOT_FOUND` | Session not found | |
| 400 | `BAD_REQUEST` | Check-in has not opened yet | Before `checkin_opens_at` |
| 400 | `BAD_REQUEST` | Check-in is closed for this session | After `floor_trial_ends_at` |
| 404 | `NOT_FOUND` | Song not found | Neither owned nor shared with the caller / soft-deleted |
| 400 | `BAD_REQUEST` | Managed partnership not found | Invalid managed song |
| 400 | `BAD_REQUEST` | Pair not found / You are not a member… | Invalid pair |
| 400 | `BAD_REQUEST` | This song is not associated with a managed partnership | `entityManagedPartnershipId` sent incorrectly |
| 400 | `BAD_REQUEST` | You may only submit a solo check-in for yourself | Solo user mismatch |
| 409 | `conflict` | This entity already has a live queue entry… | Single-entry rule, or the couple rule (the other partner's pair for the same couple has one) |
| 400 | `BAD_REQUEST` | This song hasn't been submitted to this event… | Missing `event_song_submissions` row |
| 400 | `BAD_REQUEST` | Session not found / Division not configured… | Admission context |
| 409 | `conflict` | Check-in conflicted with concurrent activity… | Transaction failure |

---

### GET /v1/checkins/mine

**Auth:** `deejaytools.checkins.read`

**Purpose:** List the caller's active check-ins (must have live `queue_entries` row): solo, pairs they lead, managed partnerships they own, and pairs led by another user whose partner row in that leader's own address book has `linked_user_id` = the caller (the link only, not the email). Queue position, overall position and run count are computed the same way for all of them.

`canWithdraw` is true where the caller is the solo user, leads the pair or owns the managed partnership, and false for a linked partner's view of the leader's pair: only those may withdraw (`DELETE /v1/checkins/:id` answers 403 otherwise).

**Path params / Query params:** none

**Request body:** none

**Response:** Array in `data` (uses `success()`, not `success_list` — no `meta.count`):

```json
{
  "data": [
    {
      "id": "chk_…",
      "sessionId": "sess_…",
      "eventName": "RSC 2026",
      "sessionName": "Saturday AM",
      "sessionFloorTrialStartsAt": 1710003600000,
      "sessionStatus": "in_progress",
      "eventTimezone": "America/Chicago",
      "divisionName": "Classic",
      "entityLabel": "Ada Lovelace & Bob Jones",
      "songDisplayName": "Our Routine",
      "songProcessedFilename": "Ada_Bob_Classic_2026_Routine_v01.mp3",
      "notes": null,
      "checkedInAt": 1710004000000,
      "queueEntryId": "qe_…",
      "queueType": "priority",
      "queuePosition": 2,
      "overallPosition": 4,
      "runCount": 0,
      "canWithdraw": true
    }
  ],
  "meta": { "version": "…" }
}
```

**Errors:** none (empty array valid).

---

### DELETE /v1/checkins/:id

**Auth:** `deejaytools.checkins.write`

**Purpose:** Self-service withdraw (same effect as admin withdraw).

**Path params:** `id` — check-in id

**Query params:** none

**Request body:** none

**Response:**

```json
{ "data": { "withdrawn": true }, "meta": { "version": "…" } }
```

**Errors:**

| Status | Code | Message | Trigger |
|--------|------|---------|---------|
| 404 | `NOT_FOUND` | Check-in not found | No row or no live queue entry, or the entry left the queue (ran, withdrew) before the session lock was taken |
| 403 | `FORBIDDEN` | Admin access required | Not entity owner |
| 409 | `conflict` | Withdraw conflicted with concurrent activity… | Transaction failure |

---

## `routers/queue.py` — `/v1/queue`

Schemas:

```typescript
const promoteBody = z.object({ queueEntryId: z.string().min(1) });
const entryActionBody = z.object({
  queueEntryId: z.string().min(1),
  reason: z.string().nullish(),
});
const withdrawBody = z.object({
  queueEntryId: z.string().min(1),
  reason: z.string().nullish(),
});
```

Queue entry item shape (all GET queue endpoints; `notes` is always `null` on the two public reads, `active` and `waiting`):

```json
{
  "queueEntryId": "qe_…",
  "checkinId": "chk_…",
  "position": 1,
  "enteredQueueAt": 1710004000000,
  "entityPairId": "pair_…",
  "entitySoloUserId": null,
  "entityManagedPartnershipId": null,
  "entityLabel": "Ada Lovelace & Bob Jones",
  "divisionName": "Classic",
  "songId": "song_…",
  "songDisplayName": "Our Routine",
  "songProcessedFilename": "Ada_Bob_Classic_2026_Routine_v01.mp3",
  "notes": null,
  "initialQueue": "priority",
  "checkedInAt": 1710004000000
}
```

### POST /v1/queue/promote

**Auth:** `deejaytools.queue.manage` on the event of the body's `queueEntryId`: held through a role (an admin's, on any event) or granted on the event (its DJ or event manager). An unknown entry, through a role only

**Purpose:** Move a priority or non-priority entry into the active queue.

**Path params / Query params:** none

**Request body:** `promoteBody`

**Response:** `{ "data": { "promoted": true }, … }`

**Errors:**

| Status | Code | Message | Trigger |
|--------|------|---------|---------|
| 404 | `NOT_FOUND` | Queue entry not found | |
| 400 | `BAD_REQUEST` | Entry is already active | |
| 404 | `NOT_FOUND` | Session not found | Inside transaction |
| 400 | `BAD_REQUEST` | Active queue is at its priority cap (N/M active). | Priority cap |
| 400 | `BAD_REQUEST` | Active queue is at its standard cap… | Non-priority cap |
| 400 | `BAD_REQUEST` | Cannot promote a standard entry while the priority queue has N waiting… | Priority queue non-empty |
| 409 | `conflict` | Promotion conflicted with concurrent activity… | |

---

### POST /v1/queue/complete

**Auth:** `deejaytools.queue.manage` on the event of the body's `queueEntryId`: held through a role (an admin's, on any event) or granted on the event (its DJ or event manager). An unknown entry, through a role only

**Purpose:** Mark active slot complete; record a `runs` row.

**Path params / Query params:** none

**Request body:** `entryActionBody`

**Response:** `{ "data": { "completed": true }, … }`

**Errors:**

| Status | Code | Message | Trigger |
|--------|------|---------|---------|
| 400 | `BAD_REQUEST` | Active queue entry not found | |
| 404 | `NOT_FOUND` | Check-in not found | |
| 409 | `conflict` | Completion conflicted with concurrent activity… | |

---

### POST /v1/queue/incomplete

**Auth:** `deejaytools.queue.manage` on the event of the body's `queueEntryId`: held through a role (an admin's, on any event) or granted on the event (its DJ or event manager). An unknown entry, through a role only

**Purpose:** Rotate active slot-1 entry to bottom of active queue.

**Path params / Query params:** none

**Request body:** `entryActionBody`

**Response:** `{ "data": { "rotated": true }, … }`

**Errors:**

| Status | Code | Message | Trigger |
|--------|------|---------|---------|
| 400 | `BAD_REQUEST` | Active queue entry not found | |
| 409 | `conflict` | Rotation conflicted with concurrent activity… | |

---

### POST /v1/queue/move-down

**Auth:** `deejaytools.queue.manage` on the event of the body's `queueEntryId`: held through a role (an admin's, on any event) or granted on the event (its DJ or event manager). An unknown entry, through a role only

**Purpose:** Swap entry with the one at `position + 1` within the same queue.

**Path params / Query params:** none

**Request body:**

```typescript
z.object({ queueEntryId: z.string().min(1) })
```

**Response:** `{ "data": { "moved": true }, … }`

**Errors:**

| Status | Code | Message | Trigger |
|--------|------|---------|---------|
| 404 | `NOT_FOUND` | Queue entry not found | |
| 400 | `BAD_REQUEST` | Entry is already at the bottom of its queue | |
| 409 | `conflict` | Move conflicted with concurrent activity… | |

---

### POST /v1/queue/withdraw

**Auth:** `deejaytools.queue.manage` on the event of the body's `queueEntryId`: held through a role (an admin's, on any event) or granted on the event (its DJ or event manager). An unknown entry, through a role only

**Purpose:** Remove any queue entry without recording a run.

**Path params / Query params:** none

**Request body:** `withdrawBody`

**Response:** `{ "data": { "withdrawn": true }, … }`

**Errors:**

| Status | Code | Message | Trigger |
|--------|------|---------|---------|
| 404 | `NOT_FOUND` | Queue entry not found | Unknown id, or the entry left the queue (ran, withdrew) before the session lock was taken |
| 409 | `conflict` | Withdraw conflicted with concurrent activity… | |

---

### GET /v1/queue/:sessionId/active

**Auth:** public

**Purpose:** List active-queue entries (position 1 = currently running), for the dancer-facing session page.

**Path params:** `sessionId`

**Query params:** none

**Request body:** none

**Response:** Array of queue entry objects in `data`, with `notes` always `null`: check-in notes are for the event's staff, who read them through `GET /v1/queue/:sessionId/active/staff`.

**Errors:** none (empty array valid).

---

### GET /v1/queue/:sessionId/active/staff

**Auth:** `deejaytools.queue.read` on the session's event: held through a role (an admin's, on any event) or granted on the event (its DJ or event manager). An unknown session, through a role only

**Purpose:** The active queue as `GET /v1/queue/:sessionId/active` gives it, with each entry's check-in `notes` (dancer instructions for the DJ).

**Path params:** `sessionId`

**Query params:** none

**Request body:** none

**Response:** Array of queue entry objects in `data`, `notes` included.

**Errors:**

| Status | Code | Message | Trigger |
|--------|------|---------|---------|
| 403 | `FORBIDDEN` | Admin access required | Scope not held on this event |

---

### GET /v1/queue/:sessionId/waiting

**Auth:** public

**Purpose:** Combined priority + non-priority waiting queues.

**Path params:** `sessionId`

**Query params:** none

**Request body:** none

**Response:** Same as active entries (`notes` always `null`) plus `"subQueue": "priority" | "non_priority"` on each row.

**Errors:** none.

---

### GET /v1/queue/:sessionId/priority

**Auth:** `deejaytools.queue.read` on the session's event: held through a role (an admin's, on any event) or granted on the event (its DJ or event manager). An unknown session, through a role only

**Purpose:** Priority queue only (admin Manager page).

**Path params:** `sessionId`

**Query params:** none

**Request body:** none

**Response:** Array of queue entry objects in `data`.

**Errors:** none.

---

### GET /v1/queue/:sessionId/non-priority

**Auth:** `deejaytools.queue.read` on the session's event: held through a role (an admin's, on any event) or granted on the event (its DJ or event manager). An unknown session, through a role only

**Purpose:** Non-priority queue only.

**Path params:** `sessionId`

**Query params:** none

**Request body:** none

**Response:** Array of queue entry objects in `data`.

**Errors:** none.

---

## `routers/runs.py` — `/v1/runs`

### GET /v1/runs

**Auth:** `deejaytools.runs.read`, on the event `event_id` names, or else on `session_id`'s session's event: held through a role (an admin's, on any event) or granted on the event (its DJ or event manager). With neither filter (every event's runs), through a role only

**Purpose:** Admin run history with display labels.

**Path params:** none

**Query params:**

```typescript
const listQuery = z.object({
  session_id: z.string().optional(),
  event_id: z.string().optional(),
  limit: z.coerce.number().int().min(1).max(500).optional(),
});
```

**Request body:** none

**Response:**

```json
{
  "data": [
    {
      "id": "run_…",
      "completed_at": 1710005000000,
      "division_name": "Classic",
      "session_id": "sess_…",
      "session_floor_trial_starts_at": 1710003600000,
      "event_id": "evt_…",
      "event_name": "RSC 2026",
      "song_id": "song_…",
      "song_label": "Ada Lovelace & Bob Jones · Classic · 2026 · Routine v01",
      "entity_label": "Ada Lovelace & Bob Jones",
      "entity_key": "pair:pair_…",
      "completed_by_label": "Admin Name"
    }
  ],
  "meta": { "version": "…", "count": 1 }
}
```

Default `limit` 200.

**`entity_key`:** stable grouping id for the run's entity — `managed:{id}`, `pair:{id}`, `solo:{userId}`, or `unknown`. Branch order matches `entity_label` resolution (managed → pair → solo).

**Errors:**

| Status | Code | Message | Trigger |
|--------|------|---------|---------|
| 400 | `VALIDATION_ERROR` | … | Invalid query |

---

## `routers/pairs.py` — `/v1/pairs`

### POST /v1/pairs/find-or-create

**Auth:** `deejaytools.partners.write`

**Purpose:** Return or create `pairs` row for `(current user, partner_id)`.

When `partner_id` names a row in another user's address book whose `linked_user_id` is the caller (the linked-partner rule songs use), the pair returned or created is that owner's: `(row's user_id, partner_id)`, the same pair a song shared with the caller checks in as. The link is required; a matching email alone does not count.

**Path params / Query params:** none

**Request body:**

```typescript
const findOrCreateBody = z.object({
  partner_id: z.string().min(1),
});
```

**Response:**

```json
{ "data": { "id": "pair_…" }, "meta": { "version": "…" } }
```

**201** when newly created; **200** when existing.

**Errors:**

| Status | Code | Message | Trigger |
|--------|------|---------|---------|
| 404 | `NOT_FOUND` | Partner not found | Partner neither owned by the user nor linked to their account |

---

## `routers/partners.py` — `/v1/partners`

Schemas:

```typescript
const createBody = z.object({
  first_name: z.string().min(1),
  last_name: z.string().min(1),
  partner_role: PartnerRoleSchema,
  email: z.string().email().optional(),
});

const patchBody = z.object({
  first_name: z.string().min(1).optional(),
  last_name: z.string().min(1).optional(),
  partner_role: PartnerRoleSchema.optional(),
  email: z.string().email().nullable().optional(),
});
```

`PartnerRoleSchema`: `z.enum(["leader", "follower"])`

### GET /v1/partners/leading-pairs

**Auth:** `deejaytools.partners.read`

**Purpose:** Pairs where caller is `user_a` (check-in entity picker).

**Path params / Query params:** none

**Request body:** none

**Response:**

```json
{
  "data": [
    {
      "id": "pair_…",
      "partner_b_id": "part_…",
      "display_name": "Bob Jones"
    }
  ],
  "meta": { "version": "…", "count": 1 }
}
```

**Errors:** none.

---

### GET /v1/partners

**Auth:** `deejaytools.partners.read`

**Purpose:** List caller's partners (`kind = 'partner'` only).

**Path params / Query params:** none

**Request body:** none

**Response:**

```json
{
  "data": [
    {
      "id": "part_…",
      "user_id": "user_…",
      "first_name": "Bob",
      "last_name": "Jones",
      "partner_role": "follower",
      "email": null,
      "linked_user_id": null,
      "created_at": 1710000000000,
      "updated_at": 1710000000000,
      "display_name": "Bob Jones"
    }
  ],
  "meta": { "version": "…", "count": 1 }
}
```

**Errors:** none.

---

### POST /v1/partners

**Auth:** `deejaytools.partners.write`

**Purpose:** Create a partner record.

**Path params / Query params:** none

**Request body:** `createBody`

**Response:** **201** — partner object in `data`.

**Errors:**

| Status | Code | Message | Trigger |
|--------|------|---------|---------|
| 400 | `VALIDATION_ERROR` | … | Invalid body |

---

### GET /v1/partners/:id/associations

**Auth:** `deejaytools.partners.read`

**Purpose:** Count songs and check-in usage before delete.

**Path params:** `id`

**Query params:** none

**Request body:** none

**Response:**

```json
{
  "data": {
    "song_count": 2,
    "has_active_checkin": false,
    "has_checkin_history": true
  },
  "meta": { "version": "…" }
}
```

**Errors:**

| Status | Code | Message | Trigger |
|--------|------|---------|---------|
| 404 | `NOT_FOUND` | Partner not found | Not owned |

---

### GET /v1/partners/:id

**Auth:** `deejaytools.partners.read`

**Purpose:** Fetch one partner.

**Path params:** `id`

**Query params:** none

**Request body:** none

**Response:** Partner object in `data`.

**Errors:**

| Status | Code | Message | Trigger |
|--------|------|---------|---------|
| 404 | `NOT_FOUND` | Partner not found | |

---

### PATCH /v1/partners/:id

**Auth:** `deejaytools.partners.write`

**Purpose:** Update partner fields.

**Path params:** `id`

**Query params:** none

**Request body:** `patchBody`

**Response:** Updated partner in `data`.

**Errors:**

| Status | Code | Message | Trigger |
|--------|------|---------|---------|
| 404 | `NOT_FOUND` | Partner not found | |
| 400 | `BAD_REQUEST` | first_name cannot be empty | Trimmed empty name |
| 400 | `BAD_REQUEST` | last_name cannot be empty | |

---

### DELETE /v1/partners/:id

**Auth:** `deejaytools.partners.write`

**Purpose:** Delete partner; orphan pairs with history, delete pairs without.

**Path params:** `id`

**Query params:** none

**Request body:** none

**Response:** **204** empty body

**Errors:**

| Status | Code | Message | Trigger |
|--------|------|---------|---------|
| 404 | `NOT_FOUND` | Partner not found | |
| 409 | `PARTNER_IN_ACTIVE_CHECKIN` | This partner is linked to a pair with an active check-in… | Live queue entry |

---

## `routers/teams.py` — `/v1/teams`

Schema (`src/schemas`):

```typescript
export const teamIdentifierSchema = z
  .string()
  .trim()
  .regex(/^[A-Za-z0-9 ]+$/, "Team name may only contain letters, numbers, and spaces")
  .min(1)
  .max(100);

export const createTeamBodySchema = z.object({
  identifier: teamIdentifierSchema,
});
```

### GET /v1/teams

**Auth:** `deejaytools.teams.read`

**Purpose:** List caller's teams.

**Path params / Query params:** none

**Request body:** none

**Response:**

```json
{
  "data": [
    {
      "id": "team_…",
      "user_id": "user_…",
      "identifier": "Team Alpha",
      "created_at": 1710000000000,
      "updated_at": 1710000000000
    }
  ],
  "meta": { "version": "…", "count": 1 }
}
```

**Errors:** none.

---

### POST /v1/teams

**Auth:** `deejaytools.teams.write`

**Purpose:** Create a team.

**Path params / Query params:** none

**Request body:** `createTeamBodySchema`

**Response:** **201** — team in `data`.

**Errors:**

| Status | Code | Message | Trigger |
|--------|------|---------|---------|
| 409 | `conflict` | You already have a team with that name. | Unique `(user_id, identifier)` |
| 500 | `INTERNAL` | … | Other DB error |

---

### PATCH /v1/teams/:id

**Auth:** `deejaytools.teams.write`

**Purpose:** Rename a team.

**Path params:** `id`

**Query params:** none

**Request body:** `createTeamBodySchema`

**Response:** Updated team in `data`.

**Errors:**

| Status | Code | Message | Trigger |
|--------|------|---------|---------|
| 404 | `NOT_FOUND` | Team not found | |
| 409 | `conflict` | You already have a team with that name. | Duplicate identifier |

---

### DELETE /v1/teams/:id

**Auth:** `deejaytools.teams.write`

**Purpose:** Delete a team.

**Path params:** `id`

**Query params:** none

**Request body:** none

**Response:** **204**

**Errors:**

| Status | Code | Message | Trigger |
|--------|------|---------|---------|
| 404 | `NOT_FOUND` | Team not found | |

---

## `routers/managed_partnerships.py` — `/v1/managed-partnerships`

Schema (`src/schemas`):

```typescript
export const createManagedPartnershipBodySchema = z.object({
  leader_first_name: z.string().trim().min(1).max(100),
  leader_last_name: z.string().trim().min(1).max(100),
  follower_first_name: z.string().trim().min(1).max(100),
  follower_last_name: z.string().trim().min(1).max(100),
});
```

### GET /v1/managed-partnerships

**Auth:** `deejaytools.partnerships.read`

**Purpose:** List active managed partnerships for caller.

**Path params / Query params:** none

**Request body:** none

**Response:**

```json
{
  "data": [
    {
      "id": "mp_…",
      "user_id": "user_…",
      "leader_first_name": "Ada",
      "leader_last_name": "Lovelace",
      "follower_first_name": "Bob",
      "follower_last_name": "Jones",
      "created_at": 1710000000000,
      "updated_at": 1710000000000
    }
  ],
  "meta": { "version": "…", "count": 1 }
}
```

**Errors:** none.

---

### POST /v1/managed-partnerships

**Auth:** `deejaytools.partnerships.write`

**Purpose:** Create a managed partnership.

**Path params / Query params:** none

**Request body:** `createManagedPartnershipBodySchema`

**Response:** **201** — partnership in `data`.

**Errors:**

| Status | Code | Message | Trigger |
|--------|------|---------|---------|
| 500 | `INTERNAL` | … | Insert failed |

---

### PATCH /v1/managed-partnerships/:id

**Auth:** `deejaytools.partnerships.write`

**Purpose:** Update names on a managed partnership.

**Path params:** `id`

**Query params:** none

**Request body:** `createManagedPartnershipBodySchema`

**Response:** Updated partnership in `data`.

**Errors:**

| Status | Code | Message | Trigger |
|--------|------|---------|---------|
| 404 | `NOT_FOUND` | Managed partnership not found | Soft-deleted or not owned |
| 500 | `INTERNAL` | … | Update failed |

---

### DELETE /v1/managed-partnerships/:id

**Auth:** `deejaytools.partnerships.write`

**Purpose:** Soft-delete partnership and its songs. Their event submissions are deleted; after commit, a `trash` Drive job is enqueued for each submission's event copy ([DRIVE.md](./DRIVE.md#where-jobs-are-enqueued)).

**Path params:** `id`

**Query params:** none

**Request body:** none

**Response:** **204**

**Errors:**

| Status | Code | Message | Trigger |
|--------|------|---------|---------|
| 404 | `NOT_FOUND` | Managed partnership not found | |
| 409 | `MANAGED_PARTNERSHIP_IN_ACTIVE_CHECKIN` | This partnership has an active check-in… | Live queue in non-terminal session |
| 500 | `INTERNAL` | Failed to delete managed partnership | Transaction error |

---

## `routers/event_song_submissions.py` — `/v1/event-song-submissions`

Schema (`src/schemas`):

```typescript
export const createEventSongSubmissionBodySchema = z.object({
  event_id: z.string().min(1),
  song_id: z.string().min(1),
});
```

### GET /v1/event-song-submissions

**Auth:** `deejaytools.submissions.read`

**Purpose:** List caller's event song submissions.

**Path params:** none

**Query params:**

```typescript
const listQuery = z.object({
  event_id: z.string().optional(),
});
```

**Request body:** none

**Response:**

```json
{
  "data": [
    {
      "id": "ess_…",
      "event_id": "evt_…",
      "event_name": "RSC 2026",
      "event_start_date": "2026-03-01",
      "event_status": "upcoming",
      "song_id": "song_…",
      "song_label": "Ada Lovelace & Bob Jones · Classic · 2026 · Routine v01",
      "division": "Classic",
      "created_at": 1710000000000
    }
  ],
  "meta": { "version": "…", "count": 1 }
}
```

**Errors:**

| Status | Code | Message | Trigger |
|--------|------|---------|---------|
| 500 | `INTERNAL` | … | Query failed |

---

### POST /v1/event-song-submissions

**Auth:** `deejaytools.submissions.write`

**Purpose:** Link a song the caller owns, or one shared with them as its linked partner, to an event. A deleted song cannot be submitted. Both partners fill the same entity slot; the caller who submits is the one who lists and withdraws the submission. On success a `copy` Drive job is enqueued to put the file in the event folder; an enqueue failure is reported to Sentry but does not fail the request ([DRIVE.md](./DRIVE.md#where-jobs-are-enqueued)).

**Path params / Query params:** none

**Request body:** `createEventSongSubmissionBodySchema`

**Response:** **201** — submission row in `data` (same shape as GET item).

**Errors:**

| Status | Code | Message | Trigger |
|--------|------|---------|---------|
| 404 | `NOT_FOUND` | Song not found | Deleted, or neither owned nor shared with the caller |
| 404 | `NOT_FOUND` | Event not found | |
| 409 | `conflict` | That song is already submitted to this event. | Unique violation |
| 500 | `INTERNAL` | … | Insert failed |

---

### DELETE /v1/event-song-submissions/:id

**Auth:** `deejaytools.submissions.write`

**Purpose:** Remove caller's submission. If it had an event copy, a `trash` Drive job is enqueued for it after the delete ([DRIVE.md](./DRIVE.md#where-jobs-are-enqueued)).

**Path params:** `id`

**Query params:** none

**Request body:** none

**Response:** **204**

**Errors:**

| Status | Code | Message | Trigger |
|--------|------|---------|---------|
| 404 | `NOT_FOUND` | Event song submission not found | Not owned |
| 500 | `INTERNAL` | … | Delete failed |

---

## `routers/songs.py`, `routers/song_uploads.py` — `/v1/songs`

Schemas:

```typescript
const listQuery = z.object({
  partner_id: z.string().optional(),
});

const createBody = z.object({
  partner_id: z.string().optional(),
  display_name: z.string().optional(),
  original_filename: z.string().optional(),
  division: z.string().min(1, "division is required"),
  routine_name: z.string().nullable().optional(),
  personal_descriptor: z.string().nullable().optional(),
  season_year: z.string().optional(),
});

const patchBody = z.object({
  partner_id: z.string().nullable().optional(),
  display_name: z.string().optional(),
  original_filename: z.string().nullable().optional(),
  division: z.string().nullable().optional(),
  routine_name: z.string().nullable().optional(),
  personal_descriptor: z.string().nullable().optional(),
  season_year: z.string().nullable().optional(),
});
```

Song object in responses:

```json
{
  "id": "song_…",
  "user_id": "user_…",
  "partner_id": "part_…",
  "display_name": "Our Routine",
  "original_filename": "routine.mp3",
  "drive_file_id": "1abc…",
  "drive_folder_id": "folder_…",
  "processed_filename": "Ada_Bob_Classic_2026_Routine_v01.mp3",
  "division": "Classic",
  "routine_name": "Routine",
  "personal_descriptor": null,
  "season_year": "2026",
  "is_legacy": false,
  "created_at": 1710000000000,
  "updated_at": 1710000000000,
  "partner_first_name": "Bob",
  "partner_last_name": "Jones",
  "partner_kind": "partner",
  "managed_partnership_id": null,
  "managed_leader_first_name": null,
  "managed_leader_last_name": null,
  "managed_follower_first_name": null,
  "managed_follower_last_name": null,
  "partnership_label": "Ada Lovelace & Bob Jones",
  "shared_with_me": false
}
```

The partner and `managed_*` name fields are filled only where the route joins them, and are `null` otherwise: `GET /v1/songs` joins both; the single-song routes join the partner only, so their `managed_*` names are `null` even when `managed_partnership_id` is set. `partnership_label` is who the song is for, ready to show (the managed partnership's leader & follower, a placeholder partner's own name, or owner and partner, leader first); it is computed by `GET /v1/songs` only, `null` elsewhere and for a song with neither a partner nor a managed partnership.

`shared_with_me` is true when the caller is not the song's owner but its
partner: the song's `partner_id` names a row in the owner's address book whose
`linked_user_id` is the caller's account. Such a partner may read the song,
submit it to events and check it in; only the owner may change or delete it.
It is false on every song the caller owns, and on routes that do not compute
it.

### GET /v1/songs

**Auth:** `deejaytools.songs.read`

**Purpose:** List caller's non-deleted songs, and non-deleted songs shared with the caller as the song's linked partner (`shared_with_me: true`).

**Path params:** none

**Query params:** `listQuery` — optional `partner_id`: one of the caller's own partners; with it, only the caller's own songs with that partner

**Request body:** none

**Response:** Array of song objects in `data`.

**Errors:**

| Status | Code | Message | Trigger |
|--------|------|---------|---------|
| 400 | `VALIDATION_ERROR` | … | Invalid query |

---

### POST /v1/songs

**Auth:** `deejaytools.songs.write`

**Purpose:** Create a song metadata row without file upload (legacy path; prefer chunked upload).

**Path params / Query params:** none

**Request body:** `createBody`

**Response:** **201** — song in `data` (Drive fields null).

**Errors:**

| Status | Code | Message | Trigger |
|--------|------|---------|---------|
| 400 | `BAD_REQUEST` | Partner not found or does not belong to you | Invalid `partner_id` |

---

### GET /v1/songs/:id

**Auth:** `deejaytools.songs.read`

**Purpose:** Fetch one owned song, or one shared with the caller as its linked partner (`shared_with_me: true`).

**Path params:** `id`

**Query params:** none

**Request body:** none

**Response:** Song object in `data`.

**Errors:**

| Status | Code | Message | Trigger |
|--------|------|---------|---------|
| 404 | `NOT_FOUND` | Song not found | Neither owned nor shared with the caller, or soft-deleted |

---

### PATCH /v1/songs/:id

**Auth:** `deejaytools.songs.write`

**Purpose:** Update song metadata. Owner only: a linked partner gets 404.

**Path params:** `id`

**Query params:** none

**Request body:** `patchBody`

**Response:** Updated song in `data`.

**Errors:**

| Status | Code | Message | Trigger |
|--------|------|---------|---------|
| 404 | `NOT_FOUND` | Song not found | |
| 400 | `BAD_REQUEST` | Partner not found or does not belong to you | |

---

### DELETE /v1/songs/:id

**Auth:** `deejaytools.songs.write`

**Purpose:** Soft-delete song and remove event submissions. Owner only: a linked partner gets 404. The song's own Drive file is moved to `_deprecated` inline (best-effort; failure is logged and ignored), and after commit a `trash` Drive job is enqueued for each submission's event copy ([DRIVE.md](./DRIVE.md#song-deletion)).

**Path params:** `id`

**Query params:** none

**Request body:** none

**Response:** **204**

**Errors:**

| Status | Code | Message | Trigger |
|--------|------|---------|---------|
| 404 | `NOT_FOUND` | Song not found | |
| 409 | `SONG_IN_ACTIVE_CHECKIN` | This song is referenced by an active check-in… | Live queue in non-terminal session |
| 500 | `INTERNAL` | Failed to delete song | Transaction error |

---

### POST /v1/songs/upload/chunk

**Auth:** `deejaytools.songs.write`. `on_behalf_of_user_id` also needs `deejaytools.delegation.act`: through a role (admins, for anyone, `event_id` not needed), or granted on the event `event_id` names (its DJ or event manager), and then only for a person connected to that event (as for `POST /v1/checkins`); for anyone else the DJ or event manager gets 403. Checked on every chunk

**Purpose:** Chunked upload. The song row is created when the final chunk arrives and returned at once; tagging, the Drive upload and the row's Drive fields happen in the background afterwards, staged in `song_uploads` ([ADR-0001](decisions/ADR-0001-durable-song-builds.md)). A failed build is retried; a song nothing references yet is deleted again instead. The song's `season_year` is the upload's season by its **UTC** date: it rolls over at 00:00 UTC on October 1 ([ADR-0011](decisions/ADR-0011-season-year-rolls-over-october-1.md)). Staging, filename construction and the background steps are specified in [DRIVE.md](./DRIVE.md#upload-pipeline).

**Path params / Query params:** none

**Request body:** `multipart/form-data` (no Zod schema — validated manually):

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `chunk` | File | yes | Max 10 MB per chunk |
| `upload_id` | UUID string | yes | Groups chunks |
| `chunk_index` | integer | yes | 0-based |
| `total_chunks` | integer | yes | 1–30 |
| `original_filename` | string | no | Defaults to `"song.mp3"` when omitted or empty |
| `mime_type` | string | no | Ignored. The format comes from the assembled file's magic bytes |
| `division` | string | yes | |
| `partner_id` | string | portal: empty | XOR with `managed_partnership_id` |
| `managed_partnership_id` | string | optional | |
| `routine_name` | string | optional | |
| `personal_descriptor` | string | optional | |
| `on_behalf_of_user_id` | string | admin, or event DJ / event manager | Target owner |
| `event_id` | string | staff on behalf | The event an upload on behalf is for: the delegation is decided on it ([ADR-0012](./decisions/ADR-0012-per-event-staff-roles.md)). Needed by a DJ or event manager, optional for an admin; not stored, and ignored without `on_behalf_of_user_id` |
| `entity_type` | `team` \| `other` | portal uploads | Creates placeholder partner. Any other non-empty value, including `solo`, is rejected with `Invalid entity_type` (the handler's solo branch is unreachable) |
| `entity_name` | string | `other` / optional solo | |
| `team_id` | string | team uploads | Must own team |
| `submit_to_event` | `"true"` | optional | With `on_behalf_of_user_id` and `event_id`: also submits the song to that event in the same request, the dancer as submitter and the caller as uploader, with the song's own division and the default round (no override). `partner_id`, if sent, must be one of the dancer's partners of kind `partner` (`GET /v1/events/:id/people/:user_id/partners`). Not for portal uploads |

**Response (non-final chunk):**

```json
{ "data": { "received": true, "complete": false }, "meta": { "version": "…" } }
```

**Response (final chunk):**

```json
{
  "data": {
    "received": true,
    "complete": true,
    "song": { },
    "submission": { }
  },
  "meta": { "version": "…" }
}
```

`submission` is there only with `submit_to_event`: the submission as `POST /v1/event-song-submissions` answers it. Its Drive copy follows once the song's build has a file.

In this response `processed_filename`, `season_year`, `drive_file_id` and `drive_folder_id` are always null, and the partner name fields are null. They are filled in when the background build finishes; poll `GET /v1/songs/:id` (it 404s if the build failed and the unreferenced row was removed).

**Errors:** listed in the order the handler checks them. The first group applies to every chunk; the rest only to the final chunk.

| Status | Code | Message | Trigger |
|--------|------|---------|---------|
| 400 | `BAD_REQUEST` | Invalid upload_id | Not a UUID (any case) |
| 400 | `BAD_REQUEST` | division is required | Missing or blank |
| 400 | `BAD_REQUEST` | Invalid chunk_index | Not an integer ≥ 0 after JavaScript `Number()` (so `""` → 0 is accepted) |
| 400 | `BAD_REQUEST` | Invalid total_chunks (max 30) | Not an integer in 1–30 |
| 400 | `BAD_REQUEST` | chunk_index out of range | `chunk_index >= total_chunks` |
| 400 | `BAD_REQUEST` | Missing chunk field | `chunk` absent or not a file |
| 400 | `BAD_REQUEST` | submit_to_event needs on_behalf_of_user_id and event_id | `submit_to_event` without either |
| 403 | `FORBIDDEN` | Admin access required | `on_behalf_of_user_id` without `deejaytools.delegation.act` through a role or on `event_id`'s event (a DJ without `event_id` included), or for a person not connected to that event |
| 400 | `BAD_REQUEST` | Chunk exceeds 10 MB limit | Chunk > 10 MiB |
| 429 | `TOO_MANY_UPLOADS` | You have 3 uploads in progress. Finish or wait for one before starting another. | First chunk of a new upload while the caller already has 3 uploads staged (counted per caller, also when uploading on behalf) |
| 400 | `BAD_REQUEST` | File exceeds 100 MB limit | Any chunk that takes the upload's staged chunks past 110 MiB; the staged chunks are removed |
| 400 | `BAD_REQUEST` | Portal uploads cannot specify partner_id or managed_partnership_id | Final chunk: `entity_type` with a partner field |
| 400 | `BAD_REQUEST` | Invalid entity_type | Not `team` / `other` |
| 400 | `BAD_REQUEST` | Target user not found | On-behalf user missing |
| 400 | `BAD_REQUEST` | team_id is required for team uploads | |
| 400 | `BAD_REQUEST` | Team not found | Not the effective user's team |
| 400 | `BAD_REQUEST` | entity_name is required for other uploads | |
| 400 | `BAD_REQUEST` | User not found / Set your name on My Profile or provide entity_name | Unreachable today (solo branch; `solo` is rejected earlier) |
| 400 | `BAD_REQUEST` | Specify either partner_id or managed_partnership_id, not both | |
| 400 | `BAD_REQUEST` | Managed partnership not found or does not belong to you | |
| 400 | `BAD_REQUEST` | Partner not found or does not belong to you | |
| 400 | `BAD_REQUEST` | submit_to_event is not available for portal uploads | `submit_to_event` with `entity_type` (checked before the partner fields) |
| 400 | `BAD_REQUEST` | Partner is not on the dancer's partner list | `submit_to_event`: the partner is the dancer's but not of kind `partner` |
| 404 | `NOT_FOUND` | Event not found | `submit_to_event`: unknown `event_id` |
| 409 | `ENTITY_SLOT_TAKEN` | This entity already has a song submitted for … | `submit_to_event`: the entity already has a song in that division at the event. Nothing is saved |
| 500 | `CHUNK_ERROR` | Failed to read uploaded chunks | Upload directory can't be listed (directory is left in place) |
| 409 | `CHUNK_MISSING` | Expected N chunks but only received M. Please retry the upload. | Chunk count ≠ `total_chunks` |
| 400 | `BAD_REQUEST` | File exceeds 100 MB limit | Assembled size > 110 MiB |
| 400 | `UNSUPPORTED_FORMAT` | That file doesn't look like a supported audio format. Please upload an MP3, WAV, FLAC, or M4A. | Magic-byte check |
| 500 | `INTERNAL` | … | Unhandled error (e.g. song insert) |

Also from global middleware: `413 payload_too_large` for a request body over 11 MiB, `429 too_many_requests`, and the shared auth rows.

**Timeout:** 300 s (upload path).

---

## `routers/feedback.py` — `/v1/feedback`

### POST /v1/feedback

**Auth:** public

**Purpose:** Submit site feedback (optional Brevo email when `BREVO_API_KEY`, `FEEDBACK_FROM_EMAIL` and `FEEDBACK_TO_EMAIL` are all set).

**Path params / Query params:** none

**Request body:**

```typescript
const feedbackBodySchema = z
  .object({
    type: z.enum(["bug", "feature", "general"]),
    subject: z.string().min(1).max(255),
    message: z.string().min(1).max(20_000),
    contactName: z.preprocess(
      (v) => (v === "" || v === undefined || v === null ? undefined : v),
      z.string().max(255).optional(),
    ),
    contactEmail: z.preprocess(
      (v) => (v === "" || v === undefined || v === null ? undefined : v),
      z.string().email().optional(),
    ),
    screenshot: z.string().max(3 * 1024 * 1024).optional(),
  })
  .superRefine((val, ctx) => {
    const s = val.screenshot;
    if (s !== undefined && s.length > 0 && !DATA_URL_PREFIX.test(s)) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["screenshot"],
        message: "Screenshot must be a PNG or JPEG data URL",
      });
    }
  });
```

(`DATA_URL_PREFIX = /^data:image\/(png|jpeg);base64,/i`)

**Response:** **201** — `{ "data": null, "meta": { … } }`

**Errors:**

| Status | Code | Message | Trigger |
|--------|------|---------|---------|
| 400 | `VALIDATION_ERROR` | … | Invalid body / screenshot format |
| 502 | `EMAIL_FAILED` | Failed to send email. Please try again. | Brevo refused the email, could not be reached, or did not answer within the call's 10 s timeout |

If `BREVO_API_KEY` or either feedback address is unset, feedback is logged and **201** still returned. Production refuses to start without both addresses.

---

## Divisions — `/v1/divisions`

### GET /v1/divisions

**Auth:** public

**Purpose:** The competition divisions, in their display groups and flat in the same order, from `DIVISION_GROUPS` in `src/api_deejaytools/domain.py` (the single source of truth).

**Path params / Query params:** none

**Request body:** none

**Response:** **200**

```json
{
  "data": {
    "groups": [["Classic", "Showcase", "…"], ["ProAm LeaderAm", "…"]],
    "divisions": ["Classic", "Showcase", "…"]
  },
  "meta": { "version": "v1" }
}
```

**Errors:** none of its own.

---

## `routers/admin_checkins.py` — `/v1/admin/checkins`

### POST /v1/admin/checkins

**Auth:** `deejaytools.testdata.write`

**Purpose:** Inject synthetic test check-in (stub user/partner/pair/song).

**Path params / Query params:** none

**Request body:**

```typescript
const injectBody = z.object({
  sessionId: z.string().min(1),
  divisionName: z.string().min(1),
  leaderFirstName: z.string().min(1),
  leaderLastName: z.string().min(1),
  followerFirstName: z.string().min(1),
  followerLastName: z.string().min(1),
  notes: z.string().nullish(),
});
```

**Response:** **201**

```json
{
  "data": {
    "id": "chk_…",
    "sessionId": "sess_…",
    "divisionName": "Classic",
    "initialQueue": "priority",
    "pair": {
      "id": "pair_…",
      "partner_b_id": "part_…",
      "display_name": "Leader Name & Follower Name"
    }
  },
  "meta": { "version": "…" }
}
```

**Errors:**

| Status | Code | Message | Trigger |
|--------|------|---------|---------|
| 404 | `NOT_FOUND` | Session not found | |
| 400 | `BAD_REQUEST` | … | Admission / division errors |
| 409 | `conflict` | This entity already has a live queue entry… / Check-in conflicted… | |

Bypasses check-in time window.

---

### GET /v1/admin/checkins/test

**Auth:** `deejaytools.testdata.read`

**Purpose:** List synthetic test injections (`admin-injected-*@test.local` users).

**Path params / Query params:** none

**Request body:** none

**Response:**

```json
{
  "data": [
    {
      "pair_id": "pair_…",
      "created_at": 1710000000000,
      "leader_name": "Test Leader",
      "follower_name": "Test Follower",
      "session_id": "sess_…",
      "session_name": "Saturday AM",
      "division_name": "Classic",
      "queue_status": "priority",
      "position": 1
    }
  ],
  "meta": { "version": "…", "count": 1 }
}
```

`queue_status`: `active` | `priority` | `non_priority` | `off_queue`

**Errors:** none.

---

### DELETE /v1/admin/checkins/test

**Auth:** `deejaytools.testdata.write`

**Purpose:** Hard-delete all synthetic test injection data.

**Path params / Query params:** none

**Request body:** none

**Response:**

```json
{ "data": { "deleted": 3 }, "meta": { "version": "…" } }
```

(`deleted` = count of synthetic users removed)

**Errors:** none (0 deletions valid).

---

## `routers/admin_songs.py` — `/v1/admin/songs`

### GET /v1/admin/songs

**Auth:** `deejaytools.library.read`

**Purpose:** Searchable directory of all songs.

**Path params:** none

**Query params:**

```typescript
const listQuery = z.object({
  q: z.string().optional(),
  include_deleted: z.enum(["true", "false"]).optional(),
});
```

**Request body:** none

**Response:**

```json
{
  "data": [
    {
      "id": "song_…",
      "song_label": "Ada Lovelace & Bob Jones · Classic · 2026 · Routine v01",
      "display_name": "Routine",
      "division": "Classic",
      "routine_name": "Routine",
      "personal_descriptor": null,
      "season_year": "2026",
      "is_legacy": false,
      "created_at": 1710000000000,
      "deleted_at": null,
      "owner": {
        "id": "user_…",
        "email": "dancer@example.com",
        "full_name": "Ada Lovelace"
      },
      "partner": {
        "id": "part_…",
        "full_name": "Bob Jones",
        "linked_user_email": null
      }
    }
  ],
  "meta": { "version": "…", "count": 1 }
}
```

**Errors:**

| Status | Code | Message | Trigger |
|--------|------|---------|---------|
| 400 | `VALIDATION_ERROR` | … | Invalid query |

---

## `routers/admin_drive_jobs.py` — `/v1/admin/drive-jobs`

Operator view of the `drive_jobs` queue: the background Drive copy / rename /
deprecate work. What the jobs do, when they are enqueued, and the state machine
are in [DRIVE.md](./DRIVE.md#drive_jobs-queue). Table: [`drive_jobs`](./SCHEMA.md#drive_jobs).

`GET /` and `GET /summary` are both registered; `/summary` is a static segment and
matches before the list route would ever see it.

### GET /v1/admin/drive-jobs/summary

**Auth:** `deejaytools.drivejobs.read`

**Purpose:** Queue health in one call — job counts by status, plus how many
submissions still have no event copy.

**Path params / Query params:** none

**Request body:** none

**Response:** **200**

```json
{
  "data": {
    "by_status": { "pending": 2, "done": 140, "failed": 1 },
    "submissions_without_copy": 3
  },
  "meta": { "version": "…" }
}
```

- `by_status` has a key only for statuses with at least one row (an empty table
  gives `{}`); counts are integers.
- `submissions_without_copy` counts every `event_song_submissions` row with a null
  `drive_copy_file_id`, including ones whose copy is legitimately skipped (song
  with no Drive file).

**Errors:**

| Status | Code | Message | Trigger |
|--------|------|---------|---------|
| 500 | `INTERNAL` | Internal server error | Query failed |

---

### GET /v1/admin/drive-jobs

**Auth:** `deejaytools.drivejobs.read`

**Purpose:** Recent jobs, most recently updated first, with the last error.

**Path params:** none

**Query params:**

```typescript
const listQuery = z.object({
  status: z.enum(["pending", "running", "done", "failed"]).optional(),
  limit: z.coerce.number().int().min(1).max(200).default(50),
});
```

**Request body:** none

**Response:** **200**

```json
{
  "data": [
    {
      "id": "6f1c…",
      "kind": "copy",
      "status": "failed",
      "attempts": 10,
      "last_error": "Google Drive environment variables are not configured",
      "next_attempt_at": 1759360000000,
      "created_at": 1759349000000,
      "updated_at": 1759358200000,
      "submission_id": "sub_…",
      "file_id": null,
      "event_id": null,
      "user_id": null,
      "event_name": "Swingtacular 2027"
    }
  ],
  "meta": { "version": "…", "count": 1 }
}
```

- Ordered by `updated_at` descending, then truncated to `limit`.
- `kind` is `copy` | `rename` | `trash` | `share_event_folder` |
  `unshare_event_folder` | `share_song` ([DRIVE.md](./DRIVE.md#job-kinds)).
  `submission_id` is set for copy and rename, `file_id` for trash and
  `share_song`. `event_id` and `user_id` are set for the two folder kinds,
  and a share's `file_id` is the folder it shared once it ran.
- `event_name` comes from a left join through the submission, or else through
  the job's `event_id`: `null` for trash jobs and for jobs whose submission or
  event has since been deleted.
- Timestamps are epoch ms. `next_attempt_at` keeps its last value after a job
  reaches `done` or `failed` and means nothing there.

**Errors:**

| Status | Code | Message | Trigger |
|--------|------|---------|---------|
| 400 | `VALIDATION_ERROR` | … | Unknown `status`, `limit` outside 1–200 |
| 500 | `INTERNAL` | Internal server error | Query failed |

---

### POST /v1/admin/drive-jobs/backfill-renames

**Auth:** `deejaytools.drivejobs.write`

**Purpose:** Enqueue a `rename` job for every submission that has an event copy,
re-applying the current naming rule. Run after any change to
`resolve_submission_filename()` (`src/api_deejaytools/submissions.py`). Safe to repeat: a rename whose name already matches
makes no Drive write.

**Path params / Query params:** none

**Request body:** none (ignored)

**Response:** **200**

```json
{ "data": { "enqueued": 42 }, "meta": { "version": "…" } }
```

Jobs are inserted one at a time, not in a transaction; a failure part-way returns
500 with the earlier jobs already queued (harmless, since renames are idempotent).
They run on later scheduler ticks, 10 per tick.

**Errors:**

| Status | Code | Message | Trigger |
|--------|------|---------|---------|
| 500 | `INTERNAL` | Internal server error | Select or insert failed |

---

### POST /v1/admin/drive-jobs/:id/retry

**Auth:** `deejaytools.drivejobs.write`

**Purpose:** Return an exhausted (`failed`) job to the queue: `status = 'pending'`,
`attempts = 0`, `next_attempt_at = updated_at = now`. `last_error` is kept until the
next attempt overwrites or clears it.

**Path params:** `id` — the job id

**Query params:** none

**Request body:** none (ignored)

**Response:** **200** — the row as updated

```json
{ "data": { "id": "6f1c…", "status": "pending" }, "meta": { "version": "…" } }
```

The update repeats the `status = 'failed'` condition, so a concurrent change
between the read and the write is reported as a conflict rather than overwritten.

**Errors:**

| Status | Code | Message | Trigger |
|--------|------|---------|---------|
| 404 | `NOT_FOUND` | Drive job not found | No row with that id |
| 409 | `conflict` | Job is `<status>`, not failed — only exhausted jobs can be retried. | Job is `pending`, `running` or `done` (`<status>` is the actual value) |
| 409 | `conflict` | Job changed state before it could be retried — re-check and try again. | Status changed between the read and the update |

Unlike the other three routes this handler catches nothing: a database error
goes to the app's unhandled-exception handler (500 `INTERNAL`, reported to
Sentry, with the raw error message outside production).

---

## `routers/admin_event_submissions.py` — `/v1/admin/event-song-submissions`

### GET /v1/admin/event-song-submissions

**Auth:** `deejaytools.entries.read`

**Purpose:** All submissions for one event (Manager Event Songs tab).

**Path params:** none

**Query params:**

```typescript
const listQuery = z.object({
  event_id: z.string().min(1),
});
```

**Request body:** none

**Response:**

```json
{
  "data": [
    {
      "id": "ess_…",
      "event_id": "evt_…",
      "event_name": "RSC 2026",
      "division": "Classic",
      "song_id": "song_…",
      "owner_user_id": "user_…",
      "song_label": "…",
      "partnership_label": "Ada Lovelace & Bob Jones",
      "submitter_email": "dancer@example.com",
      "created_at": 1710000000000
    }
  ],
  "meta": { "version": "…", "count": 1 }
}
```

`owner_user_id` is the song owner's user id, the `on_behalf_of_user_id` that `POST /v1/checkins` takes to check in for that song.

**Errors:**

| Status | Code | Message | Trigger |
|--------|------|---------|---------|
| 400 | `VALIDATION_ERROR` | … | Missing `event_id` |
| 500 | `INTERNAL` | … | Query failed |

---

### GET /v1/events/:id/song-submissions

The same list for an event's staff, outside `/v1/admin` ([ADR-0012](./decisions/ADR-0012-per-event-staff-roles.md)).

**Auth:** `deejaytools.entries.read` on the event: held through a role (an admin's, on any event) or granted on the event in the path (its coordinator, DJ or event manager)

**Purpose:** Every submission to the event, for the coordinator portal.

**Path params:** `id` — event id

**Query params:** none

**Request body:** none

**Response:** As `GET /v1/admin/event-song-submissions` for `event_id` = `id`, `owner_user_id` included, except that `submitter_email` is masked (`d•••@example.com`) for every caller but an admin; admins see it whole.

**Errors:**

| Status | Code | Message | Trigger |
|--------|------|---------|---------|
| 403 | `FORBIDDEN` | Admin access required | Scope not held on this event |
| 404 | `NOT_FOUND` | Event not found | Unknown id |
| 500 | `INTERNAL` | … | Query failed |

---

### GET /v1/events/:id/people

The staff portals' person picker ([ADR-0012](./decisions/ADR-0012-per-event-staff-roles.md)).

**Auth:** `deejaytools.entries.read` on the event: held through a role (an admin's, on any event) or granted on the event in the path (its coordinator, DJ or event manager)

**Purpose:** Find a person connected to the event by name. Only the event's own people are searched, never every user:

- on a song submitted to it: the submitter, the song's owner, and the account linked to the song's partner;
- checked in at, or with a queue entry in, one of its sessions: the solo dancer, the pair's leader and the account linked to its partner, or the user who manages the managed partnership;
- on its staff.

A person connected in several ways is listed once. Emails are never matched and never returned in full.

**Path params:** `id` — event id

**Query params:**

```typescript
const query = z.object({
  q: z.string().trim().min(2), // part of the name, case-insensitive
});
```

`q` is matched as a substring of the person's display name or their first and last name.

**Request body:** none

**Response:** at most 20 people, ordered by name. `display_name` is the person's display name, or else their first and last name. `email_masked` is the first character of the email's local part, `•••`, then `@` and the domain; null when there is nothing to mask.

```json
{
  "data": [
    {
      "user_id": "user_2abc…",
      "display_name": "Ada Lovelace",
      "email_masked": "a•••@example.com"
    }
  ],
  "meta": { "version": "…", "count": 1 }
}
```

**Errors:**

| Status | Code | Message | Trigger |
|--------|------|---------|---------|
| 400 | `VALIDATION_ERROR` | … | `q` missing, repeated, or shorter than 2 characters once trimmed |
| 403 | `FORBIDDEN` | Admin access required | Scope not held on this event |
| 404 | `NOT_FOUND` | Event not found | Unknown id |

---

### GET /v1/events/:id/people/:user_id/partners

The partner list for a staff upload on behalf.

**Auth:** `deejaytools.delegation.act` on the event: held through a role (an admin's, on any event) or granted on the event in the path (its DJ or event manager). The admin-wide `GET /v1/admin/users/:id/partners` stays admin-only.

**Purpose:** One person's partners of kind `partner`, by last then first name, to pick `partner_id` from for `POST /v1/songs/upload/chunk` with `submit_to_event`. Only for a person connected to the event, as `GET /v1/events/:id/people` finds them.

**Path params:** `id` — event id; `user_id` — the person

**Query params / Request body:** none

**Response:** the partners, as `GET /v1/partners` returns them, except for a non-admin caller: each `email` is masked (`b•••@example.com`) and `linked_user_id` is `null`. Admins see both whole.

**Errors:**

| Status | Code | Message | Trigger |
|--------|------|---------|---------|
| 403 | `FORBIDDEN` | Admin access required | Scope not held on this event |
| 404 | `NOT_FOUND` | Event not found | Unknown id |
| 404 | `NOT_FOUND` | Person not found at this event | The person is not connected to the event |

---

## `routers/admin_users.py` — `/v1/admin/users`

### GET /v1/admin/users

**Auth:** `deejaytools.users.read`

**Purpose:** User directory with song/partner counts.

**Path params:** none

**Query params:**

```typescript
const listQuery = z.object({
  q: z.string().optional(),
  role: z.enum(["user", "admin"]).optional(),
});
```

**Request body:** none

**Response:**

```json
{
  "data": [
    {
      "id": "user_…",
      "email": "dancer@example.com",
      "first_name": "Ada",
      "last_name": "Lovelace",
      "role": "user",
      "created_at": 1710000000000,
      "song_count": 3,
      "partner_count": 2
    }
  ],
  "meta": { "version": "…", "count": 1 }
}
```

**Errors:**

| Status | Code | Message | Trigger |
|--------|------|---------|---------|
| 400 | `VALIDATION_ERROR` | … | Invalid query |

---

### PATCH /v1/admin/users/:id/role

**Auth:** `deejaytools.users.write`

**Purpose:** Promote or demote a user.

**Path params:** `id`

**Query params:** none

**Request body:**

```typescript
const updateRoleParam = z.object({ id: z.string().min(1) });
const updateRoleBody = z.object({ role: z.enum(["user", "admin"]) });
```

**Response:** User object (same shape as GET list item) in `data`.

**Errors:**

| Status | Code | Message | Trigger |
|--------|------|---------|---------|
| 403 | `forbidden` | You cannot change your own admin role. | Self-demote |
| 404 | `NOT_FOUND` | Not found | Unknown user id |

---

### GET /v1/admin/users/:id/partners

**Auth:** `deejaytools.users.read`

**Purpose:** Partner roster for a user (Upload For flow).

**Path params:** `id` — target user id

**Query params:** none

**Request body:** none

**Response:** Array of partner objects (same as `GET /v1/partners`) in `data`.

**Errors:**

| Status | Code | Message | Trigger |
|--------|------|---------|---------|
| 404 | `NOT_FOUND` | User not found | |

---

### GET /v1/admin/users/:id/event-song-submissions

**Auth:** `deejaytools.users.read`

**Purpose:** Event submissions for a specific user and event.

**Path params:** `id` — target user id

**Query params:**

```typescript
const submissionsQuery = z.object({ event_id: z.string().min(1) });
```

**Request body:** none

**Response:** Same submission shape as `GET /v1/event-song-submissions` in `data`.

**Errors:**

| Status | Code | Message | Trigger |
|--------|------|---------|---------|
| 404 | `NOT_FOUND` | User not found | |
| 400 | `VALIDATION_ERROR` | … | Missing `event_id` |

---

## Routes without a web-app caller

The web app does not call these. They are kept as part of the API, for direct use (scripts, operators, a future screen), and are tested like every other route:

| Route | What it is for |
|-------|----------------|
| `POST /v1/queue/promote` | Moving one waiting entry to the active queue by hand; the scheduler's auto-fill does this in normal running |
| `GET /v1/partners/:id` | Reading one partner; the web app reads the list |
| `POST /v1/songs` | Creating a song record without a file; the web app creates songs through the chunked upload |
| `GET /v1/songs/:id` | Reading one song; the web app reads the list |
| `PATCH /v1/songs/:id` | Editing a song's fields (owner only); the web app has no song-edit screen yet |

## Not implemented

These routes appear in older docs, comments, or migration history but **do not exist** in the current API:

| Route | Notes |
|-------|-------|
| `GET /v1/legacy-songs` | No handler; the `legacy_songs` table exists but nothing reads it |
| `POST /v1/songs/claim-legacy` | Legacy metadata lives in `songs` rows whose `processed_filename` starts with `[Legacy] ` |
| `/v1/music-history` | No such route |
| `/v1/floor-slots` / slot fill routes | The queue is `/v1/queue/*` ([ADR-0006](decisions/ADR-0006-floor-trial-queue-model-as-built.md)) |
| `GET /v1/queue-events` | Audit log is written but has no read API |
| `GET /v1/event-registrations` | No such table |

Unknown paths, and a known path with an unsupported method, return **404** `NOT_FOUND` "Not found".
