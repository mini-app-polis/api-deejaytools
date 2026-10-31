# Google Drive integration

Every song file this API accepts ends up in Google Drive. Two mechanisms
put it there, with different failure models:

| Path | Trigger | Runs | On failure |
|------|---------|------|------------|
| **Upload pipeline** | Final chunk of `POST /v1/songs/upload/chunk` | Background task after the response is sent, its bytes staged in `song_uploads`; the scheduler resumes it after a restart ([ADR-0001](decisions/ADR-0001-durable-song-builds.md)) | Retried with backoff, `failed` after 10 attempts; a song nothing references yet is hard-deleted instead, and the user retries the upload |
| **`drive_jobs` queue** | Event submission created, removed, or cascaded away; song or managed partnership deleted; admin backfill; an event's DJs and event managers granted, revoked or moved to a new folder; a partner set on a built song | Scheduler tick (`run_tick()` → `process_drive_jobs()`) | Rescheduled with backoff; `failed` after 10 attempts; admin retry endpoint |

The scheduler also trashes the folders of deleted events once they are empty;
see [Event folder sweep](#event-folder-sweep).

Source: [`services/drive.py`](../src/api_deejaytools/services/drive.py),
[`services/drive_jobs/`](../src/api_deejaytools/services/drive_jobs/),
[`services/song_builds/`](../src/api_deejaytools/services/song_builds/) (the background build),
[`routers/song_uploads.py`](../src/api_deejaytools/routers/song_uploads.py) (the chunk handler),
[`submissions.py`](../src/api_deejaytools/submissions.py) (`resolve_submission_filename`),
[`domain.py`](../src/api_deejaytools/domain.py) (`season_year_for`).
Tag contents written into each file are specified in [AUDIO-TAGGING.md](./AUDIO-TAGGING.md).

---

## Configuration

| Env var | Purpose |
|---------|---------|
| `GOOGLE_CREDENTIALS_JSON` | The service account's JSON key file; its `client_email` and `private_key` are what the API authenticates as. Parsed leniently, so real newlines inside the key are accepted. |
| `GOOGLE_DRIVE_PARENT_FOLDER_ID` | Root folder everything below lives in. |

Both are required by every Drive call; if either is missing (or the JSON lacks
either field) the call fails with
`Google Drive environment variables are not configured`. Nothing checks this at
boot, so an unconfigured deployment fails per upload (build retried, or an
unreferenced song deleted) and per job (retried, then `failed`).

**Scope:** `https://www.googleapis.com/auth/drive.file`. The service account can
only see and modify files **it created**. Copy, rename and trash therefore only
work on files that came through `upload_song_file` (or copies of them).

Every call passes `supportsAllDrives: true` (and `includeItemsFromAllDrives: true`
on list), so the root may be in a shared drive.

---

## Folder layout

```
<root>/
├── <seasonYear>/                       e.g. 2027
│   ├── <division>/                     uploaded originals
│   │   └── <processed_filename>
│   └── Events/
│       └── <event name>/
│           └── <division>/             event copies (both rounds)
│               ├── <filename>
│               ├── Finals/<filename>   round = finals_only
│               └── Prelims/<filename>  round = prelims_only
└── _deprecated/                        soft-deleted files, flat
```

**Folder names** for season year, division, event name and round subfolder pass
through `sanitize_folder_name`: `/` and `\` become `-`, runs of whitespace collapse
to one space, the result is trimmed, and an empty result becomes `unknown`.
`Events` and `_deprecated` are literal.

**Find-or-create:** a folder is looked up by exact name, parent, folder MIME type
and `trashed=false` (single quotes and backslashes in the name are escaped for
the Drive query); the first match wins, otherwise it is created. Resolved ids are
cached process-locally for **1 hour** keyed by `(parentId, name)`. The whole
cache is dropped whenever an event copy fails, so a folder deleted out from
under the cache recovers on the next retry. Nothing else clears it: an upload or a
`trash` job that hits a stale cached folder keeps failing until the hour is up.
A folder the [event folder sweep](#event-folder-sweep) trashes is dropped from
the cache at once.

### Season year

Seasons roll over on **October 1**: October–December belong to the next calendar
year (2026-09-30 → `"2026"`, 2026-10-01 → `"2027"`;
[ADR-0011](decisions/ADR-0011-season-year-rolls-over-october-1.md)).

| Used for | Derived from |
|----------|--------------|
| Uploaded original (folder, filename, `songs.season_year`, year tag) | The time the upload is accepted, by its **UTC** date (`season_year_now()`), so it rolls over at 00:00 UTC on October 1 wherever the server runs. Fixed in `song_uploads.season_year` then, so a retried build keeps it |
| Event copy folder | `events.season_year` if non-blank, else parsed from `events.start_date` (`YYYY-MM-DD`, parsed as calendar fields — not via `Date`) |

The event's season, not the song's, decides where a copy goes: one event's
submissions share one year folder.

---

## Upload pipeline

`POST /v1/songs/upload/chunk` (request/response contract in
[API.md](./API.md#post-v1songsuploadchunk)).

### Chunk staging

1. Every request first sweeps `/tmp/dj-upload-chunks/*`, deleting directories
   whose mtime is older than **2 hours**. Fire-and-forget; never fails the request.
2. Fields are validated (order and messages in API.md's error table), then the chunk
   is written to `/tmp/dj-upload-chunks/<callerUserId>_<upload_id>/chunk_<index, zero-padded to 6>`.
   The directory is keyed by the **caller**, even for an admin uploading
   `on_behalf_of_user_id`. Re-sending an index overwrites it.

   The staging area is bounded: a caller may have at most **3** uploads staged
   at once (the first chunk of a fourth answers `429 TOO_MANY_UPLOADS`), and a
   chunk that takes an upload's staged chunks past **110 MiB** answers
   `400 File exceeds 100 MB limit` and removes them.

   `chunk_index` and `total_chunks` are converted with JavaScript `Number()`,
   not an integer parser: `""` is `0`, `" 2 "` is `2`, `"1e1"` is `10`,
   `"0x1"` is `1`; anything that is then not an integer is rejected.
3. Non-final chunks (`chunk_index < total_chunks - 1`) return
   `{ received: true, complete: false }`. Chunks may arrive in any order; only the
   one with the last index triggers assembly, so the client must send it last.

### Final chunk

Runs in this order. Every early return below also deletes the upload directory,
except the `500 CHUNK_ERROR` return when the directory can't be listed.

1. **Ownership checks:** portal fields vs partner fields, effective user,
   team / entity name, partner or managed partnership ownership (errors in API.md).
2. **Assembly:** directory entries are sorted by name and counted. Count ≠
   `total_chunks` → `409 CHUNK_MISSING`. Otherwise the files are concatenated
   and the directory is removed.
3. **Size:** assembled length > 110 MiB → `400 File exceeds 100 MB limit`
   (the message understates the real limit).
4. **Format:** magic-byte detection (`detectAudioFormat`), checked in this order:

   | Signature | Detected MIME |
   |-----------|---------------|
   | `ID3` at byte 0 | `audio/mpeg` |
   | `0xFF` then a byte with top 3 bits set | `audio/mpeg` |
   | `RIFF` at 0 and `WAVE` at 8 | `audio/wav` |
   | `fLaC` at 0 | `audio/flac` |
   | `ftyp` at 4 | `audio/mp4` |

   Fewer than 12 bytes, or no match → `400 UNSUPPORTED_FORMAT`. The client's
   `mime_type` field is never read.
5. **Portal placeholder partner** (`entity_type` = `team` | `other`): find a
   `partners` row for the effective user with `kind = entity_type`,
   `first_name = <team identifier | entity_name>`, `last_name = ''`; create it
   (`partner_role = 'follower'`) if absent. The song is attached to it.
6. **Song row inserted:** `display_name = routine_name || original_filename`,
   `partner_id = null` when a managed partnership is given, and
   `processed_filename`, `season_year`, `drive_file_id`, `drive_folder_id` all null.
   In the same transaction the bytes are staged in a `song_uploads` row with
   the upload's season and the uploader.
7. **Response sent:** `{ received: true, complete: true, song }`. The song's
   partner-name fields are null in this response regardless of the partner.
8. **Background build** starts (next section). The request does not wait for it.

### Background build

[`services/song_builds/`](../src/api_deejaytools/services/song_builds/).
A build claims its `song_uploads` row with a fresh `claim_id`, renews a
10-minute lease every minute while it runs, and is taken over by the
scheduler if it stops renewing; a build that lost its claim stops without
writing anything. The scheduler starts due builds each tick, at most 8 at
once per process.

1. Load the effective user. If the song has a managed partnership, load it
   (must belong to the user, else throw). Otherwise load the partner, if any.
2. **Season** = upload-time season year (above), as staged.
3. **Version** = 1 + the highest `_v<N>` suffix among the user's *other* songs
   with the same division, routine name, season year, partner and managed
   partnership (nulls compared as `''`), and the filenames other builds in
   flight have reserved for the slot. Soft-deleted songs count, so a deleted
   version's number is not handed out again. Formatted as at least two digits
   (`v01`, `v12`, `v100`). The filename is reserved in `song_uploads` under a
   per-slot advisory lock, so two uploads in flight for the same slot get
   distinct numbers, and a retried build keeps its name.
4. **Entity names** (`first last`, blanks dropped):

   | Song attached to | Leader | Follower |
   |------------------|--------|----------|
   | Managed partnership | partnership's leader | partnership's follower |
   | Nothing | uploader (falls back to user id) | — |
   | Partner with `kind` other than `partner` (team, other) | partner | — |
   | Partner with `partner_role = leader` | partner | uploader |
   | Partner with any other role | uploader | partner |

   **ProAm swap:** when the follower is not null and the division is exactly
   `ProAm FollowerAm` (trimmed), the follower is named first. Otherwise the
   leader is first. A follower whose name is blank is `""`, not null, so the swap
   still happens and the filename then starts with `_`.
5. **Processed filename:**

   ```
   <First>[_<Second>]_<Division>_<Season>[_<Routine>][_<Descriptor>]_v<NN>[.<ext>]
   ```

   - Names, season, routine and descriptor go through `sanitize_segment`: split on
     whitespace, remove every character outside ASCII `[A-Za-z0-9]` from each word
     (accented letters are removed, not transliterated: `José` → `Jos`), lowercase
     it, capitalise the first letter, join with no separator
     (`"mary-jo o'neil"` → `MaryjoOneil`).
   - Division goes through `static_segment`: characters outside `[A-Za-z0-9]`
     removed, case kept (`ProAm LeaderAm` → `ProAmLeaderAm`).
   - Empty segments are dropped. If the partnership segment is empty the
     sanitized user id is used, then the literal `user`.
   - Extension: from the **original filename** (text after the last dot, if the
     dot is neither first nor last), non-alphanumerics removed, lowercased. No
     extension in the original → none on the output, whatever the detected format.

   Example: `AliceTester_PatPartner_Classic_2027_BlueMonday_v01.mp3`.
6. **Tag** the bytes: title, artist, year — see [AUDIO-TAGGING.md](./AUDIO-TAGGING.md).
   Tagging never throws; on any problem the original bytes go through untagged.
7. **Upload** to `<root>/<season>/<division>/` as the processed filename, with the
   detected MIME type, as a resumable upload, tagged with the appProperty
   `deejaytools_song_id`. An empty division gives the `unknown` folder. The
   file's id is recorded in `song_uploads.drive_file_id` as soon as the upload
   returns, and a retry reuses it (or a file it finds by that appProperty), so
   a build never uploads twice.
8. **Share** with `reader` role, no notification email, to: the effective user's
   email, plus the partner's `email` when the song is attached to a partner (not
   for managed partnerships). Addresses are trimmed, lowercased, de-duplicated and
   must match `^[^\s@]+@[^\s@]+\.[^\s@]+$`. Per-address failures are logged
   (`song_drive_share_partial_failure`) and never fail the build.
9. **Update the song row:** `original_filename`, `processed_filename`,
   `season_year`, `drive_file_id`, `drive_folder_id` (the division folder),
   `updated_at`, and delete the `song_uploads` row, in one transaction. A
   song deleted by its owner meanwhile has the file deprecated instead.
10. **Re-queue event copies:** enqueue a `copy` job for every submission of this
    song whose `drive_copy_file_id` is still null. The song is usable before its
    file exists, so a submission made during steps 1–9 may already have had its
    copy job run and find nothing to copy (see `copy` step 3). If that submission's
    first job is still pending, it just gets a second one, which does nothing once
    the first has recorded the copy. Best-effort: a failure logs
    `drive_copy_requeue_failed`, goes to Sentry (`stage: "requeue"`), and does
    **not** count as a failed build.

**Failure:** a failure in steps 1–9 is handled by what references the song.
A song nothing references yet (no submission, no check-in) is hard-deleted,
with any file it already uploaded deprecated; the uploader sees it vanish and
uploads again. A song already submitted or checked in stays: its build is
retried with the drive-job backoff and marked `failed` after 10 attempts
(`song_build_exhausted`, reported to Sentry); [OPERATIONS.md](./OPERATIONS.md)
says how to put it back in the queue.

---

## Song deletion

`DELETE /v1/songs/:id`, after the active-check-in guard:

1. In one transaction: stamp `songs.deleted_at` (taking the song's own file id
   from the update's `RETURNING`, so a build finishing concurrently is seen),
   and delete the song's submissions, collecting their `drive_copy_file_id`s.
2. After commit, enqueue one `trash` job for the song's own file (when it has
   both `drive_file_id` and `drive_folder_id`) and one per collected copy id.

Nothing touches Drive before the commit, so a failed transaction leaves the
song and its file as they were.

`DELETE /v1/managed-partnerships/:id` soft-deletes the partnership's songs the
same way, queueing `trash` jobs for their own files and their event copies.

**`soft_delete(file_id)`** moves the file into `<root>/_deprecated/`: it
finds or creates that folder, reads the file's current parents, and updates the
file adding `_deprecated` and removing **all** current parents. Nothing is
permanently deleted or moved to the Drive trash.

---

## `drive_jobs` queue

Per-event copies of submitted songs, renames of those copies, and deprecation of
copies whose submission went away, the deprecation of a deleted song's own
file, the share of a song's file with a partner set after it was built, and the
share and unshare of an event's folder with its DJs and event managers
([ADR-0012](./decisions/ADR-0012-per-event-staff-roles.md), "The DJ's Drive
folder"). These run off the request path so a Drive outage or a slow copy never
fails or delays a submission during the pre-event rush.

Table: [`drive_jobs`](./SCHEMA.md#drive_jobs). Admin endpoints:
[`/v1/admin/drive-jobs`](./API.md#routersadmin_drive_jobspy--v1admindrive-jobs).

### Job kinds

| `kind` | Field set | Does |
|--------|-----------|------|
| `copy` | `submission_id` | Copies the song's Drive file into the event folder and records the copy's id on the submission |
| `rename` | `submission_id` | Re-applies the current naming rule to an existing copy |
| `trash` | `file_id` | Moves a file (an event copy, or a deleted song's own file) into `_deprecated` (`soft_delete`) |
| `share_event_folder` | `event_id`, `user_id` | Shares the event's folder, `<root>/<season>/Events/<event name>/` (created if missing), with the person's `users.email` as a reader, and records the folder's id in `file_id` |
| `unshare_event_folder` | `event_id`, `user_id` | Removes the person's reader permission from every folder a share for them on the event recorded |
| `share_song` | `file_id` | Gives the partner of the live song whose Drive file it is reader access to the file, by the partner's email. Reads the song's partner when it runs; nothing to do when there is none or it has no email |

### Where jobs are enqueued

| Event | Job(s) | When |
|-------|--------|------|
| `POST /v1/event-song-submissions` succeeds | `copy` for the new submission | After the insert |
| `DELETE /v1/event-song-submissions/:id` | `trash` for its `drive_copy_file_id`, if set | After the delete |
| Background build of an upload finishes | `copy` for each of the song's submissions still without a copy | After the song row gets its Drive fields ([step 10](#background-build)) |
| `DELETE /v1/songs/:id` | `trash` for the song's own file and per copy of each of its submissions | After the transaction commits |
| `DELETE /v1/managed-partnerships/:id` | `trash` for each of the partnership's live songs' own files and per copy of each of their submissions | After the transaction commits |
| `DELETE /v1/events/:id` | `trash` per copy of each of the event's submissions | After the transaction commits |
| `POST /v1/admin/drive-jobs/backfill-renames` | `rename` for every submission that has a copy | Immediately |
| `PUT /v1/events/:id/staff/:user_id/:role` | `share_event_folder`, when it gives the person their first `dj` or `event-manager` role on the event | In the grant's transaction |
| `DELETE /v1/events/:id/staff/:user_id/:role` | `unshare_event_folder`, when it revokes their last `dj` or `event-manager` role on the event | In the revoke's transaction |
| `PATCH /v1/events/:id` | `share_event_folder` for each of the event's DJs and event managers, when the change moves the event's folder (its name or season) | In the update's transaction |
| `DELETE /v1/events/:id` | `unshare_event_folder` for each of the event's DJs and event managers | In the delete's transaction |
| `PATCH /v1/songs/:id` | `share_song`, when it sets a new partner on a song that already has its Drive file | In the update's transaction |

In every case the file ids are captured **before** the rows that hold them are
deleted, and the job is enqueued **after** the delete commits, so a failed delete
never deprecates a copy that is still in use.

Enqueueing is outside the request's transaction and **best-effort**: an enqueue
failure is logged (`drive_copy_enqueue_failed` / `drive_trash_enqueue_failed`)
and reported to Sentry (tags `subsystem=drive_jobs`, `drive_job_kind`; context
`drive_job` with `stage: "enqueue"` and the ids), and the request still succeeds.
Nothing retries a lost enqueue. The folder jobs and `share_song` are the
exception: they are written in the transaction of the change that calls for
them, so they are lost only with it.

Nothing else enqueues work: editing a submission, renaming an event, or changing
a song's division does **not** move or rename existing copies. (A rename does
share the event's new folder with its DJs and event managers; the copies made
before it stay in the old folder, which they keep reading.) Run the rename
backfill after changing `resolve_submission_filename`.

A new job is inserted with `status = 'pending'`, `attempts = 0`,
`next_attempt_at = created_at = updated_at = now`, and a random UUID `id`.

### State machine

| From | To | Trigger | Other changes |
|------|----|---------|---------------|
| — | `pending` | Enqueue | `attempts = 0`, due now |
| `pending` | `running` | Claimed by a tick (`next_attempt_at <= now`) | `updated_at = now` (starts the lease) |
| `running` | `done` | Job succeeded | `last_error = null` |
| `running` | `pending` | Job threw, `attempts + 1 < 10` | `attempts += 1`, `last_error`, `next_attempt_at = now + backoff` |
| `running` | `failed` | Job threw, `attempts + 1 >= 10` | as above; reported to Sentry |
| `running` | `failed` | Job refused (retrying cannot help, e.g. the person has no email) | as above, after one attempt; reported to Sentry |
| `running` | `pending` | Lease expired: `updated_at` older than 10 min | `attempts` unchanged |
| `failed` | `pending` | `POST /v1/admin/drive-jobs/:id/retry` | `attempts = 0`, due now |

`done` and `failed` are terminal for the processor. Rows are never deleted.

### Processing (`process_drive_jobs`, once per tick)

The scheduler runs a tick at startup and then every `TICK_INTERVAL_MS` (default
30 s), skipping a tick while the previous one is still running; `DISABLE_SCHEDULER=1`
turns it off, and `GET /internal/tick` runs the same pass on demand. Each tick runs
session work first, then Drive work, in separate `try` blocks: a failure in one never
stops the other.

`process_drive_jobs` catches errors from reclaim, claim, each job's own work
and each status update. A database error in the **success** update is handled
as if the job had failed, so work that already happened in Drive is retried (a
copy reuses its tagged file). A database error in the **failure** update is
logged (`drive_job_failure_update_failed`) and leaves that job `running` until
the lease reclaims it; the rest of the batch goes on.

1. **Reclaim:** `UPDATE … SET status='pending', updated_at=now WHERE
   status='running' AND updated_at < now − 10 min`. `attempts` is **not**
   incremented (the job never got a real try). Logs `drive_jobs_reclaimed` (warn)
   when any rows move. A reclaim error is logged and the tick continues.
2. **Claim** up to **10** due jobs atomically:

   ```sql
   UPDATE drive_jobs SET status = 'running', updated_at = :now
   WHERE id IN (
     SELECT id FROM drive_jobs
     WHERE status = 'pending' AND next_attempt_at <= :now
     ORDER BY next_attempt_at
     LIMIT :limit
     FOR UPDATE SKIP LOCKED
   )
   RETURNING *
   ```

   `SKIP LOCKED` lets concurrent replicas take disjoint batches. A claim error
   is logged (`drive_jobs_claim_failed`), sent to Sentry and Discord, and ends
   the pass.
3. **Run each claimed job in sequence** (behaviour per kind below).
4. **Success:** `UPDATE … SET status='done', last_error=null, updated_at=now
   WHERE id=:id AND status='running'`. If no row matched, the lease was reclaimed
   while this run was in flight; it logs `drive_job_completion_superseded` (warn)
   and does not count as done.
5. **Failure:** `attempts := attempts + 1`, `last_error := <error message>`,
   `next_attempt_at := now + backoff(attempts)`, `status := 'failed'` if
   `attempts >= 10` else `'pending'`, with the same `status='running'` guard.
   Logged at **error** on the first failure and on exhaustion, **warn** otherwise
   (`drive_job_retrying` / `drive_job_exhausted`, context includes
   `error_message` and `next_attempt_at`). Only exhaustion goes to Sentry (tags
   `subsystem=drive_jobs`, `drive_job_kind`; context `drive_job`).
6. Logs `drive_jobs_processed` with claimed / succeeded / failed counts when the
   batch was non-empty. Superseded completions count as failed there. A
   superseded run that fails still logs (`superseded=true`) but reports nothing
   to Sentry: the run that owns the job does.

**Backoff:** `min(60 s × 2^attempts, 30 min)`, where `attempts` is the count
*after* the failure just recorded:

| After attempt | 1 | 2 | 3 | 4 | 5–9 |
|---------------|---|---|---|---|-----|
| Next try in | 2 min | 4 min | 8 min | 16 min | 30 min |

Ten attempts span roughly three hours, sized to outlast a Google-side incident
rather than a blip.

**Batch vs lease:** a batch of 10 should finish inside the 10-minute lease, or
jobs still running get reclaimed and may run twice. Every kind tolerates that:
a copy is tagged with its submission and reused (below), and rename, trash and
the share kinds are idempotent.

### Per-kind behaviour

Every kind throws (→ retry) when a required field is missing. Drive errors
propagate as job failures.

**`copy`**

1. Load the submission joined to its song and event. Not found (submission
   deleted since) → success, nothing done.
2. Submission already has `drive_copy_file_id` → success, nothing done.
3. Song has no `drive_file_id` → logs `drive_copy_skipped_no_source` (warn) and
   succeeds. For an upload whose background build hasn't finished, step 10 of the
   build queues the copy again once the file exists.
4. Filename = `resolve_submission_filename`: the song's `processed_filename`, else
   `original_filename`, else the song id (each trimmed). Events whose name
   normalises to start with `theopen` have their own branch, which today returns
   the same value.
5. Destination: `<root>/<event season>/Events/<event name>/<division>/[Finals|Prelims/]`,
   where division = submission's `division` if set, else the song's, trimmed,
   else `unknown`; subfolder `Finals` for `finals_only`, `Prelims` for
   `prelims_only`, none otherwise (including null).
6. Copy the source into the destination with that name, tagged with the
   submission's id; a copy already there with that tag (from an overlapping run,
   or a run that failed after copying) is reused instead. On any error in
   folder resolution or the copy, the folder cache is cleared before rethrowing.
7. Set `event_song_submissions.drive_copy_file_id` to the copy's id, only while
   it is still null. When that matches nothing (the submission was deleted
   meanwhile, or another run recorded a different copy) the copy is
   soft-deleted, unless it is the very copy already recorded.

**`rename`**

1. Load as for copy. Submission gone, or no copy yet → success, nothing done (a
   pending copy job applies the current rule itself).
2. Compute the filename as for copy, read the copy's current name, and
   `files.update` it only if different. Logs `drive_rename_succeeded` or
   `drive_rename_noop`. Safe to enqueue any number of times.

**`trash`**

`soft_delete(file_id)` (see [Song deletion](#song-deletion)).

**`share_event_folder`**

1. The person no longer holds `dj` or `event-manager` on the event, or the event
   is gone → success, nothing done (an unshare is, or was, queued).
2. Their `users.email` is empty → the job is **refused**: `failed` at once, with
   `last_error` naming the user id. The same for an address Drive cannot share
   with. The admin retry runs it again once the address is fixed.
3. Resolve the event's folder, `<root>/<season>/Events/<event name>/`, as a copy
   does (season = the event's `season_year`, else from its `start_date`),
   creating it if missing; on an error the folder cache is cleared.
4. Grant `reader` to the address with no notification email, and record the
   folder's id in the job's `file_id`.
5. If the role was revoked while this ran, queue another `unshare_event_folder`,
   which finds the folder just recorded.

**`unshare_event_folder`**

1. The person holds `dj` or `event-manager` on the event again → success,
   nothing done.
2. The folders: every `file_id` a `share_event_folder` job for this person and
   event recorded (so a folder the event has since moved from is included),
   less any folder a share for them on another event they are still DJ or event
   manager of recorded. None → success, nothing done.
3. An empty `users.email` is refused, as for a share.
4. Remove each `reader` permission the address holds directly on each folder.
   Other people and other roles are left alone. Repeating it is safe.

The address is the person's `users.email` when the job runs, so an address
changed between the share and the unshare leaves the old one's access in place.

### Operating it

- `GET /v1/admin/drive-jobs/summary` gives counts by status plus how many
  submissions still lack a copy. A non-zero `failed`, or a rising
  `submissions_without_copy`, means look at the rows.
- `GET /v1/admin/drive-jobs?status=failed` lists them with `last_error`.
- `POST /v1/admin/drive-jobs/:id/retry` returns a `failed` job to `pending` with
  `attempts = 0`, due now. It is the only way out of `failed` short of SQL.
- `POST /v1/admin/drive-jobs/backfill-renames` after any naming-rule change.

---

## Event folder sweep

Deleting an event queues `trash` jobs for its copies, which move them to
`_deprecated`, but the event's folder and its division and round folders stay.
Events that come and go (a contract suite creates and deletes one per run)
would pile up empty folders under `Events/`. The sweep clears them.

**Rule.** A folder `<root>/<season>/Events/<name>/` is trashed, with every
folder below it, when both hold:

1. No `events` row maps to it: no event whose season (its `season_year`, else
   from its `start_date`) and name, as folder names, are the folder's. A
   renamed event's old folder maps to nothing either.
2. No untrashed file is anywhere below it, whoever put it there. Folders do
   not count as files. Copies moved to `_deprecated` or in the Drive trash are
   no longer in it.

Only folders under `Events/` are considered: the upload folders
`<root>/<season>/<division>/` are shared by every song and are never touched,
nor are the season, `Events` and `_deprecated` folders themselves. Folders go
to the **Drive trash** (`trashed = true`, recoverable), deepest first, then the
event folder; nothing is deleted permanently. Each trashed folder is dropped
from the folder cache, so a later copy or share for an event of the same name
creates a fresh folder.

The service account sees only files it created (`drive.file` scope), so a file
another account put in the folder is not seen by the check.

**When.** Not a job: a deleted event leaves no row to hang one on, and the
sweep has to wait for the copies' `trash` jobs anyway. The scheduler runs it on
the first tick and then on the first tick an hour or more after the last sweep
began (`EVENT_FOLDER_SWEEP_INTERVAL_SECONDS`), after the Drive jobs of that tick,
and only when Drive is configured. `GET /internal/tick` runs the same pass, so
the same hourly limit applies. It never runs on a request path. One sweep
trashes at most `EVENT_FOLDER_SWEEP_LIMIT` folders (3): each takes a few
seconds of Drive calls, and the sweep runs inside the tick, so a large backlog
in one go would hold up Drive jobs and queue fills. A sweep that stops at the
limit makes the next one due after `EVENT_FOLDER_SWEEP_BACKLOG_SECONDS` (60)
rather than the hour, so a backlog clears a few folders a minute. A folder whose
copies are still waiting on their `trash` jobs is left for a later sweep.

**Order.** Drive is listed first, then the events are read, so a folder made
for an event created meanwhile is matched to it. Each candidate is checked for
files just before it is trashed.

**Reporting.** Each trashed folder logs `drive_event_folder_trashed` with its id,
path and number of subfolders; every sweep logs `drive_event_folders_swept` with
how many event folders it saw and how many it trashed, and returns what it
trashed. A Drive error ends the sweep, logs `event_folder_sweep_failed`, goes to
Sentry (tag `subsystem=drive_folders`) and is posted to Discord once per run of
failures; the next sweep starts over. Several replicas sweeping at once is
harmless: trashing a folder twice changes nothing.
