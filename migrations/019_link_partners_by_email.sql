-- Migration 019: link partners to accounts by email
--
-- A partner row is linked (linked_user_id) to the account whose users.email
-- is the row's email, trimmed and in any case. The link shares the row
-- owner's songs with that partner with the account. From this release the
-- API keeps the link in step (adding a partner, changing their email, and an
-- account's sign-in); nothing set it before, so this links the rows that
-- already exist.
--
-- Only unlinked kind = 'partner' rows with an email; never to the row's own
-- owner; and only where exactly one account has the email, ignoring case.
--
-- Data only. Idempotent: a second run finds no unlinked row to link.

UPDATE partners p
SET linked_user_id = (
  SELECT u.id FROM users u
  WHERE lower(btrim(u.email)) = lower(btrim(p.email))
)
WHERE p.linked_user_id IS NULL
  AND p.kind = 'partner'
  AND p.email IS NOT NULL
  AND btrim(p.email) <> ''
  AND (
    SELECT count(*) FROM users u
    WHERE lower(btrim(u.email)) = lower(btrim(p.email))
  ) = 1
  AND NOT EXISTS (
    SELECT 1 FROM users u
    WHERE lower(btrim(u.email)) = lower(btrim(p.email))
      AND u.id = p.user_id
  );
