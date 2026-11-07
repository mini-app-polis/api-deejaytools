-- Migration 010: scratching a registration entry
--
-- An admin scratches an entry on an event's registration list when the
-- couple signed up but will no longer compete. The scratch is a mark on the
-- entry (event_registrations.scratched). Their songs stay submitted to the
-- event (its Drive folder, check-in), but the ones the admin's page matched
-- to the entry are hidden from the event's public entry list
-- (GET /v1/events/:id/entities): event_song_submissions.hidden_by_registration_id
-- names the scratched entry that hides each.
--
-- Unscratching clears both. Deleting the entry (a re-import without it)
-- unhides its submissions through ON DELETE SET NULL.
--
-- Additive only: two new columns, NULL / false on every existing row.
--
-- Idempotent: safe on a database where the columns are already there.

ALTER TABLE event_registrations
  ADD COLUMN IF NOT EXISTS scratched BOOLEAN NOT NULL DEFAULT false;

ALTER TABLE event_song_submissions
  ADD COLUMN IF NOT EXISTS hidden_by_registration_id TEXT
    REFERENCES event_registrations(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_event_song_submissions_hidden_by
  ON event_song_submissions (hidden_by_registration_id)
  WHERE hidden_by_registration_id IS NOT NULL;
