-- Migration 002: identity principal store
--
-- Installs the identity principal store in the deejaytools database
-- (deejaytools-api ADR-007, ADR-008 point 4): the same tables as
-- api-kaianolevine-com migration 023, with identity_issuers.jwks_url
-- nullable as its migration 024 left it and the identity library's models
-- expect. One store per ecosystem: the same shape, never the same rows.
--
-- Additive only. deejaytools-api ignores these tables, so traffic can still
-- go back to it.
--
-- What is the same in every environment lives here: the tables, the two
-- roles and their scopes. What differs per environment does not:
--   - the issuer row (each environment has its own Clerk instance)
--   - principals and grants for existing users
-- Both are written by scripts/backfill_principals.py, which runs after this
-- runner on every deploy and reads the issuer from settings, and the issuer
-- row also by POST /v1/auth/sync.

-- ---------------------------------------------------------------------------
-- Core tables
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS identity_issuers (
  issuer        TEXT PRIMARY KEY,
  display_name  TEXT NOT NULL DEFAULT '',
  jwks_url      TEXT,
  enabled       BOOLEAN NOT NULL DEFAULT TRUE,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS identity_roles (
  name          TEXT PRIMARY KEY
                  CHECK (name ~ '^[a-z][a-z0-9-]*$'),
  description   TEXT NOT NULL DEFAULT '',
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS identity_role_scopes (
  role_name     TEXT NOT NULL REFERENCES identity_roles(name) ON DELETE CASCADE,
  scope         TEXT NOT NULL
                  CHECK (scope ~ '^[a-z][a-z0-9-]*(\.[a-z][a-z0-9-]*){2}$'),
  PRIMARY KEY (role_name, scope)
);

CREATE TABLE IF NOT EXISTS identity_principals (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  kind          TEXT NOT NULL CHECK (kind IN ('human', 'machine')),
  issuer        TEXT NOT NULL REFERENCES identity_issuers(issuer),
  subject       TEXT NOT NULL,
  display_name  TEXT NOT NULL DEFAULT '',
  email         TEXT,
  status        TEXT NOT NULL DEFAULT 'active'
                  CHECK (status IN ('active', 'suspended')),
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_seen_at  TIMESTAMPTZ,
  UNIQUE (issuer, subject)
);

CREATE INDEX IF NOT EXISTS idx_identity_principals_issuer_subject
  ON identity_principals(issuer, subject);
CREATE INDEX IF NOT EXISTS idx_identity_principals_kind
  ON identity_principals(kind);

CREATE TABLE IF NOT EXISTS identity_principal_roles (
  principal_id  UUID NOT NULL REFERENCES identity_principals(id) ON DELETE CASCADE,
  role_name     TEXT NOT NULL REFERENCES identity_roles(name) ON DELETE RESTRICT,
  granted_by    TEXT NOT NULL DEFAULT '',
  granted_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (principal_id, role_name)
);

CREATE TABLE IF NOT EXISTS identity_explicit_grants (
  principal_id  UUID NOT NULL REFERENCES identity_principals(id) ON DELETE CASCADE,
  scope         TEXT NOT NULL
                  CHECK (scope ~ '^[a-z][a-z0-9-]*(\.[a-z][a-z0-9-]*){2}$'),
  resource      TEXT NOT NULL,
  granted_by    TEXT NOT NULL DEFAULT '',
  granted_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (principal_id, scope, resource)
);

CREATE TABLE IF NOT EXISTS identity_audit_events (
  event_id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  occurred_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  enforcement_point  TEXT NOT NULL,
  -- No foreign key: an audit event must survive deletion of the principal it
  -- describes, or the trail erases itself exactly when it matters most.
  principal_id       UUID,
  principal_kind     TEXT CHECK (principal_kind IN ('human', 'machine')),
  issuer             TEXT,
  subject            TEXT,
  scope              TEXT NOT NULL,
  resource           TEXT,
  allowed            BOOLEAN NOT NULL,
  reason             TEXT NOT NULL,
  request_id         TEXT
);

CREATE INDEX IF NOT EXISTS idx_identity_audit_occurred_at
  ON identity_audit_events(occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_identity_audit_principal
  ON identity_audit_events(principal_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_identity_audit_enforcement_point
  ON identity_audit_events(enforcement_point, occurred_at DESC);

-- ---------------------------------------------------------------------------
-- Roles and scopes (deejaytools-api ADR-007's route classification)
-- ---------------------------------------------------------------------------

INSERT INTO identity_roles (name, description) VALUES
  ('deejaytools-dancer', 'Every signed-in person, granted on first sync.'),
  ('deejaytools-admin',  'Event organizers: events, sessions, queues, users, Drive jobs.')
ON CONFLICT (name) DO NOTHING;

INSERT INTO identity_role_scopes (role_name, scope) VALUES
  ('deejaytools-dancer', 'deejaytools.profile.write'),
  ('deejaytools-dancer', 'deejaytools.entities.read'),
  ('deejaytools-dancer', 'deejaytools.checkins.read'),
  ('deejaytools-dancer', 'deejaytools.checkins.write'),
  ('deejaytools-dancer', 'deejaytools.submissions.read'),
  ('deejaytools-dancer', 'deejaytools.submissions.write'),
  ('deejaytools-dancer', 'deejaytools.partners.read'),
  ('deejaytools-dancer', 'deejaytools.partners.write'),
  ('deejaytools-dancer', 'deejaytools.teams.read'),
  ('deejaytools-dancer', 'deejaytools.teams.write'),
  ('deejaytools-dancer', 'deejaytools.partnerships.read'),
  ('deejaytools-dancer', 'deejaytools.partnerships.write'),
  ('deejaytools-dancer', 'deejaytools.songs.read'),
  ('deejaytools-dancer', 'deejaytools.songs.write'),
  ('deejaytools-admin',  'deejaytools.events.write'),
  ('deejaytools-admin',  'deejaytools.sessions.write'),
  ('deejaytools-admin',  'deejaytools.queue.read'),
  ('deejaytools-admin',  'deejaytools.queue.manage'),
  ('deejaytools-admin',  'deejaytools.runs.read'),
  ('deejaytools-admin',  'deejaytools.library.read'),
  ('deejaytools-admin',  'deejaytools.entries.read'),
  ('deejaytools-admin',  'deejaytools.users.read'),
  ('deejaytools-admin',  'deejaytools.users.write'),
  ('deejaytools-admin',  'deejaytools.drivejobs.read'),
  ('deejaytools-admin',  'deejaytools.drivejobs.write'),
  ('deejaytools-admin',  'deejaytools.testdata.read'),
  ('deejaytools-admin',  'deejaytools.testdata.write'),
  ('deejaytools-admin',  'deejaytools.delegation.act')
ON CONFLICT (role_name, scope) DO NOTHING;
