-- Migration 018: registration reminder dismissals
--
-- My Content tells a dancer when an event's registration list names them
-- but no music is in for that entry (GET /v1/registration-reminders). A
-- dancer who is not the person named says so ("Not me"): one row per
-- (entry, person), and their reminders leave that entry out from then on.
-- Nobody else's are affected.
--
-- A re-import keeps an unchanged entry's id (migration 009), so a dismissal
-- outlives it; an entry dropped from the list takes its rows with it.
--
-- Additive only: a new table. Idempotent: safe to run again.

CREATE TABLE IF NOT EXISTS registration_reminder_dismissals (
  registration_id TEXT NOT NULL REFERENCES event_registrations(id) ON DELETE CASCADE,
  user_id         TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  -- Epoch milliseconds, as everywhere in this schema.
  created_at      BIGINT NOT NULL,
  PRIMARY KEY (registration_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_registration_reminder_dismissals_user
  ON registration_reminder_dismissals (user_id);
