# Database schema reference

Source of truth: the numbered SQL files in [`migrations/`](../migrations/),
applied in order by `scripts/apply_migrations.py` and recorded in
`schema_migrations`. `001_baseline.sql` holds every table below except
`song_uploads` (003), `event_staff_grants` (007) and the `identity_*`
tables (002); later files alter what earlier ones built (005 drops
`users.role`, 006 adds `identity_audit_events.key_slot`, 008 adds
`drive_jobs.event_id` and `user_id`). The SQLAlchemy models in
`src/api_deejaytools/models.py` mirror the tables the app reads; they never
create or alter anything.

---

## Conventions

### Timestamps

Time columns in the app's tables are PostgreSQL `bigint` holding **epoch milliseconds**, not `timestamptz` or `timestamp`. The `identity_*` tables and `schema_migrations` are the exception: they use `timestamptz` (see [Identity store](#identity-store)).

When writing raw SQL or ad-hoc queries, compare against numeric ms (e.g. `WHERE checkin_opens_at <= 1735689600000`) or convert explicitly. Do not pass ISO strings to bigint columns without conversion. Mixing types (`to_timestamp(ms/1000.0)` vs direct bigint compare) is a common source of off-by-timezone or off-by-unit bugs.

Columns using this pattern: `created_at`, `updated_at`, `deleted_at` (where present), `checkin_opens_at`, `floor_trial_starts_at`, `floor_trial_ends_at`, `entered_queue_at`, `completed_at`, `next_attempt_at`, `granted_at` (`event_staff_grants`).

### Primary keys

Every app table's primary key is `text` holding a UUID string (`uuid.uuid4()`), **except**:

| Table | PK column | Value |
|-------|-----------|--------|
| `users` | `id` | Clerk user id (JWT `sub` claim), not a random UUID |
| `event_division_run_limits` | *(none)* | Logical key `(event_id, division_name)` via unique index `uq_event_division_run_limits_pk` |
| `song_uploads` | `song_id` | The song it stages (`songs.id`) |
| `event_staff_grants` | `(event_id, user_id, role)` | Composite |

