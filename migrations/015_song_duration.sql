-- Migration 015: each song's length
--
-- duration_ms: how long the song's file plays, read from the file itself
-- (services/song_lengths.py). Staff see it next to each submission, to check
-- a routine's music against its division's time limit. NULL until it has been
-- read, and for a file that cannot be read (or a song with no file).
--
-- duration_checked_at: when the scheduler last tried to read it, so a file
-- that cannot be read is tried again a day later rather than on every tick,
-- and never holds up the songs not tried yet.
--
-- Additive only. Idempotent: safe to run again.

ALTER TABLE songs ADD COLUMN IF NOT EXISTS duration_ms INTEGER;
ALTER TABLE songs ADD COLUMN IF NOT EXISTS duration_checked_at BIGINT;
