# Operations

Procedures for running this service on deejaytools-api's database.

## Switching traffic back to deejaytools-api

The schema stays additive (ADR-008), so deejaytools-api can serve the same
database again at any time. One thing does not carry over: song builds in
flight. This service keeps an uploaded song's bytes in `song_uploads` until
its Drive build finishes; deejaytools-api never reads that table, so a song
still building when traffic moves back stays without a file.

Before switching back:

1. Check what is still building:

   ```sql
   SELECT status, count(*) FROM song_uploads GROUP BY status;
   ```

2. `pending` and `running` rows finish on their own within a few scheduler
   ticks (a `pending` row may be waiting out a retry backoff: see
   `next_attempt_at`, epoch milliseconds). Wait until none are left, or
   force a pass with `GET /internal/tick`.
3. `failed` rows have used all their attempts. Retry them (below), or
   accept that those songs have no file. Their uploaders see a song with no
   file and can upload it again.
4. Switch traffic. The table can stay; deejaytools-api ignores it, and its
   song delete still works (`ON DELETE CASCADE`).

Other state needs nothing: Drive jobs live in deejaytools-api's own
`drive_jobs` table, and identity grants are re-derived from `users.role` by
the backfill on this service's next deploy.

## Retrying failed song builds

A build is retried with backoff and marked `failed` after 10 attempts,
reported to Sentry as `song_build_exhausted`, with the last error in
`song_uploads.last_error`. Once the cause is fixed (Drive credentials, a
quota), put the rows back in the queue; the next tick picks them up:

```sql
-- One song
UPDATE song_uploads
SET status = 'pending', attempts = 0, next_attempt_at = 0,
    claim_id = NULL, last_error = NULL
WHERE song_id = '<song id>' AND status = 'failed';

-- Every failed build
UPDATE song_uploads
SET status = 'pending', attempts = 0, next_attempt_at = 0,
    claim_id = NULL, last_error = NULL
WHERE status = 'failed';
```

A retried build reuses a file it already uploaded (it finds it by its
`deejaytools_song_id` appProperty), so a retry never uploads twice.

## Configuration not carried over

`CORS_ORIGINS`, deejaytools-api's legacy fallback for
`DEEJAYTOOLS_CORS_ORIGINS`, is deliberately not read. Set
`DEEJAYTOOLS_CORS_ORIGINS` in the shared config before the switch; without it
the service allows only `http://localhost:5173`, so the web app's
requests fail in the browser.

## Discord notifications

The service posts to the fleet's shared Discord channels through
common-python-utils' `mini_app_polis.activity` and `mini_app_polis.discord`
(`src/api_deejaytools/services/notifications.py`). Every message's footer
says `api-deejaytools` and the environment; outside production every
title also starts with `[DEVELOPMENT]` (or `[LOCAL]`) — `[DEVELOPMENT] song
added`, `[DEVELOPMENT] fault · 500` — so dev's faults in the shared `errors`
channel are not read as production's. Messages name people only where that
is the point (the dancers in "song added"); the caller of a request is
named by Clerk user id, never by email.

### Configuration

| Variable | Purpose |
|----------|---------|
| `DISCORD_WEBHOOK_URL` | Fallback webhook for every channel. |
| `DISCORD_WEBHOOK_URL_ERRORS` | Optional: the `errors` channel's own webhook. |
| `DISCORD_WEBHOOK_URL_ACTIVITY` | Optional: the `activity` channel's own webhook. |
| `NOTIFY_DATA_CHANGES` | Default `true`. `false` mutes the "data changed" feed without a deploy; faults and "song added" still post. |

A channel without its own variable uses `DISCORD_WEBHOOK_URL`. With neither
set, that channel is off: nothing is sent, nothing fails, and the log says
`discord notifications off: no webhook for channel=…` once per channel. A
webhook pasted with GitHub's `/github` suffix works too. A Discord rate limit
holds every post until it lifts (logged, and reported to Sentry once).

### What posts where

**`activity`**

- **Song added**, one per song, when its Drive build succeeds (the commit
  that records the file), never for a build that fails or rolls back:

  > **song added**
  > Jane Doe added 'Ballad' (Classic, with John Smith)
  > [Drive file](https://drive.google.com/file/d/…/view)

  Uploaded for someone else ("Upload For"):
  `Org Anizer uploaded 'Ballad' for Jane Doe (Classic)`. A managed
  partnership reads `(ProAm, managed partnership Lee Lead & Fay Follow)`, a
  team upload `(Team, team Jt Swing)`, an "other" upload `(…, as Formation)`.
  Names are `First Last`, else the display name, else the user id; the
  routine is the routine name, else the song's display name (the filename).
- **Feedback**, one per piece of site feedback accepted (`POST /v1/feedback`):
  its type and subject, whether it had a screenshot, and whether it was
  emailed. Never the message, the sender's name or their email — those go
  by email only. Without a Brevo key the post says the message was not
  kept: it is the only sign the feedback arrived. A Brevo refusal is a
  `fault · 502` in `errors` instead, and no feedback post.

  > **feedback · bug**
  > Queue froze
  > emailed to the maintainer

- **Data changed**, one per request that committed anything worth saying:
  the tables it changed with `+` created, `~` updated, `-` deleted rows and
  `*` statements (row count unknown), and in the footer the method, path and
  caller's Clerk user id (their `users.id`; look it up behind auth to find
  the person — their email does not go in a shared channel):

  > **data changed**
  > `teams` +1
  > POST /v1/teams · user_2abc… · api-deejaytools · production

