# ADR-0012: Per-event staff roles: coordinator, DJ and event manager

Date: 2026-10-28

## Status

Accepted

## Context

Authority is all-or-nothing today (ADR-0008): every signed-in person holds
`deejaytools-dancer`, and the organizers hold `deejaytools-admin`, which
reaches every event, every user and the Drive pipeline. Running an event
takes more people than the admins: someone checks the event's entries,
someone DJs the floor trials, and an organizer may run one event without
being trusted with the whole service.

Three staff roles are wanted, each given to a person for one event by an
admin, never globally. This ADR fixes what each role may do, route by
route, how the grant is stored and checked, and what each role's portal in
deejaytools-com shows. Building it is split into later work. The
[Route additions](#route-additions) section says which rows of ADR-0008's
scope table each part adds.

The identity library already has the mechanism. An explicit grant is a
`(scope, resource)` pair held by a principal outside any role
(`identity_explicit_grants`). `authorize` consults it after roles when the
request names a resource. The store's own documentation offers it as the
alternative to inventing a role per resource.

## Decision

### The three roles

All three are granted per event. Each holds its scopes only on the event
`event:<event_id>` it was granted for. The dancer role, the admin role and
their behaviour are unchanged.

| Role | Purpose | Scopes, on the granted event |
|------|---------|------------------------------|
| Coordinator | Sees what has been entered | `deejaytools.entries.read` |
| DJ | Runs the floor trials and plays the music | `deejaytools.entries.read`, `deejaytools.queue.read`, `deejaytools.queue.manage`, `deejaytools.runs.read`, `deejaytools.sessions.write`, `deejaytools.delegation.act`, plus read access to the event's Drive folder |
| Event manager | Admin, limited to the events granted | everything the DJ has (Drive folder access included), plus `deejaytools.events.write`, `deejaytools.staff.read` and `deejaytools.staff.write` |

The role definitions live in data, not code. Each is an `identity_roles` row
(`deejaytools-event-coordinator`, `deejaytools-event-dj`,
`deejaytools-event-manager`) with its scopes in `identity_role_scopes`. They
are never written to `identity_principal_roles`, because that would make
them global.

### What each scope covers when held on an event

- `deejaytools.entries.read`: the event's song submissions, through a new
  non-admin route `GET /v1/events/:id/song-submissions`. It returns what
  `GET /v1/admin/event-song-submissions` returns for that event, which
  includes each submission's song label and division. The admin route stays
  admin-only. The event's songs are seen through its submissions:
  `deejaytools.library.read` (every user's songs) stays admin-only.
  Staff also find the event's own people by name, with their email masked,
  through `GET /v1/events/:id/people`.
- `deejaytools.queue.read` and `deejaytools.queue.manage`: the queue views
  and the promote, complete, incomplete, move-down and withdraw actions, for
  sessions of the event. A body or path that names a session or queue entry
  is resolved to its event before the decision.
- `deejaytools.runs.read`: `GET /v1/runs` when `event_id` is given, or
  `session_id` names a session of the event. Without either filter, the
  route needs the global scope.
- `deejaytools.sessions.write`: create, edit, set status, set divisions,
  and delete sessions of the event. Create resolves the event from the
  body's `event_id`.
- `deejaytools.delegation.act`: acting for a dancer at the event, on both
  delegated routes. A check-in (`POST /v1/checkins` with
  `on_behalf_of_user_id`) resolves the event from its session. A song upload
  (`POST /v1/songs/upload/chunk` with `on_behalf_of_user_id`) does not belong
  to an event today, so an upload on behalf by a scoped caller must carry an
  `event_id` and is authorized against that event. An admin's upload on
  behalf keeps working without one. The audit record still names the dancer
  acted for.
- `deejaytools.events.write`: `PATCH` and `DELETE /v1/events/:id` for the
  event. Creating an event (`POST /v1/events`) names no existing event, so
  it stays admin-only, and an event manager cannot create events.
- `deejaytools.staff.write` (new): grant and revoke coordinator and DJ on
  the event. Only an admin grants or revokes event manager.

### What stays admin-only

`POST /v1/events`; every `/v1/admin/*` route (users, roles, the song
library, every event's submissions, Drive jobs and test check-ins); acting
on behalf with no event; granting or revoking event manager; and any
global read without an event filter. No staff role gets an admin page or an
`/v1/admin/*` route.

### Granting and revoking

- One table, `event_staff_grants`: `event_id` (references `events`, delete
  cascades), `user_id`, `role` (one of `coordinator`, `dj`,
  `event-manager`), `granted_by`, `granted_at`, primary key
  `(event_id, user_id, role)`. It is the record of who is on an event's
  staff.
- Routes, outside `/v1/admin`:
  - `GET /v1/events/:id/staff` (`deejaytools.staff.read`) lists the
    event's staff.
  - `PUT /v1/events/:id/staff/:userId/:role` (`deejaytools.staff.write`)
    grants a role. It is idempotent.
  - `DELETE /v1/events/:id/staff/:userId/:role` (`deejaytools.staff.write`)
    revokes it.

  The admin role gains `deejaytools.staff.read` and
  `deejaytools.staff.write` globally. An event manager holds both on its
  events, and its grant and revoke are refused for the `event-manager` role.
- A grant writes the `event_staff_grants` row and, in the same transaction,
  one `identity_explicit_grants` row per scope of the role, with resource
  `event:<event_id>`. `granted_by` on those rows names the role and the
  granter.
- A revoke deletes the row, and removes each derived explicit grant unless
  another role the same person still holds on that event also carries the
  scope.
- Deleting an event deletes its staff rows and every explicit grant on
  `event:<event_id>` in the same transaction.
- Every grant and revoke is audited through the identity library, like any
  other decision.
- `GET /v1/auth/me` gains `event_roles`, a list of `{event_id, role}` for
  the caller. Its classification is unchanged (it reads only the caller's
  own record). The web app uses it to show a person their portals.

### The DJ's Drive folder

A DJ downloads the event's music from its Drive folder,
`<root>/<season_year>/Events/<event_name>/`. Granting DJ or event manager
shares that folder with the person's email as a reader, creating the
folder if it does not exist yet. Revoking the last such role on the event
unshares it. The share and unshare run as durable Drive jobs, like the
event copies, so a Drive failure is retried and visible in the
Drive-jobs view. Renaming an event or changing its season moves where
copies go, so the event patch re-shares the new folder with the event's DJs
and event managers.

### Portals in deejaytools-com

Three portals, each reached per event from a "My events" list built from
`event_roles`. A person with more than one role on an event sees the
richer portal.

- **Coordinator portal**: the event's submissions, filterable by division,
  with each song's label and who it belongs to. Read-only.
- **DJ portal**: the event's sessions (create, edit, status, divisions,
  delete), the live floor-trial queue with its actions, run history, check
  in or withdraw a dancer and upload a song on their behalf, the event's
  submissions, and a link to the event's Drive folder.
- **Event-manager portal**: everything in the DJ portal, plus editing and
  deleting the event, and the event's staff list with grant and revoke for
  coordinator and DJ.

### Route additions

These go into ADR-0008's scope table as event-scoped rows (scope held
globally by admin, or on `event:<id>` by staff):

- **Grant model, scoped check and admin grant endpoints**:
  `event_staff_grants`, the three role definitions, `deejaytools.staff.read`
  and `deejaytools.staff.write` with the three staff routes, `event_roles`
  on `GET /v1/auth/me`, and the resolution helper that maps a session,
  queue entry or body `event_id` to `event:<id>` for the decision.
- **Coordinator API permissions**: `GET /v1/events/:id/song-submissions`
  (`deejaytools.entries.read`).
- **DJ API permissions**: event-scoped `queue.read`, `queue.manage`,
  `runs.read`, `sessions.write` and `delegation.act` on the routes above,
  `event_id` on uploads on behalf, and the Drive folder share and unshare
  jobs (with the re-share on event rename).
- **Event-manager API permissions**: event-scoped `events.write` on
  `PATCH` and `DELETE /v1/events/:id`, and `deejaytools.staff.write`
  limited to coordinator and DJ.

## Consequences

- An organizer can staff an event without making anyone an admin, and every
  staff grant is a reviewable row plus audited decisions.
- Routes that act on a session or queue entry now resolve its event before
  authorizing: one extra lookup per request for those routes, and the
  helper has to stay correct as routes change.
- Explicit grants are derived from `event_staff_grants`. The two must only
  be written together, by the grant and revoke paths. A grant edited by
  hand in SQL drifts from the staff list.
- Uploads on behalf gain an `event_id` for scoped callers. The admin path is
  unchanged, so existing callers keep working.
- DJs get Drive access to an event folder through a share, which outlives
  the API if the unshare job fails. The Drive-jobs view is where a stuck
  unshare shows.
