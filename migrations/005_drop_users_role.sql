-- Migration 005: drop users.role
--
-- Admin access is the deejaytools-admin grant on a person's principal in
-- identity_principal_roles; nothing reads or writes users.role. The column
-- and its user_role enum, which nothing else uses, are dropped. Grants are
-- not touched.
--
-- Idempotent: safe on a database where either is already gone.

ALTER TABLE users DROP COLUMN IF EXISTS role;
DROP TYPE IF EXISTS user_role;
