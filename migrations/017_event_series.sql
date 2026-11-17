-- Migration 017: event series
--
-- series names the run of events an event is one edition of (e.g. "The Open"),
-- set by an admin. An event's submission timeline
-- (GET /v1/events/{id}/submission-timeline) compares it with the earlier
-- events of its series. NULL: in no series, so compared with nothing.
--
-- Stored trimmed; matched case-insensitively.
--
-- Additive only: a new nullable column, NULL on every existing row.
--
-- Idempotent: safe on a database where it is already there.

ALTER TABLE events ADD COLUMN IF NOT EXISTS series TEXT;

CREATE INDEX IF NOT EXISTS idx_events_series
  ON events (lower(series))
  WHERE series IS NOT NULL;
