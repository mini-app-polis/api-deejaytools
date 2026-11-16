-- Migration 013: keep site feedback, and share rate-limit windows
--
-- site_feedback: POST /v1/feedback used to put the message only in the
-- Brevo email, so when Brevo refused it the feedback was gone. Each
-- submission is now saved here first; emailing it is a delivery step the
-- scheduler retries (services/feedback_delivery.py). user_id is the
-- verified Clerk subject when the sender was signed in, never a foreign key:
-- a sender need not have synced.
--
-- rate_limit_windows: the per-caller rate-limit windows, shared by every
-- worker and replica (ADR-0014). Each uvicorn worker used to keep its own
-- windows in memory, so with four workers every limit was four times what
-- it said. UNLOGGED: the windows last a minute and need not survive a
-- crash.
--
-- Additive only. Idempotent: safe to run again.

CREATE TABLE IF NOT EXISTS site_feedback (
  id               TEXT PRIMARY KEY,
  type             TEXT NOT NULL CHECK (type IN ('bug', 'feature', 'general')),
  subject          TEXT NOT NULL,
  message          TEXT NOT NULL,
  contact_name     TEXT,
  contact_email    TEXT,
  -- The PNG or JPEG data URL sent with it, attached to the email.
  screenshot       TEXT,
  user_id          TEXT,
  -- pending: not emailed yet (and waits while email is not configured);
  -- emailed: delivered; failed: gave up after the retries.
  status           TEXT NOT NULL DEFAULT 'pending'
                     CHECK (status IN ('pending', 'emailed', 'failed')),
  attempts         INTEGER NOT NULL DEFAULT 0,
  next_attempt_at  BIGINT NOT NULL,
  last_error       TEXT,
  emailed_at       BIGINT,
  created_at       BIGINT NOT NULL,
  updated_at       BIGINT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_site_feedback_due
  ON site_feedback (status, next_attempt_at);

CREATE UNLOGGED TABLE IF NOT EXISTS rate_limit_windows (
  key           TEXT PRIMARY KEY,
  -- Epoch seconds (fractional) when the window began.
  window_start  DOUBLE PRECISION NOT NULL,
  -- Requests counted in the window, refused ones included.
  count         INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_rate_limit_windows_start
  ON rate_limit_windows (window_start);
