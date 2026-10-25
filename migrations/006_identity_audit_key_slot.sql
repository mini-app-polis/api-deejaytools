-- Migration 006: which of a machine's keys proved it
--
-- miniapppolis-identity 2.3.0 records, on each audit event for a
-- key-authenticated request, which of the machine's keys matched: its
-- current key or the previous one it still holds while a rotation overlaps.
-- The previous key is retired once nothing presents it any more, and this
-- column is how that is known. NULL for every other credential, and for rows
-- written before this migration.
--
-- The index holds only the rows that matter to retirement: requests proved
-- by a previous key, which exist only while a rotation is in progress.
--
-- Idempotent: safe on a database where either is already there.

ALTER TABLE identity_audit_events
  ADD COLUMN IF NOT EXISTS key_slot TEXT
    CHECK (key_slot IN ('current', 'previous'));

CREATE INDEX IF NOT EXISTS idx_identity_audit_previous_key
  ON identity_audit_events (subject, occurred_at DESC)
  WHERE key_slot = 'previous';
