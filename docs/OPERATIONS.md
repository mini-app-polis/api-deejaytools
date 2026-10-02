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
