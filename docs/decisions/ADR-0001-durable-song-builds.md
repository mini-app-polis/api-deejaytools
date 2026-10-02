# 0001. Make song builds durable

Date: 2026-10-02

## Status

Accepted

## Context

An uploaded song is built in the background after the upload answers: it
is named, tagged, uploaded to Google Drive and recorded on the song.
deejaytools-api runs that build in a fire-and-forget promise holding the
bytes in memory. DRIVE.md lists what that loses:

- a restart mid-build leaves the song with no file, and nothing resumes it;
- a failed build of a song already submitted or checked in leaves it
  without a file for good, because the song cannot be deleted;
- a failure after the Drive upload orphans the file;
- two uploads in flight for the same slot can get the same version number.

The brief for this service is to fix DRIVE.md's known defects rather than
reproduce them, without the web app noticing (ADR-009), and with additive
schema changes only while deejaytools-api may serve the database again
(ADR-008).

## Decision

Stage each upload in a new table, `song_uploads` (migration 003), in the
same transaction as the song row, and run the build from it:

- The bytes stay in the row until the build finishes; the row is deleted
  then, and with the song (`ON DELETE CASCADE`).
- A build claims its row with a lease it renews every minute and a
  `claim_id`. The scheduler resumes builds that are due or whose lease
  lapsed; a build that was taken over can no longer write.
- The Drive file id is recorded as soon as the upload returns, and a file
  found by its `deejaytools_song_id` appProperty is reused, so a retry
  never uploads twice.
- The filename is reserved under a per-slot advisory lock that counts
  other builds' reserved names as well as finished songs.
- A failure retries with backoff; after 10 attempts the row is `failed`
  and reported to Sentry. A song nothing references is still deleted when
  its build fails, as in deejaytools-api, which the web app relies on.

## Consequences

**Easier:**
- A restart, a Drive outage or a crash after the upload no longer loses a
  song's file.
- Failed builds are visible and retryable with one UPDATE
  (docs/OPERATIONS.md).

**Harder:**
- Upload bytes (up to 110 MB) sit in Postgres until the build finishes.
- deejaytools-api never reads `song_uploads`, so switching traffic back
  needs the table drained first (docs/OPERATIONS.md).