The `identity_*` tables have their own keys; see [Identity store](#identity-store).

### Foreign-key delete behavior

Unless noted below, foreign keys use PostgreSQL's default, **`NO ACTION`**.

Explicit overrides:

| Table | Column | References | onDelete |
|-------|--------|------------|----------|
| `checkins` | `session_id` | `sessions.id` | `RESTRICT` |
| `checkins` | `(session_id, division_name)` | `session_divisions (session_id, division_name)` | `RESTRICT` |
| `checkins` | `entity_pair_id` | `pairs.id` | `RESTRICT` |
| `checkins` | `entity_solo_user_id` | `users.id` | `RESTRICT` |
| `checkins` | `entity_managed_partnership_id` | `managed_partnerships.id` | `RESTRICT` |
| `checkins` | `song_id` | `songs.id` | `RESTRICT` |
| `queue_entries` | `entity_pair_id` | `pairs.id` | `RESTRICT` |
| `queue_entries` | `entity_solo_user_id` | `users.id` | `RESTRICT` |
| `queue_entries` | `entity_managed_partnership_id` | `managed_partnerships.id` | `RESTRICT` |
| `runs` | `entity_pair_id` | `pairs.id` | `RESTRICT` |
| `runs` | `entity_solo_user_id` | `users.id` | `RESTRICT` |
| `runs` | `entity_managed_partnership_id` | `managed_partnerships.id` | `RESTRICT` |
| `runs` | `song_id` | `songs.id` | `RESTRICT` |
| `song_uploads` | `song_id` | `songs.id` | `CASCADE` |
| `event_staff_grants` | `event_id` | `events.id` | `CASCADE` |
| `event_staff_grants` | `user_id` | `users.id` | `CASCADE` |

### PostgreSQL enums

| Enum name | Values |
|-----------|--------|
| `session_status` | `scheduled`, `checkin_open`, `in_progress`, `completed`, `cancelled` |
| `partner_role` | `leader`, `follower` |
| `queue_type` | `priority`, `non_priority`, `active` |
| `queue_event_action` | `checked_in`, `promoted_to_active`, `run_completed`, `run_incomplete_rotated`, `withdrawn`, `moved_within_queue` |
| `initial_queue` | `priority`, `non_priority` |

Other enumerated columns are plain `text`, some with a `CHECK` constraint: `partners.kind`, `event_song_submissions.round`, `drive_jobs.kind` and `status` (no check), `song_uploads.status` and `event_staff_grants.role` (checked). Admin access is not a column: it is the `deejaytools-admin` role on the person's principal in `identity_principal_roles`.

### Entity columns (check-ins, queue, runs)

`checkins`, `queue_entries`, and `runs` each store exactly **one** of three nullable entity FK columns (CHECK constraint `ck_*_entity_xor`):

- `entity_pair_id` — registered pair (`pairs` row: logged-in user + `partners` row)
- `entity_solo_user_id` — solo check-in (`users.id`)
- `entity_managed_partnership_id` — managed partnership (names only, no linked accounts)

The same entity columns are **denormalized** onto `queue_entries` and `runs` for indexing and read performance; they must match the source check-in at insert time.

---

## Tables the app does not read

### `legacy_songs`

Historical catalog import table, created by the baseline. **No model in `models.py` and not referenced by application code.**

| Column | Type | Nullable | Default | Notes |
|--------|------|----------|---------|-------|
| `id` | `text` | NO | — | PK |
| `partnership` | `text` | NO | — | Partnership label string |
| `division` | `text` | YES | — | |
| `routine_name` | `text` | YES | — | |
| `descriptor` | `text` | YES | — | |
| `version` | `text` | YES | — | |
| `submitted_at` | `text` | YES | — | Text, not epoch ms |
| `created_at` | `bigint` | NO | — | Epoch ms |

**Indexes:** `idx_legacy_songs_division` on `(division)`.

Legacy metadata lives in `songs` rows whose `processed_filename` starts with `"[Legacy] "` (see `is_legacy_song()` in `src/api_deejaytools/song_records.py`).

### `schema_migrations`

Created and written by `scripts/apply_migrations.py`, not by a migration: one row per applied file.

| Column | Type | Nullable | Default | Notes |
|--------|------|----------|---------|-------|
| `filename` | `text` | NO | — | PK; e.g. `001_baseline.sql` |
| `applied_at` | `timestamptz` | NO | `now()` | |
| `duration_ms` | `integer` | YES | — | |

---

## Tables (dependency order)

### `users`

Clerk-authenticated accounts; anchor for ownership and audit.

| Column | Type | Nullable | Default | Notes |
|--------|------|----------|---------|-------|
| `id` | `text` | NO | — | PK; Clerk JWT `sub` |
| `email` | `text` | NO | — | Unique |
| `display_name` | `text` | YES | — | Optional profile display name |
| `first_name` | `text` | YES | — | |
| `last_name` | `text` | YES | — | |
| `created_at` | `bigint` | NO | — | Epoch ms |
| `updated_at` | `bigint` | NO | — | Epoch ms |

**Primary key:** `id`

**Unique constraints:** `users_email_unique` on `email`

**Relationships:** `partners`, `teams`, `managed_partnerships`, `pairs` (as `user_a_id`), `songs`, `events.created_by`, `sessions.created_by`, `event_song_submissions.submitted_by_user_id`, `checkins` (solo entity and submitter), `queue_entries.entity_solo_user_id`, `runs` (solo entity and `completed_by_user_id`), `queue_events.actor_user_id`, `partners.linked_user_id`, `event_staff_grants.user_id`

**Gotchas:** `id` is not generated by this app; it comes from Clerk on first sign-in (`POST /v1/auth/sync`). There is no role column: admin access is the `deejaytools-admin` grant in the identity store, and per-event staff roles are `event_staff_grants` rows. The person's principal is the `identity_principals` row whose `subject` is this `id`.

---

### `partners`

Per-user address book of dance partners and placeholder entities (solo/team/other).

| Column | Type | Nullable | Default | Notes |
|--------|------|----------|---------|-------|
| `id` | `text` | NO | — | PK; UUID |
| `user_id` | `text` | NO | — | Owning user |
| `first_name` | `text` | NO | — | |
| `last_name` | `text` | NO | — | |
| `partner_role` | `partner_role` | NO | `follower` | `leader` or `follower`; used when building processed filenames and pair labels |
| `kind` | `text` | NO | `partner` | Discriminator: `partner`, `solo`, `team`, or `other`. Non-`partner` rows are single-name placeholders (portal uploads); excluded from partner counts in admin APIs |
| `email` | `text` | YES | — | Optional contact email |
| `linked_user_id` | `text` | YES | — | Optional link to another `users` row if partner has an account |
| `created_at` | `bigint` | NO | — | Epoch ms |
| `updated_at` | `bigint` | NO | — | Epoch ms |

**Primary key:** `id`

**Foreign keys:**

| Column | → Table | onDelete |
|--------|---------|----------|
| `user_id` | `users.id` | NO ACTION |
| `linked_user_id` | `users.id` | NO ACTION |

**Indexes:** `idx_partners_user_id`, `idx_partners_linked_user_id`, `idx_partners_email`

**Relationships:** `pairs.partner_b_id`, `songs.partner_id`

**Gotchas:** `kind` is plain `text`, not an enum. List endpoints filter `kind = 'partner'` for real partners vs placeholders.

---

### `teams`

Named team identifiers owned by a user (e.g. for team routine uploads).

| Column | Type | Nullable | Default | Notes |
|--------|------|----------|---------|-------|
| `id` | `text` | NO | — | PK |
| `user_id` | `text` | NO | — | Owner |
| `identifier` | `text` | NO | — | Team name; title-cased on write |
| `created_at` | `bigint` | NO | — | Epoch ms |
| `updated_at` | `bigint` | NO | — | Epoch ms |

**Primary key:** `id`

**Foreign keys:** `user_id` → `users.id` (NO ACTION)

**Unique constraints:** `uq_teams_user_identifier` on `(user_id, identifier)`

**Indexes:** `idx_teams_user_id`

**Relationships:** None as FK; team songs use a `partners` placeholder row with `kind = 'team'`.

---

### `managed_partnerships`

User-managed partnership with leader/follower names only (neither side required to have an account).

| Column | Type | Nullable | Default | Notes |
|--------|------|----------|---------|-------|
| `id` | `text` | NO | — | PK |
| `user_id` | `text` | NO | — | Owner who manages the partnership |
| `leader_first_name` | `text` | NO | — | |
| `leader_last_name` | `text` | NO | — | |
| `follower_first_name` | `text` | NO | — | |
| `follower_last_name` | `text` | NO | — | |
| `created_at` | `bigint` | NO | — | Epoch ms |
| `updated_at` | `bigint` | NO | — | Epoch ms |
| `deleted_at` | `bigint` | YES | — | Soft delete; null = active |

**Primary key:** `id`

**Foreign keys:** `user_id` → `users.id` (NO ACTION)

**Indexes:** `idx_managed_partnerships_user_id`

**Relationships:** `songs.managed_partnership_id`, check-in/queue/run entity columns

**Gotchas:** Deletes are soft (`deleted_at` set); songs and live queue rows block hard delete via RESTRICT FKs.

---

### `pairs`

Mutual dance pair: logged-in user (`user_a_id`) plus a `partners` row (`partner_b_id`).

| Column | Type | Nullable | Default | Notes |
|--------|------|----------|---------|-------|
| `id` | `text` | NO | — | PK |
| `user_a_id` | `text` | NO | — | Always the authenticated user who owns the pair |
| `partner_b_id` | `text` | YES | — | `partners.id` for the other side |
| `created_at` | `bigint` | NO | — | Epoch ms |

**Primary key:** `id`

**Foreign keys:**

| Column | → Table | onDelete |
|--------|---------|----------|
| `user_a_id` | `users.id` | NO ACTION |
| `partner_b_id` | `partners.id` | NO ACTION |

**Unique constraints:** `uq_pairs_user_partner` on `(user_a_id, partner_b_id)`

**Relationships:** `checkins.entity_pair_id`, `queue_entries.entity_pair_id`, `runs.entity_pair_id`

**Gotchas:** Created via `POST /v1/pairs/find-or-create`; one row per `(user_a_id, partner_b_id)`.

---

### `songs`

Uploaded or metadata-only music records tied to a user (and optional partner or managed partnership).

| Column | Type | Nullable | Default | Notes |
|--------|------|----------|---------|-------|
| `id` | `text` | NO | — | PK |
| `user_id` | `text` | NO | — | Owner |
| `partner_id` | `text` | YES | — | `partners.id` for self/managed/special uploads |
| `managed_partnership_id` | `text` | YES | — | Mutually exclusive with `partner_id` for uploads |
| `display_name` | `text` | YES | — | User-facing title; API falls back to `processed_filename` then `original_filename` |
| `original_filename` | `text` | YES | — | Client filename at upload time |
| `drive_file_id` | `text` | YES | — | Google Drive file id after successful upload; null while pending or for legacy rows |
| `drive_folder_id` | `text` | YES | — | Google Drive folder id for the song's season/division tree |
| `processed_filename` | `text` | YES | — | Canonical Drive filename after processing (e.g. `Leader_Follower_division_2026_routine_v01.mp3`); legacy rows use `"[Legacy] …"` prefix |
| `division` | `text` | YES | — | Competition division label |
| `routine_name` | `text` | YES | — | Routine title segment in processed filename |
| `personal_descriptor` | `text` | YES | — | Optional disambiguator in processed filename |
| `season_year` | `text` | YES | — | Season year string (e.g. `"2026"`), fixed when the upload is accepted, from its UTC date (rolls over at 00:00 UTC on October 1, [ADR-0011](decisions/ADR-0011-season-year-rolls-over-october-1.md)) |
| `deleted_at` | `bigint` | YES | — | Soft delete; null = active |
| `created_at` | `bigint` | NO | — | Epoch ms |
| `updated_at` | `bigint` | NO | — | Epoch ms |

**Primary key:** `id`

**Foreign keys:**

| Column | → Table | onDelete |
|--------|---------|----------|
| `user_id` | `users.id` | NO ACTION |
| `partner_id` | `partners.id` | NO ACTION |
| `managed_partnership_id` | `managed_partnerships.id` | NO ACTION |

**Indexes:** `idx_songs_user_id`

**Relationships:** `event_song_submissions.song_id`, `checkins.song_id`, `runs.song_id`, `song_uploads.song_id`

**Gotchas:** Row may exist with `processed_filename` / Drive ids null while its build is staged in `song_uploads`. Legacy rows have no Drive file and cannot be used for check-in. `deleted_at` filters most user-facing lists.

---

### `events`

Multi-day competition container.

| Column | Type | Nullable | Default | Notes |
|--------|------|----------|---------|-------|
| `id` | `text` | NO | — | PK |
| `name` | `text` | NO | — | |
| `start_date` | `text` | NO | — | `YYYY-MM-DD` (validated in API) |
| `end_date` | `text` | NO | — | `YYYY-MM-DD`; must be `>= start_date` |
| `timezone` | `text` | NO | `America/Chicago` | IANA timezone; session timestamps displayed in this zone |
| `season_year` | `text` | YES | — | Season the event belongs to (e.g. `"2027"`). Set on create from the body or derived from `start_date` (October 1 rollover, [ADR-0011](decisions/ADR-0011-season-year-rolls-over-october-1.md)); **not** recomputed when `start_date` changes. Reads fall back to deriving from `start_date` when null. Decides the Drive year folder for event copies |
| `created_by` | `text` | YES | — | Admin who created the event |
| `created_at` | `bigint` | NO | — | Epoch ms |
| `updated_at` | `bigint` | NO | — | Epoch ms |

**Primary key:** `id`

**Foreign keys:** `created_by` → `users.id` (NO ACTION)

**Check constraints:** `ck_events_date_range`: `start_date <= end_date`

**Relationships:** `sessions`, `event_song_submissions`, `event_division_run_limits`, `event_staff_grants`, `runs.event_id` (denormalized)

**Gotchas:** Event "status" (`upcoming` / `active` / `completed`) is derived from dates in API code, not stored.

---

### `event_song_submissions`

Links a song to an event so it can be used for check-in at that event.

| Column | Type | Nullable | Default | Notes |
|--------|------|----------|---------|-------|
| `id` | `text` | NO | — | PK |
| `event_id` | `text` | NO | — | |
| `song_id` | `text` | NO | — | |
| `submitted_by_user_id` | `text` | NO | — | User who submitted the link |
| `division` | `text` | YES | — | Division entered for this event. Null = use the song's division. Stored trimmed; an empty value is stored as null |
| `round` | `text` | YES | — | `prelims_and_finals` \| `prelims_only` \| `finals_only` (plain text, not an enum). Null = `prelims_and_finals` |
| `drive_copy_file_id` | `text` | YES | — | Drive id of the per-event copy. Null until the queued `copy` job succeeds (and stays null when the song has no Drive file). See [DRIVE.md](./DRIVE.md#drive_jobs-queue) |
| `created_at` | `bigint` | NO | — | Epoch ms |

**Primary key:** `id`

**Foreign keys:**

| Column | → Table | onDelete |
|--------|---------|----------|
| `event_id` | `events.id` | NO ACTION |
| `song_id` | `songs.id` | NO ACTION |
| `submitted_by_user_id` | `users.id` | NO ACTION |

**Unique constraints:** `uq_event_song_submissions_event_song` on `(event_id, song_id)`

**Indexes:** `idx_event_song_submissions_event_id`, `idx_event_song_submissions_submitted_by_user_id`

**Relationships:** `events`, `songs`, `users`

---

### `event_division_run_limits`

Optional per-event cap on priority-eligible runs for a division across all sessions in the event.

| Column | Type | Nullable | Default | Notes |
|--------|------|----------|---------|-------|
| `event_id` | `text` | NO | — | Part of logical PK |
| `division_name` | `text` | NO | — | Part of logical PK |
| `priority_run_limit` | `integer` | NO | — | Max priority runs counted toward admission across the whole event |

**Primary key:** None as a single column; **`uq_event_division_run_limits_pk`** unique index on `(event_id, division_name)` enforces one row per pair.

**Foreign keys:** `event_id` → `events.id` (NO ACTION)

**Relationships:** `events`

**Gotchas:** Absence of a row means no event-level limit for that division. Used by `determine_initial_queue()` (`src/api_deejaytools/queue/admission.py`) when assigning priority vs non-priority at check-in.

---

### `sessions`

One floor-trial time block (check-in window + floor trial window).

| Column | Type | Nullable | Default | Notes |
|--------|------|----------|---------|-------|
| `id` | `text` | NO | — | PK |
| `event_id` | `text` | YES | — | Parent event; nullable for standalone sessions |
| `name` | `text` | NO | — | |
| `date` | `text` | YES | — | Optional display/sort date string; format not validated in schema |
| `checkin_opens_at` | `bigint` | NO | — | Epoch ms; check-in allowed when `now >= checkin_opens_at` |
| `floor_trial_starts_at` | `bigint` | NO | — | Epoch ms; floor trial / in-progress boundary |
| `floor_trial_ends_at` | `bigint` | NO | — | Epoch ms; session end |
| `active_priority_max` | `integer` | NO | `6` | Max entries in active queue when promoting from priority queue |
| `active_non_priority_max` | `integer` | NO | `4` | Max active entries when promoting from non-priority (requires empty priority queue) |
| `status` | `session_status` | NO | `scheduled` | Persisted status; API also derives status from wall clock for clients |
| `created_by` | `text` | YES | — | Admin creator |
| `created_at` | `bigint` | NO | — | Epoch ms |

**Primary key:** `id`

**Foreign keys:**

| Column | → Table | onDelete |
|--------|---------|----------|
| `event_id` | `events.id` | NO ACTION |
| `created_by` | `users.id` | NO ACTION |

**Indexes:** `idx_sessions_event_id`

**Check constraints:** `ck_sessions_active_caps`: `active_non_priority_max <= active_priority_max AND active_priority_max >= 0`

**Relationships:** `session_divisions`, `checkins`, `queue_entries`, `runs`, `queue_events`

**Gotchas:** `status = cancelled` is a manual admin override and is preserved by the scheduler. Non-cancelled status shown to clients is recomputed from timestamps in `derive_status()` (`src/api_deejaytools/routers/sessions.py`).

---

### `session_divisions`

Divisions configured for check-in on a specific session.

| Column | Type | Nullable | Default | Notes |
|--------|------|----------|---------|-------|
| `id` | `text` | NO | — | PK |
| `session_id` | `text` | NO | — | |
| `division_name` | `text` | NO | — | Must match `checkins.division_name` |
| `is_priority` | `boolean` | NO | `false` | Whether division is priority-eligible at all |
| `priority_run_limit` | `integer` | NO | `0` | Session-level X: runs 1..X in this division/session can be priority (if `is_priority`) |
| `sort_order` | `integer` | NO | `0` | UI ordering |

**Primary key:** `id`

**Foreign keys:** `session_id` → `sessions.id` (NO ACTION)

**Unique constraints:** `uq_session_divisions_session_division` on `(session_id, division_name)`

**Relationships:** `sessions`; `checkins (session_id, division_name)` via `fk_checkins_session_division`

**Gotchas:** Deleting or renaming divisions is restricted when check-ins exist (`fk_checkins_session_division`, and the `PUT /v1/sessions/{id}/divisions` handler in `src/api_deejaytools/routers/sessions.py`).

---

### `checkins`

Append-only record of one check-in submission (intent to run in a session + division).

| Column | Type | Nullable | Default | Notes |
|--------|------|----------|---------|-------|
| `id` | `text` | NO | — | PK |
| `session_id` | `text` | NO | — | |
| `division_name` | `text` | NO | — | Must exist in `session_divisions` for this session |
| `entity_pair_id` | `text` | YES | — | XOR entity FK |
| `entity_solo_user_id` | `text` | YES | — | XOR entity FK |
| `entity_managed_partnership_id` | `text` | YES | — | XOR entity FK |
| `song_id` | `text` | NO | — | Song used for this check-in |
| `submitted_by_user_id` | `text` | NO | — | User who submitted the form |
| `initial_queue` | `initial_queue` | NO | — | Snapshot: `priority` or `non_priority` at check-in time |
| `notes` | `text` | YES | — | Dancer instructions for the deejay |
| `created_at` | `bigint` | NO | — | Epoch ms |

**Primary key:** `id`

**Foreign keys:**

| Column | → Table | onDelete |
|--------|---------|----------|
| `session_id` | `sessions.id` | RESTRICT |
| `(session_id, division_name)` | `session_divisions (session_id, division_name)` | RESTRICT (`fk_checkins_session_division`) |
| `entity_pair_id` | `pairs.id` | RESTRICT |
| `entity_solo_user_id` | `users.id` | RESTRICT |
| `entity_managed_partnership_id` | `managed_partnerships.id` | RESTRICT |
| `song_id` | `songs.id` | RESTRICT |
| `submitted_by_user_id` | `users.id` | NO ACTION |

**Indexes:** `idx_checkins_session_id`, `idx_checkins_entity_pair_id`, `idx_checkins_entity_solo_user_id`, `idx_checkins_entity_managed_partnership_id`

**Check constraints:** `ck_checkins_entity_xor` — exactly one entity column non-null

**Relationships:** `queue_entries` (1:1 while live), `runs` (0:1 after completion), `queue_events`

**Gotchas:** Never updated after insert. Re-check-in after a run creates a **new** row. `initial_queue` is historical; live queue state is in `queue_entries.queue_type`.

---

### `queue_entries`

Mutable live queue state: one row per entity currently in priority, non-priority, or active queue.

| Column | Type | Nullable | Default | Notes |
|--------|------|----------|---------|-------|
| `id` | `text` | NO | — | PK |
| `checkin_id` | `text` | NO | — | Unique; ties row to source check-in |
| `session_id` | `text` | NO | — | Denormalized from check-in |
| `entity_pair_id` | `text` | YES | — | Denormalized entity FK |
| `entity_solo_user_id` | `text` | YES | — | Denormalized entity FK |
| `entity_managed_partnership_id` | `text` | YES | — | Denormalized entity FK |
| `queue_type` | `queue_type` | NO | — | Current queue: `priority`, `non_priority`, or `active` |
| `position` | `integer` | NO | — | 1-based position within `(session_id, queue_type)` |
| `entered_queue_at` | `bigint` | NO | — | Epoch ms; reset when moving between queues |

**Primary key:** `id`

**Foreign keys:**

| Column | → Table | onDelete |
|--------|---------|----------|
| `checkin_id` | `checkins.id` | NO ACTION |
| `session_id` | `sessions.id` | NO ACTION |
| `entity_pair_id` | `pairs.id` | RESTRICT |
| `entity_solo_user_id` | `users.id` | RESTRICT |
| `entity_managed_partnership_id` | `managed_partnerships.id` | RESTRICT |

**Unique constraints:**

| Name | Columns | Notes |
|------|---------|-------|
| `queue_entries_checkin_id_unique` | `checkin_id` | One live row per check-in |
| `uq_queue_entries_session_queue_position` | `(session_id, queue_type, position)` | No two entries at same slot |
| `uq_queue_entries_session_pair_live` | `(session_id, entity_pair_id)` | Partial: `WHERE entity_pair_id IS NOT NULL` |
| `uq_queue_entries_session_solo_live` | `(session_id, entity_solo_user_id)` | Partial: `WHERE entity_solo_user_id IS NOT NULL` |
| `uq_queue_entries_session_managed_live` | `(session_id, entity_managed_partnership_id)` | Partial: `WHERE entity_managed_partnership_id IS NOT NULL` |

**Indexes:** `idx_queue_entries_session_id`

**Check constraints:** `ck_queue_entries_entity_xor`, `ck_queue_entries_position_positive` (`position >= 1`)

**Relationships:** `checkins`, `sessions`

**Gotchas:** Row deleted when entity leaves all queues (complete, withdraw, etc.). Partial unique indexes enforce at most one live entry per entity per session.

---

### `runs`

Append-only record of a completed floor trial run.

| Column | Type | Nullable | Default | Notes |
|--------|------|----------|---------|-------|
| `id` | `text` | NO | — | PK |
| `checkin_id` | `text` | NO | — | Unique; the check-in that produced this run |
| `session_id` | `text` | NO | — | Denormalized |
| `event_id` | `text` | YES | — | Denormalized from session for event-level run counts |
| `division_name` | `text` | NO | — | |
| `entity_pair_id` | `text` | YES | — | Denormalized entity FK |
| `entity_solo_user_id` | `text` | YES | — | Denormalized entity FK |
| `entity_managed_partnership_id` | `text` | YES | — | Denormalized entity FK |
| `song_id` | `text` | NO | — | |
| `completed_at` | `bigint` | NO | — | Epoch ms |
| `completed_by_user_id` | `text` | NO | — | Admin who marked run complete |

**Primary key:** `id`

**Foreign keys:**

| Column | → Table | onDelete |
|--------|---------|----------|
| `checkin_id` | `checkins.id` | NO ACTION |
| `session_id` | `sessions.id` | NO ACTION |
| `event_id` | `events.id` | NO ACTION |
| `entity_pair_id` | `pairs.id` | RESTRICT |
| `entity_solo_user_id` | `users.id` | RESTRICT |
| `entity_managed_partnership_id` | `managed_partnerships.id` | RESTRICT |
| `song_id` | `songs.id` | RESTRICT |
| `completed_by_user_id` | `users.id` | NO ACTION |

**Unique constraints:** `runs_checkin_id_unique` on `checkin_id`

**Indexes:** `idx_runs_session_id`, `idx_runs_event_id`, `idx_runs_pair_division` on `(entity_pair_id, division_name)`, `idx_runs_solo_division` on `(entity_solo_user_id, division_name)`, `idx_runs_managed_division` on `(entity_managed_partnership_id, division_name)`

**Check constraints:** `ck_runs_entity_xor`

**Relationships:** `checkins`, `sessions`, `events`, `songs`, `users`

**Gotchas:** Run counts for priority admission are `COUNT(*)` from this table per session/division/entity (and optionally event). Withdraw does **not** create a run.

---

### `queue_events`

Append-only audit log of queue state transitions.

| Column | Type | Nullable | Default | Notes |
|--------|------|----------|---------|-------|
| `id` | `text` | NO | — | PK |
| `session_id` | `text` | NO | — | |
| `checkin_id` | `text` | YES | — | Nullable for session-level events not tied to one check-in |
| `action` | `queue_event_action` | NO | — | See enum above |
| `from_queue` | `queue_type` | YES | — | Source queue |
| `from_position` | `integer` | YES | — | Source position |
| `to_queue` | `queue_type` | YES | — | Destination queue |
| `to_position` | `integer` | YES | — | Destination position |
| `actor_user_id` | `text` | YES | — | Staff member or check-in submitter; null for the scheduler |
| `reason` | `text` | YES | — | Optional admin note (e.g. withdraw, incomplete) |
| `created_at` | `bigint` | NO | — | Epoch ms |

**Primary key:** `id`

**Foreign keys:**

| Column | → Table | onDelete |
|--------|---------|----------|
| `session_id` | `sessions.id` | NO ACTION |
| `checkin_id` | `checkins.id` | NO ACTION |
| `actor_user_id` | `users.id` | NO ACTION |

**Indexes:** `idx_queue_events_session_created` on `(session_id, created_at)`

**Relationships:** `sessions`, `checkins`, `users`

**Gotchas:** No read API; written by `src/api_deejaytools/queue/audit.py`. [ADR-0006](decisions/ADR-0006-floor-trial-queue-model-as-built.md) gives the action semantics.

---

### `drive_jobs`

Durable queue of background Google Drive work: per-event copies of submitted songs, renames of those copies, deprecation of copies whose submission was deleted, the share and unshare of an event's folder with its DJs and event managers, and the share of a song's file with a partner set after it was built. Drained by the scheduler tick. Behaviour, enqueue sites and the state machine: [DRIVE.md](./DRIVE.md#drive_jobs-queue).

| Column | Type | Nullable | Default | Notes |
|--------|------|----------|---------|-------|
| `id` | `text` | NO | — | PK (UUID) |
| `kind` | `text` | NO | — | `copy` \| `rename` \| `trash` \| `share_event_folder` \| `unshare_event_folder` \| `share_song` (plain text, not an enum) |
| `submission_id` | `text` | YES | — | Set for `copy` and `rename`. **Not a FK**: the job outlives a deleted submission |
| `file_id` | `text` | YES | — | Set for `trash`: the Drive file to move to `_deprecated`. For `share_song`: the song's Drive file. For `share_event_folder`, the folder it shared, recorded when it runs |
| `event_id` | `text` | YES | — | Set for `share_event_folder` and `unshare_event_folder`: the event. **Not a FK**: an unshare runs after an event delete |
| `user_id` | `text` | YES | — | Set for the two folder kinds: the person (`users.id`) the folder is shared with or unshared from. Not a FK |
| `status` | `text` | NO | `'pending'` | `pending` \| `running` \| `done` \| `failed` (plain text) |
| `attempts` | `integer` | NO | `0` | Failed attempts so far. Reset to 0 by the admin retry endpoint; not incremented by lease reclaim |
| `next_attempt_at` | `bigint` | NO | — | Epoch ms; a `pending` job is claimable once this is `<= now` |
| `last_error` | `text` | YES | — | Message of the most recent failure; cleared on success |
| `created_at` | `bigint` | NO | — | Epoch ms |
| `updated_at` | `bigint` | NO | — | Epoch ms. Doubles as the lease start while `running`: a `running` row older than 10 minutes is returned to `pending` |

**Primary key:** `id`

**Foreign keys:** none

**Indexes:** `idx_drive_jobs_due` on `(status, next_attempt_at)` (the claim query), `idx_drive_jobs_submission_id` on `(submission_id)`

**Gotchas:** Rows are never deleted; `done` jobs accumulate. Claimed with `FOR UPDATE SKIP LOCKED`, so concurrent replicas take disjoint batches.

---

### `song_uploads`

Staging for a song's build ([ADR-0001](decisions/ADR-0001-durable-song-builds.md)): an accepted upload's bytes and progress until its Drive build finishes. The row is deleted when the build succeeds; deleting the song deletes it too. Drained by the scheduler tick. Behaviour: [DRIVE.md](./DRIVE.md).

| Column | Type | Nullable | Default | Notes |
|--------|------|----------|---------|-------|
| `song_id` | `text` | NO | — | PK; the song being built |
| `data` | `bytea` | NO | — | The uploaded file |
| `mime_type` | `text` | NO | — | |
| `original_filename` | `text` | NO | — | Client filename |
| `status` | `text` | NO | `'pending'` | `pending` \| `running` \| `failed` (CHECK) |
| `attempts` | `integer` | NO | `0` | Failed attempts so far |
| `next_attempt_at` | `bigint` | NO | — | Epoch ms; a `pending` row is due once this is `<= now` |
| `last_error` | `text` | YES | — | Message of the most recent failure |
| `claim_id` | `text` | YES | — | The running build that owns the row. A build renews `updated_at` while it runs; one that stops renewing for the lease is taken over under a new `claim_id`, and the old one's later writes match nothing |
| `season_year` | `text` | YES | — | Fixed when the upload is accepted |
| `processed_filename` | `text` | YES | — | Reserved under a per-slot advisory lock, so concurrent uploads for the same slot get distinct version numbers |
| `drive_file_id` | `text` | YES | — | Recorded as soon as the Drive upload returns, so a retry never uploads twice |
| `drive_folder_id` | `text` | YES | — | |
| `uploaded_by_user_id` | `text` | YES | — | Who uploaded it, when that is not the song's owner (an upload on someone's behalf). Null reads as "uploaded by the owner". Not a FK |
| `created_at` | `bigint` | NO | — | Epoch ms |
| `updated_at` | `bigint` | NO | — | Epoch ms; doubles as the lease while `running` |

**Primary key:** `song_id`

**Foreign keys:** `song_id` → `songs.id` (CASCADE)

**Indexes:** `idx_song_uploads_due` on `(status, next_attempt_at)`

---

### `event_staff_grants`

Who is on an event's staff, and as what ([ADR-0012](decisions/ADR-0012-per-event-staff-roles.md)). Each row is written together with one `identity_explicit_grants` row per scope of the role, on the resource `event:<event_id>`, by the grant and revoke routes and the event delete (`src/api_deejaytools/staff.py`); never one without the other.

| Column | Type | Nullable | Default | Notes |
|--------|------|----------|---------|-------|
| `event_id` | `text` | NO | — | Part of PK |
| `user_id` | `text` | NO | — | Part of PK; the staff member |
| `role` | `text` | NO | — | Part of PK. `coordinator` \| `dj` \| `event-manager` (CHECK) |
| `granted_by` | `text` | NO | — | `users.id` of the admin or event manager who granted it. Not a FK |
| `granted_at` | `bigint` | NO | — | Epoch ms |

**Primary key:** `(event_id, user_id, role)`

**Foreign keys:**

| Column | → Table | onDelete |
|--------|---------|----------|
| `event_id` | `events.id` | CASCADE |
| `user_id` | `users.id` | CASCADE |

**Indexes:** `idx_event_staff_grants_user` on `(user_id)` (a person's roles across events, for `GET /v1/auth/me`)

**Gotchas:** The cascade removes these rows but not the matching `identity_explicit_grants`; the app's event delete removes both.

---

## Identity store

The principal store the identity library (`miniapppolis-identity`) reads to authorize every request ([ADR-0008](decisions/ADR-0008-replacement-api-identity-authorization.md)). Created by `002_identity_store.sql`, which also seeds the global roles `deejaytools-dancer` and `deejaytools-admin` with their scopes; `007_event_staff_grants.sql` adds the per-event roles `deejaytools-event-coordinator`, `deejaytools-event-dj` and `deejaytools-event-manager` and the admin's two staff scopes. The migrations are the list of which scope each role holds. The issuer row and each person's principal and `deejaytools-dancer` grant are per environment, written by `scripts/backfill_principals.py` on every deploy and by `POST /v1/auth/sync`.

Unlike the app's tables, these use `timestamptz` and UUID keys generated by the database (`gen_random_uuid()`).

### `identity_issuers`

| Column | Type | Nullable | Default | Notes |
|--------|------|----------|---------|-------|
| `issuer` | `text` | NO | — | PK; the token `iss` (this environment's Clerk instance) |
| `display_name` | `text` | NO | `''` | |
| `jwks_url` | `text` | YES | — | |
| `enabled` | `boolean` | NO | `true` | |
| `created_at` | `timestamptz` | NO | `now()` | |

### `identity_roles`

| Column | Type | Nullable | Default | Notes |
|--------|------|----------|---------|-------|
| `name` | `text` | NO | — | PK; CHECK `^[a-z][a-z0-9-]*$` |
| `description` | `text` | NO | `''` | |
| `created_at` | `timestamptz` | NO | `now()` | |

### `identity_role_scopes`

| Column | Type | Nullable | Default | Notes |
|--------|------|----------|---------|-------|
| `role_name` | `text` | NO | — | → `identity_roles.name` (CASCADE) |
| `scope` | `text` | NO | — | Three dot-separated parts, e.g. `deejaytools.queue.manage` (CHECK) |

**Primary key:** `(role_name, scope)`

### `identity_principals`

| Column | Type | Nullable | Default | Notes |
|--------|------|----------|---------|-------|
| `id` | `uuid` | NO | `gen_random_uuid()` | PK |
| `kind` | `text` | NO | — | `human` \| `machine` (CHECK) |
| `issuer` | `text` | NO | — | → `identity_issuers.issuer` (NO ACTION) |
| `subject` | `text` | NO | — | Token `sub`; for a person, their `users.id` |
| `display_name` | `text` | NO | `''` | |
| `email` | `text` | YES | — | |
| `status` | `text` | NO | `'active'` | `active` \| `suspended` (CHECK) |
| `created_at` | `timestamptz` | NO | `now()` | |
| `last_seen_at` | `timestamptz` | YES | — | |

**Unique constraints:** `(issuer, subject)`

**Indexes:** `idx_identity_principals_issuer_subject` on `(issuer, subject)`, `idx_identity_principals_kind` on `(kind)`

### `identity_principal_roles`

Global role grants. The per-event staff roles are never written here (that would make them global); they are `identity_explicit_grants`.

| Column | Type | Nullable | Default | Notes |
|--------|------|----------|---------|-------|
| `principal_id` | `uuid` | NO | — | → `identity_principals.id` (CASCADE) |
| `role_name` | `text` | NO | — | → `identity_roles.name` (RESTRICT) |
| `granted_by` | `text` | NO | `''` | |
| `granted_at` | `timestamptz` | NO | `now()` | |

**Primary key:** `(principal_id, role_name)`

### `identity_explicit_grants`

One scope on one resource, e.g. `deejaytools.queue.manage` on `event:<event_id>`.

| Column | Type | Nullable | Default | Notes |
|--------|------|----------|---------|-------|
| `principal_id` | `uuid` | NO | — | → `identity_principals.id` (CASCADE) |
| `scope` | `text` | NO | — | Same CHECK as `identity_role_scopes.scope` |
| `resource` | `text` | NO | — | e.g. `event:<event_id>` |
| `granted_by` | `text` | NO | `''` | |
| `granted_at` | `timestamptz` | NO | `now()` | |

**Primary key:** `(principal_id, scope, resource)`

### `identity_audit_events`

One row per authorization decision the library records.

| Column | Type | Nullable | Default | Notes |
|--------|------|----------|---------|-------|
| `event_id` | `uuid` | NO | `gen_random_uuid()` | PK |
| `occurred_at` | `timestamptz` | NO | `now()` | |
| `enforcement_point` | `text` | NO | — | |
| `principal_id` | `uuid` | YES | — | **Not a FK**: the trail survives deletion of the principal |
| `principal_kind` | `text` | YES | — | `human` \| `machine` (CHECK) |
| `issuer` | `text` | YES | — | |
| `subject` | `text` | YES | — | |
| `scope` | `text` | NO | — | |
| `resource` | `text` | YES | — | |
| `allowed` | `boolean` | NO | — | |
| `reason` | `text` | NO | — | |
| `request_id` | `text` | YES | — | |
| `key_slot` | `text` | YES | — | `current` \| `previous` (CHECK): which of a machine's keys proved a key-authenticated request; null for every other credential |

**Indexes:** `idx_identity_audit_occurred_at` on `(occurred_at DESC)`, `idx_identity_audit_principal` on `(principal_id, occurred_at DESC)`, `idx_identity_audit_enforcement_point` on `(enforcement_point, occurred_at DESC)`, `idx_identity_audit_previous_key` on `(subject, occurred_at DESC) WHERE key_slot = 'previous'`

---

## Entity-relationship diagram (core graph)

```
users ─────────────────────────────────────────────────────────────────┐
  │                                                                    │
  ├──< partners ──< pairs ──< checkins ──< queue_entries              │
  │       │              │         │              │                    │
  │       │              │         ├──< runs ──────┤                    │
  │       │              │         └──< queue_events                   │
  │       └──< songs ────┴─────────────────────────┘                    │
  │              │                                                       │
  ├──< teams    ├──< event_song_submissions >── events                  │
  │              │                              │  │                     │
  └──< managed_partnerships ────────────────────┘  ├──< sessions        │
         │                                          │       │            │
         └──── (entity_managed_partnership_id) ─────┘       ├──< session_divisions
                                                             └──< event_division_run_limits

Legend:
  ──<  one-to-many (child holds FK to parent)
  pairs / solo user / managed_partnership are alternate entity FKs on checkins,
  queue_entries, and runs (exactly one set per row).

Not drawn: song_uploads (songs 1:0..1), event_staff_grants (events × users),
drive_jobs (no FKs) and the identity_* tables (linked to users by subject).
```

---

## Reading the queue tables

The floor-trial queue splits **historical intent** (`checkins`), **live ordering** (`queue_entries`), **completed outcomes** (`runs`), and **audit** (`queue_events`). Full state-machine detail lives in [ADR-0006: Floor-trial queue model, as built](decisions/ADR-0006-floor-trial-queue-model-as-built.md); this section orients readers to the tables only.

### Lifecycle

1. **Check in** — inserts `checkins` (with `initial_queue` snapshot) + `queue_entries` at the bottom of `priority` or `non_priority` + `queue_events` (`checked_in`).
2. **Promote** — moves a row from `priority` or `non_priority` into `active` at the bottom eligible slot; updates `queue_type`, `position`, `entered_queue_at`.
3. **Run complete** — deletes active slot-1 `queue_entries` row, inserts `runs`, compacts active queue.
4. **Run incomplete / withdraw** — mutates or deletes `queue_entries` without inserting `runs` (withdraw).

An entity has at most **one** `queue_entries` row per session (partial unique indexes on entity columns). When that row is gone, the entity may check in again (new `checkins` id).

### `queue_type` is current state, not a permanent category

`queue_entries.queue_type` is **`priority`**, **`non_priority`**, or **`active`** — the queue the row **currently occupies**. It changes when an entry is promoted (`non_priority` → `active`, etc.). It is not the same as `checkins.initial_queue`, which freezes the admission decision at check-in time.

`active` is the on-floor working set: position `1` is currently running; `2..N` are up next. Priority vs non-priority provenance matters for **promotion rules**, not for ordering within active.

### `queue_events` vs `queue_entries`

| Table | Mutability | Purpose |
|-------|------------|---------|
| `queue_entries` | Updated and deleted | Current queue snapshots |
| `queue_events` | Append-only | Audit trail of transitions (`action`, `from_*`, `to_*`) |

Replay a session timeline with `SELECT * FROM queue_events WHERE session_id = $1 ORDER BY created_at`.

### Position compaction

Positions are **1-based** and **dense** within each `(session_id, queue_type)`: no gaps after deletes. When a row is removed, `compact_after_removal()` in `src/api_deejaytools/queue/compaction.py` shifts all higher positions down by one inside the same transaction (two-step sentinel update to avoid unique-index races on `(session_id, queue_type, position)`).

New append operations use `next_bottom_position()` (`MAX(position) + 1`).

### Related reading

- [ADR-0006: Floor-trial queue model, as built](decisions/ADR-0006-floor-trial-queue-model-as-built.md) — admission, promotion caps, state transitions (supersedes [ADR-0005](decisions/ADR-0005-floor-trial-queue-model.md))
- `src/api_deejaytools/queue/admission.py` — `determine_initial_queue()`
- `src/api_deejaytools/queue/compaction.py` — compaction helpers