**`errors`**

- **Every 5xx and unhandled exception** a request ends with (`fault · 500`,
  `fault · 503` for a deadline), naming the method and path, the caller,
  and the cause: the exception's type and Sentry event id
  (`RuntimeError · sentry 3f2a…`), an `ApiError` code
  (`ApiError CHUNK_ERROR`), or `deadline exceeded after 30000ms`. Never the
  exception's text, which for a database error is the statement. The web
  app still gets the same 500 envelope. No 4xx is posted: the only caller
  is the web app, and a person meeting a guard is not a fault.
- **Background faults** (`fault · background`):
  - `song build <song id> · failed, song removed`: a build of a song
    nothing referenced failed, so the song was deleted; its owner sees it
    vanish and must upload again.
  - `song build <song id> · gave up after 10 attempts`: a submitted or
    checked-in song's build is exhausted (see "Retrying failed song
    builds"). Its earlier failed attempts are not posted.
  - `drive job <job id> (<kind>) · gave up after 10 attempts`: a Drive job
    is `failed` for good (retry it with
    `POST /v1/admin/drive-jobs/{id}/retry`). Retries are not posted.
  - `scheduler · sessions`, `scheduler · song builds`,
    `scheduler · drive jobs`, `drive jobs · claim failed`: a scheduler step
    failed. Posted once per run of failures: the next is posted only after
    the step has succeeded again.

  Each names the exception's type and its Sentry event id (every one of
  these is reported to Sentry, with a `subsystem` tag); nothing is
  reported to Sentry twice for Discord's sake.

### What is left out, and why

| Left out | Why |
|----------|-----|
| `identity_audit_events` (suppressed table) | One row per authorization decision on every scoped request, the polled GETs included. It would turn the feed into the access log. |
| `song_uploads` (suppressed table) | Staged upload bytes, written with the song and deleted when its build finishes. The song is announced on its own. |
| `drive_jobs` (suppressed table, except under `/v1/admin/drive-jobs`) | Queue rows enqueued as a side effect of a submission or a delete, already reported under their own table, and churned by the scheduler. An admin's retry or rename backfill is reported. |
| Changes made by `POST /v1/auth/sync` | Every sign-in updates the users row and re-ensures the principal. Faults are still posted. |
| Changes made by `POST /v1/songs/upload/chunk` | The song (and a team or "other" upload's placeholder partner) is announced by its build instead, and only if the build succeeds. Faults are still posted. |
| Changes made by `GET /internal/tick` | The scheduler's own work, which the background loop does every 30 s unreported. Faults are posted by the scheduler. |
| Changes made under `/v1/checkins` and `/v1/queue` | The live floor: every check-in and withdrawal, and every manager queue action (promote, complete, incomplete, move down, withdraw). Dozens a minute at an event, each routine; they would bury everything else in the shared channel. Session status changes are still reported. Faults are still posted. |
| Changes made by `POST /v1/admin/checkins` and `DELETE /v1/admin/checkins/test` | The admin's synthetic test check-ins (a stub leader, partner, pair and check-in each) and their removal: test data, which would read in the shared feed as dancers arriving. Faults are still posted. |
| `/health`, `/version` | Polled by monitors; never posted, faults included. |

The legacy `POST /v1/songs` (a song record with no file and no build) gets
no "song added": it appears in the change feed as an ordinary change. The
conformance and e2e suites upload real songs to dev, so dev's `activity`
channel shows their songs, labelled `[DEVELOPMENT]`.

### What it cannot see

- Writes made with `session.execute(text(...))` or outside an ORM session.
  The request routes write through the ORM, so they are all seen; the raw
  SQL in `services/drive_jobs.py` runs in the background, which the change
  feed does not cover anyway.
- Anything background work changes (scheduler, song builds, Drive jobs):
  there is no request to tally it. What matters there is announced (song
  added) or reported (faults).
- Writes a request makes after its deadline answered `503`: the handler
  runs on, but the feed has already been posted.
- An exception after a response has started streaming (Sentry still has
  it), and a 5xx answered by the server before the app (a crash).
- Anything when Discord is down or rate-limiting: delivery is
  fire-and-forget, never retried, logged and reported to Sentry when it
  fails. The channel is not a record; the database and Sentry are.
