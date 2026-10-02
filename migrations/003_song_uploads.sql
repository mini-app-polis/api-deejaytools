-- Migration 003: durable staging for song builds
--
-- deejaytools-api builds an uploaded song (tag, Drive upload, record the
-- Drive fields) in a fire-and-forget promise holding the bytes in memory.
-- DRIVE.md "Known defects" lists what that loses: a process restart leaves
-- the song with no file and nothing sweeps it; a failed build of a song that
-- was already submitted or checked in leaves it without a file for good; a
-- failure after the Drive upload orphans the file; two uploads in flight for
-- the same slot get the same version number.
--
-- This table holds an upload's bytes and progress until its build finishes,
-- so a build can be retried (by the scheduler, after a restart or a failure)
-- and resumed without uploading twice. The row is deleted when the build
-- succeeds; deleting the song deletes it too.
--
-- Additive: a new table only. deejaytools-api never reads it, and its hard
-- delete of a song still works (ON DELETE CASCADE).

CREATE TABLE IF NOT EXISTS song_uploads (
  song_id             TEXT PRIMARY KEY REFERENCES songs(id) ON DELETE CASCADE,
  data                BYTEA NOT NULL,
  mime_type           TEXT NOT NULL,
  original_filename   TEXT NOT NULL,
  status              TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending', 'running', 'failed')),
  attempts            INTEGER NOT NULL DEFAULT 0,
  next_attempt_at     BIGINT NOT NULL,
  last_error          TEXT,
  -- The running build that owns the row. A build renews updated_at while it
  -- runs; one that stops renewing for the lease is taken over under a new
  -- claim_id, and the old one's later writes match nothing.
  claim_id            TEXT,
  -- The season is fixed when the upload is accepted. The filename is
  -- reserved under a per-slot advisory lock, so concurrent uploads for the
  -- same slot get distinct version numbers.
  season_year         TEXT,
  processed_filename  TEXT,
  -- Recorded as soon as the Drive upload returns, so a retry never uploads
  -- the same build twice.
  drive_file_id       TEXT,
  drive_folder_id     TEXT,
  created_at          BIGINT NOT NULL,
  updated_at          BIGINT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_song_uploads_due
  ON song_uploads (status, next_attempt_at);
