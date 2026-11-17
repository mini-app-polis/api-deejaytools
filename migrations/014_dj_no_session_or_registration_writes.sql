-- Migration 014: a DJ no longer changes sessions or registrations
--
-- ADR-0012. A DJ plays the music and runs the queue; changing an event's
-- sessions (create, edit, delete, divisions, status) and its registration
-- list is its event manager's, its coordinator's (registrations) and an
-- admin's. Not every DJ on an event works its floor trials, so session
-- changes stay with the event manager.
--
-- The DJ role's definition loses deejaytools.sessions.write and
-- deejaytools.registrations.write, so a DJ granted from now on never holds
-- them. A staff grant carries its role's scopes as explicit grants on
-- event:<id> (staff.py), so each DJ already on an event holds both there:
-- those grants are removed, unless the same person holds another role on
-- the same event that still carries the scope (an event manager keeps
-- both; a coordinator keeps registrations.write). One explicit grant row
-- stands for a scope however many roles carry it, so whether a row stays
-- is decided by the person's remaining roles on that event, as revoking a
-- role decides it (staff.scopes_to_remove).
--
-- Idempotent: safe to run again.

DELETE FROM identity_role_scopes
WHERE role_name = 'deejaytools-event-dj'
  AND scope IN ('deejaytools.sessions.write', 'deejaytools.registrations.write');

DELETE FROM identity_explicit_grants g
USING identity_principals p
WHERE g.principal_id = p.id
  AND g.scope IN ('deejaytools.sessions.write', 'deejaytools.registrations.write')
  AND g.resource LIKE 'event:%'
  -- A DJ on this event...
  AND EXISTS (
    SELECT 1 FROM event_staff_grants dj
    WHERE dj.user_id = p.subject
      AND dj.role = 'dj'
      AND 'event:' || dj.event_id = g.resource
  )
  -- ...with no other role on it that still carries the scope.
  AND NOT EXISTS (
    SELECT 1
    FROM event_staff_grants other
    JOIN identity_role_scopes rs
      ON rs.role_name = CASE other.role
        WHEN 'coordinator' THEN 'deejaytools-event-coordinator'
        WHEN 'dj' THEN 'deejaytools-event-dj'
        WHEN 'event-manager' THEN 'deejaytools-event-manager'
      END
     AND rs.scope = g.scope
    WHERE other.user_id = p.subject
      AND 'event:' || other.event_id = g.resource
  );
