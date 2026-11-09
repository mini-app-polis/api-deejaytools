-- Migration 011: event staff manage their event's registration list
--
-- An event's coordinator, DJ and event manager may import, add to, mark and
-- remove entries on the event's registration list, on that event only
-- (ADR-0012). Each staff role's definition gains
-- deejaytools.registrations.write; a staff grant carries its role's scopes as
-- explicit grants on event:<id>, so people already on an event's staff get
-- the new scope there too. Every staff role carries
-- deejaytools.entries.read, so each existing explicit grant of it on an
-- event marks one person on that event's staff.
--
-- Additive only. Idempotent: safe to run again.

INSERT INTO identity_role_scopes (role_name, scope) VALUES
  ('deejaytools-event-coordinator', 'deejaytools.registrations.write'),
  ('deejaytools-event-dj',          'deejaytools.registrations.write'),
  ('deejaytools-event-manager',     'deejaytools.registrations.write')
ON CONFLICT (role_name, scope) DO NOTHING;

INSERT INTO identity_explicit_grants (principal_id, scope, resource, granted_by)
SELECT principal_id, 'deejaytools.registrations.write', resource, granted_by
FROM identity_explicit_grants
WHERE scope = 'deejaytools.entries.read'
  AND resource LIKE 'event:%'
ON CONFLICT (principal_id, scope, resource) DO NOTHING;
