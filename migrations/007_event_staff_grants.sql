-- Migration 007: per-event staff roles
--
-- ADR-0012. A person is put on one event's staff as coordinator, dj or
-- event-manager, by an admin, never globally. event_staff_grants is the
-- record of who is on an event's staff. Each grant also writes one
-- identity_explicit_grants row per scope of the role, on the resource
-- event:<event_id>, so the identity library's decision allows that scope
-- on that event only. The two are written together, by the grant and
-- revoke routes and the event delete, never one without the other.
--
-- The three roles' definitions are identity_roles rows with their scopes in
-- identity_role_scopes, so a grant derives its explicit grants from data.
-- They are never written to identity_principal_roles: that would make them
-- global. The admin role gains the two staff scopes, globally.
--
-- Additive only: a new table and new seed rows, which deejaytools-api
-- ignores. Its event delete still works (ON DELETE CASCADE), though it
-- leaves the event's explicit grants behind.
--
-- Idempotent: safe on a database where any of it is already there.

CREATE TABLE IF NOT EXISTS event_staff_grants (
  event_id    TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
  user_id     TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  role        TEXT NOT NULL
                CHECK (role IN ('coordinator', 'dj', 'event-manager')),
  -- users.id of the admin (or event manager) who granted it.
  granted_by  TEXT NOT NULL,
  -- Epoch milliseconds, as everywhere in this schema.
  granted_at  BIGINT NOT NULL,
  PRIMARY KEY (event_id, user_id, role)
);

-- GET /v1/auth/me lists a person's roles across events.
CREATE INDEX IF NOT EXISTS idx_event_staff_grants_user
  ON event_staff_grants (user_id);

INSERT INTO identity_roles (name, description) VALUES
  ('deejaytools-event-coordinator', 'Per event only: sees what has been entered.'),
  ('deejaytools-event-dj',          'Per event only: runs the floor trials and plays the music.'),
  ('deejaytools-event-manager',     'Per event only: admin, limited to the events granted.')
ON CONFLICT (name) DO NOTHING;

INSERT INTO identity_role_scopes (role_name, scope) VALUES
  ('deejaytools-admin',             'deejaytools.staff.read'),
  ('deejaytools-admin',             'deejaytools.staff.write'),
  ('deejaytools-event-coordinator', 'deejaytools.entries.read'),
  ('deejaytools-event-dj',          'deejaytools.entries.read'),
  ('deejaytools-event-dj',          'deejaytools.queue.read'),
  ('deejaytools-event-dj',          'deejaytools.queue.manage'),
  ('deejaytools-event-dj',          'deejaytools.runs.read'),
  ('deejaytools-event-dj',          'deejaytools.sessions.write'),
  ('deejaytools-event-dj',          'deejaytools.delegation.act'),
  ('deejaytools-event-manager',     'deejaytools.entries.read'),
  ('deejaytools-event-manager',     'deejaytools.queue.read'),
  ('deejaytools-event-manager',     'deejaytools.queue.manage'),
  ('deejaytools-event-manager',     'deejaytools.runs.read'),
  ('deejaytools-event-manager',     'deejaytools.sessions.write'),
  ('deejaytools-event-manager',     'deejaytools.delegation.act'),
  ('deejaytools-event-manager',     'deejaytools.events.write'),
  ('deejaytools-event-manager',     'deejaytools.staff.read'),
  ('deejaytools-event-manager',     'deejaytools.staff.write')
ON CONFLICT (role_name, scope) DO NOTHING;
