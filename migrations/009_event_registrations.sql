-- Migration 009: event registration lists
--
-- An admin's copy of an event's registration list (who entered which
-- division), pasted in from the event's own registration export, so the
-- admin page can show who has not submitted music yet. Optional per event:
-- an event with no rows simply has no list.
--
-- Each row is one registered entry. Re-importing replaces the event's list
-- but keeps the admin's marks (linked_label, handled, note) on entries
-- whose (division, dancer_1, dancer_2) is unchanged; entry_key is that
-- triple, normalized by the API, so the match survives case, accent and
-- spacing differences between exports.
--
-- Admin only: two new scopes, deejaytools.registrations.read and
-- deejaytools.registrations.write, on deejaytools-admin.
--
-- Additive only: a new table and new seed rows.
--
-- Idempotent: safe on a database where any of it is already there.

CREATE TABLE IF NOT EXISTS event_registrations (
  id           TEXT PRIMARY KEY,
  event_id     TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
  -- Order in the pasted list, from 0.
  position     INTEGER NOT NULL,
  -- The division as the registration names it, not one of /v1/divisions.
  division     TEXT NOT NULL,
  dancer_1     TEXT NOT NULL,
  dancer_2     TEXT,
  -- Normalized (division, dancer_1, dancer_2): what a re-import matches on.
  entry_key    TEXT NOT NULL,
  -- A submission's partnership label the admin linked this entry to by
  -- hand, for names the automatic match misses.
  linked_label TEXT,
  -- The admin marked it as needing no submission here (handled elsewhere).
  handled      BOOLEAN NOT NULL DEFAULT false,
  note         TEXT,
  -- Epoch milliseconds, as everywhere in this schema.
  created_at   BIGINT NOT NULL,
  updated_at   BIGINT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_event_registrations_event
  ON event_registrations (event_id, position);

INSERT INTO identity_role_scopes (role_name, scope) VALUES
  ('deejaytools-admin', 'deejaytools.registrations.read'),
  ('deejaytools-admin', 'deejaytools.registrations.write')
ON CONFLICT (role_name, scope) DO NOTHING;
