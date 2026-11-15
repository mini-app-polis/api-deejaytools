-- Migration 012: admins move an event entry to another division
--
-- PATCH /v1/admin/event-song-submissions/{id} moves one song submission to
-- another division of the same event: the submission's per-event division
-- override changes in place, and a drive job moves and renames its copy in
-- the event's Drive folder. It is behind a new scope,
-- deejaytools.entries.write, on deejaytools-admin only: no per-event staff
-- role carries it.
--
-- Additive only. Idempotent: safe to run again.

INSERT INTO identity_role_scopes (role_name, scope) VALUES
  ('deejaytools-admin', 'deejaytools.entries.write')
ON CONFLICT (role_name, scope) DO NOTHING;
