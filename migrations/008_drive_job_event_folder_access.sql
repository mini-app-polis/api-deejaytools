-- Migration 008: Drive jobs that share an event's folder with its staff
--
-- ADR-0012, "The DJ's Drive folder". Granting DJ or event manager on an
-- event shares the event's Drive folder with the person as a reader;
-- revoking the last such role unshares it. Both run as durable drive_jobs,
-- kinds share_event_folder and unshare_event_folder, so a Drive failure is
-- retried and visible like any other job. A job is about one person on one
-- event: event_id and user_id say which. A share job records the folder it
-- shared in file_id, which is what the unshare later removes access from.
--
-- Neither column is a foreign key: like submission_id, the job outlives
-- its rows (an unshare runs after the event or the grant is gone).
--
-- Additive: two nullable columns on drive_jobs, which deejaytools-api
-- never reads. NULL on every row queued before this migration.
--
-- Idempotent: safe on a database where the columns are already there.

ALTER TABLE drive_jobs ADD COLUMN IF NOT EXISTS event_id TEXT;
ALTER TABLE drive_jobs ADD COLUMN IF NOT EXISTS user_id TEXT;
