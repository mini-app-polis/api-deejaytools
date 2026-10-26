> Ported from [deejaytools-api](https://github.com/legacy-graveyard/deejaytools-api)'s docs/DRIVE.md when that repository was archived. "This service" and `src/` paths below mean deejaytools-api, whose contract api-deejaytools reproduces.

# Google Drive integration

Every song file this API accepts ends up in Google Drive. Two mechanisms
put it there, with different failure models:

| Path | Trigger | Runs | On failure |
|------|---------|------|------------|
| **Upload pipeline** | Final chunk of `POST /v1/songs/upload/chunk` | Fire-and-forget promise in the request process, after the response is sent | Song row is hard-deleted; the user retries the upload |
| **`drive_jobs` queue** | Event submission created, removed, or cascaded away; admin backfill | Scheduler tick (`runTick()` → `processDriveJobs()`) | Rescheduled with backoff; `failed` after 10 attempts; admin retry endpoint |

Song deletion also calls Drive directly (synchronously, best-effort) for the
song's own file; see [Song deletion](#song-deletion).

Source: [`src/services/drive.ts`](https://github.com/legacy-graveyard/deejaytools-api/tree/main/src/services/drive.ts),
[`src/services/driveJobs.ts`](https://github.com/legacy-graveyard/deejaytools-api/tree/main/src/services/driveJobs.ts),
[`src/routes/songs.ts`](https://github.com/legacy-graveyard/deejaytools-api/tree/main/src/routes/songs.ts) (`buildAndUploadSong`, the chunk handler),
[`src/lib/submissionFilename.ts`](https://github.com/legacy-graveyard/deejaytools-api/tree/main/src/lib/submissionFilename.ts),
[`src/lib/seasonYear.ts`](https://github.com/legacy-graveyard/deejaytools-api/tree/main/src/lib/seasonYear.ts).
Tag contents written into each file are specified in [AUDIO-TAGGING.md](./AUDIO-TAGGING.md).

---

## Configuration

| Env var | Purpose |
|---------|---------|
| `GOOGLE_SERVICE_ACCOUNT_EMAIL` | Service account the API authenticates as (JWT). |
| `GOOGLE_SERVICE_ACCOUNT_PRIVATE_KEY` | Its PEM key. Literal `\n` sequences are converted to newlines before use. |
| `GOOGLE_DRIVE_PARENT_FOLDER_ID` | Root folder everything below lives in. |

All three are required by every Drive call; if any is missing the call throws
`Google Drive environment variables are not configured`. Nothing checks this at
boot, so an unconfigured deployment fails per upload (song row deleted) and per
job (retried, then `failed`).

**Scope:** `https://www.googleapis.com/auth/drive.file`. The service account can
only see and modify files **it created**. Copy, rename and trash therefore only
work on files that came through `uploadSongToDrive` (or copies of them).

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
through `sanitizeFolderName`: `/` and `\` become `-`, runs of whitespace collapse
to one space, the result is trimmed, and an empty result becomes `unknown`.
`Events` and `_deprecated` are literal.

**Find-or-create:** a folder is looked up by exact name, parent, folder MIME type
and `trashed=false` (single quotes and backslashes in the name are escaped for
the Drive query); the first match wins, otherwise it is created. Resolved ids are
cached process-locally for **1 hour** keyed by `(parentId, name)`. The whole
cache is dropped whenever an event copy fails, so a folder deleted out from
under the cache recovers on the next retry. Nothing else clears it: an upload or a
`trash` job that hits a stale cached folder keeps failing until the hour is up.

### Season year

Seasons roll over on **October 1**: October–December belong to the next calendar
year (2026-09-30 → `"2026"`, 2026-10-01 → `"2027"`).

| Used for | Derived from |
|----------|--------------|
| Uploaded original (folder, filename, `songs.season_year`, year tag) | Upload time, in the **server's local timezone** (`seasonYearFromTimestamp(Date.now())`) |
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
7. **Response sent:** `{ received: true, complete: true, song }`. The song's
   partner-name fields are null in this response regardless of the partner.
8. **Background build** starts (next section). The request does not wait for it.

### Background build (`buildAndUploadSong`)

1. Load the effective user. If the song has a managed partnership, load it
   (must belong to the user, else throw). Otherwise load the partner, if any.
2. **Season** = upload-time season year (above).
3. **Version** = 1 + the highest `_v<N>` suffix among the user's *other* songs
   with the same division, routine name, season year, partner and managed
   partnership (nulls compared as `''`). Soft-deleted songs count, so a deleted
   version's number is not handed out again. Formatted as at least two digits
   (`v01`, `v12`, `v100`). Not collision-proof: songs whose build hasn't finished
   have no `processed_filename` yet and are not counted, so two uploads in flight
   for the same slot get the same number.
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

   - Names, season, routine and descriptor go through `sanitizeSegment`: split on
     whitespace, remove every character outside ASCII `[A-Za-z0-9]` from each word
     (accented letters are removed, not transliterated: `José` → `Jos`), lowercase
     it, capitalise the first letter, join with no separator
     (`"mary-jo o'neil"` → `MaryjoOneil`).
   - Division goes through `staticSegment`: characters outside `[A-Za-z0-9]`
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
   detected MIME type, as a resumable upload. An empty division gives the
   `unknown` folder.
8. **Share** with `reader` role, no notification email, to: the effective user's
   email, plus the partner's `email` when the song is attached to a partner (not
   for managed partnerships). Addresses are trimmed, lowercased, de-duplicated and
   must match `^[^\s@]+@[^\s@]+\.[^\s@]+$`. Per-address failures are logged
   (`song_drive_share_partial_failure`) and never fail the build.
9. **Update the song row:** `original_filename`, `processed_filename`,
   `season_year`, `drive_file_id`, `drive_folder_id` (the division folder),
   `updated_at`.
10. **Re-queue event copies:** enqueue a `copy` job for every submission of this
    song whose `drive_copy_file_id` is still null. The song is usable before its
    file exists, so a submission made during steps 1–9 may already have had its
    copy job run and find nothing to copy (see `copy` step 3). If that submission's
    first job is still pending, it just gets a second one, which does nothing once
    the first has recorded the copy. Best-effort: a failure logs
    `drive_copy_requeue_failed`, goes to Sentry (`stage: "requeue"`), and does
    **not** count as a failed build.

**Failure:** any throw from steps 1–9 logs `song_background_upload_failed` and
hard-deletes the song row. If that delete fails it logs
`song_cleanup_delete_failed` and the row stays. It fails whenever the song was
already submitted to an event or checked in (both foreign keys block it), which is
possible because the song is listed and usable as soon as the response is sent.
See also [Known defects](#known-defects).

---

## Song deletion

`DELETE /v1/songs/:id`, after the active-check-in guard:

1. If the song has both `drive_file_id` and `drive_folder_id`, call
   `softDeleteOnDrive` **inline, before the database transaction**. Failure is
   logged (`song_drive_soft_delete_failed`) and ignored; the delete continues. If
   the transaction then fails (500), the song stays live with its file already in
   `_deprecated`.
2. In one transaction: collect the song's submissions' `drive_copy_file_id`s,
   delete those submissions, stamp `songs.deleted_at`.
3. After commit, enqueue one `trash` job per collected copy id.

`DELETE /v1/managed-partnerships/:id` soft-deletes the partnership's songs the
same way but does **not** deprecate their own Drive files, only their event
copies.

**`softDeleteOnDrive(fileId)`** moves the file into `<root>/_deprecated/`: it
finds or creates that folder, reads the file's current parents, and updates the
file adding `_deprecated` and removing **all** current parents. Nothing is
permanently deleted or moved to the Drive trash.

---

## `drive_jobs` queue

Per-event copies of submitted songs, renames of those copies, and deprecation of
copies whose submission went away. These run off the request path so a Drive
outage or a slow copy never fails or delays a submission during the pre-event rush.

Table: [`drive_jobs`](./SCHEMA.md#drive_jobs). Admin endpoints:
[`/v1/admin/drive-jobs`](./API.md#admin-drive-jobsts--v1admindrive-jobs).

### Job kinds

| `kind` | Field set | Does |
|--------|-----------|------|
| `copy` | `submission_id` | Copies the song's Drive file into the event folder and records the copy's id on the submission |
| `rename` | `submission_id` | Re-applies the current naming rule to an existing copy |
| `trash` | `file_id` | Moves a copy into `_deprecated` (`softDeleteOnDrive`) |

### Where jobs are enqueued

| Event | Job(s) | When |
|-------|--------|------|
| `POST /v1/event-song-submissions` succeeds | `copy` for the new submission | After the insert |
| `DELETE /v1/event-song-submissions/:id` | `trash` for its `drive_copy_file_id`, if set | After the delete |
| Background build of an upload finishes | `copy` for each of the song's submissions still without a copy | After the song row gets its Drive fields ([step 10](#background-build-buildanduploadsong)) |
| `DELETE /v1/songs/:id` | `trash` per copy of each of the song's submissions | After the transaction commits |
| `DELETE /v1/managed-partnerships/:id` | `trash` per copy of each submission of the partnership's live songs | After the transaction commits |
| `DELETE /v1/events/:id` | `trash` per copy of each of the event's submissions | After the transaction commits |
| `POST /v1/admin/drive-jobs/backfill-renames` | `rename` for every submission that has a copy | Immediately |

In every case the file ids are captured **before** the rows that hold them are
deleted, and the job is enqueued **after** the delete commits, so a failed delete
never deprecates a copy that is still in use.

Enqueueing is outside the request's transaction and **best-effort**: an enqueue
failure is logged (`drive_copy_enqueue_failed` / `drive_trash_enqueue_failed`)
and reported to Sentry (tags `subsystem=drive_jobs`, `drive_job_kind`; context
`drive_job` with `stage: "enqueue"` and the ids), and the request still succeeds.
Nothing retries a lost enqueue.

Nothing else enqueues work: editing a submission, renaming an event, or changing
a song's division does **not** move or rename existing copies. Run the rename
backfill after changing `resolveSubmissionFilename`.

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
| `running` | `pending` | Lease expired: `updated_at` older than 10 min | `attempts` unchanged |
| `failed` | `pending` | `POST /v1/admin/drive-jobs/:id/retry` | `attempts = 0`, due now |

`done` and `failed` are terminal for the processor. Rows are never deleted.

### Processing (`processDriveJobs`, once per tick)

The scheduler runs a tick at startup and then every `TICK_INTERVAL_MS` (default
30 s), skipping a tick while the previous one is still running; `DISABLE_SCHEDULER=1`
turns it off, and `GET /internal/tick` runs the same pass on demand. Each tick runs
session work first, then Drive work, in separate `try` blocks: a failure in one never
stops the other.

`processDriveJobs` catches errors from reclaim, claim and each job's own work.
It does not guard the status updates in steps 4 and 5. A database error in the
**success** update is caught as if the job had failed, so work that already
happened in Drive is retried. A database error in the **failure** update escapes:
the rest of the batch is abandoned in `running` until the lease expires, and the
tick logs `drive_jobs_tick_failed`.

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
   is logged (`drive_jobs_claim_failed`), sent to Sentry, and ends the pass.
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
   superseded run that fails still logs, and on its tenth attempt still reports to
   Sentry, although its guarded update changed nothing.

**Backoff:** `min(60 s × 2^attempts, 30 min)`, where `attempts` is the count
*after* the failure just recorded:

| After attempt | 1 | 2 | 3 | 4 | 5–9 |
|---------------|---|---|---|---|-----|
| Next try in | 2 min | 4 min | 8 min | 16 min | 30 min |

Ten attempts span roughly three hours, sized to outlast a Google-side incident
rather than a blip.

**Batch vs lease:** a batch of 10 must finish inside the 10-minute lease, or jobs
still running get reclaimed and may run twice. Rename and trash tolerate that.
Copy does not: its only guard is the `drive_copy_file_id` read at the start, so
two overlapping runs both copy and one copy is orphaned. Raising the batch size
without raising the lease is unsafe.

### Per-kind behaviour

All three throw (→ retry) when their required field is missing. Drive errors
propagate as job failures.

**`copy`**

1. Load the submission joined to its song and event. Not found (submission
   deleted since) → success, nothing done.
2. Submission already has `drive_copy_file_id` → success, nothing done. This
   makes a retry safe once a copy has been **recorded**; it does not protect
   against two runs at once, or a failure between the copy and step 7 (the retry
   copies again).
3. Song has no `drive_file_id` → logs `drive_copy_skipped_no_source` (warn) and
   succeeds. For an upload whose background build hasn't finished, step 10 of the
   build queues the copy again once the file exists.
4. Filename = `resolveSubmissionFilename`: the song's `processed_filename`, else
   `original_filename`, else the song id (each trimmed). Events whose name
   normalises to start with `theopen` have their own branch, which today returns
   the same value.
5. Destination: `<root>/<event season>/Events/<event name>/<division>/[Finals|Prelims/]`,
   where division = submission's `division` if set, else the song's, trimmed,
   else `unknown`; subfolder `Finals` for `finals_only`, `Prelims` for
   `prelims_only`, none otherwise (including null).
6. `files.copy` the source into the destination with that name. On any error in
   folder resolution or the copy, the folder cache is cleared before rethrowing.
7. Set `event_song_submissions.drive_copy_file_id` to the new id. The update has
   no `IS NULL` condition, and matches nothing if the submission was deleted
   meanwhile (the copy is then orphaned; the delete saw no copy id, so no trash
   job was queued).

**`rename`**

1. Load as for copy. Submission gone, or no copy yet → success, nothing done (a
   pending copy job applies the current rule itself).
2. Compute the filename as for copy, read the copy's current name, and
   `files.update` it only if different. Logs `drive_rename_succeeded` or
   `drive_rename_noop`. Safe to enqueue any number of times.

**`trash`**

`softDeleteOnDrive(file_id)` (see [Song deletion](#song-deletion)).

### Operating it

- `GET /v1/admin/drive-jobs/summary` gives counts by status plus how many
  submissions still lack a copy. A non-zero `failed`, or a rising
  `submissions_without_copy`, means look at the rows.
- `GET /v1/admin/drive-jobs?status=failed` lists them with `last_error`.
- `POST /v1/admin/drive-jobs/:id/retry` returns a `failed` job to `pending` with
  `attempts = 0`, due now. It is the only way out of `failed` short of SQL.
- `POST /v1/admin/drive-jobs/backfill-renames` after any naming-rule change.

---

## Known defects

Behaviour as built that a reimplementation should **not** copy. Each one leaves
Drive and the database out of step without an error anyone sees.

| Defect | Effect | Where |
|--------|--------|-------|
| Overlapping copy runs (lease reclaim) and copy-then-DB-failure both copy twice | An unreferenced duplicate in the event folder | `runCopyJob` |
| Copy finishing after its submission was deleted | An unreferenced copy in the event folder | `runCopyJob`, `DELETE /v1/event-song-submissions/:id` |
| Background build fails after the song was submitted or checked in | Song row stays with null Drive fields | `songs.ts` cleanup delete blocked by foreign keys |
| Background build fails after the Drive upload (in practice: the final DB update) | Unreferenced file in the division folder | `buildAndUploadSong` |
| Process restart during a background build | Song row stays with null Drive fields; nothing sweeps it | fire-and-forget promise |
| Two uploads in flight for the same slot | Same `vNN`, so two Drive files with the same name | version query ignores unfinished songs |
| Song delete transaction fails after the inline Drive move | Live song whose file is in `_deprecated` | `DELETE /v1/songs/:id` |
| Managed partnership delete | Its songs' original files are never deprecated | `DELETE /v1/managed-partnerships/:id` |
| DB error while recording a job failure | Rest of the batch held in `running` for the 10-minute lease | `processDriveJobs` |
