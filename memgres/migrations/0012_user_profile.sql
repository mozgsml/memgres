-- Who a user actually is (schema v13).
--
-- `app_user` held a `name` and a `description`, and `name` was doing two jobs at
-- once: the handle a token resolves to, and the thing a person reads in `blame`
-- and `history`. It is not unique and may be empty, so an authorship line could
-- come back as a bare uuid — which is an audit trail nobody can act on.
--
-- These are plain columns on the user, not an org-structure table: `department`
-- and `position` are free text. Departments as objects — nesting, heads, moves —
-- are a different thing with their own relationships, and are better introduced
-- deliberately than grown out of two text fields.
--
-- `email` is the one with a constraint: unique when present, because it is the
-- natural login for the web panel that will come later, and a duplicate would
-- make it useless for that. NULL is allowed and repeats freely (Postgres does
-- not compare NULLs in a unique index), so accounts that are services rather
-- than people are unaffected.
--
-- 🔴 It is NOT verified. Anyone who may provision users can set any address on
-- any plain-user account, which means an address can be claimed before its real
-- owner has one. That is harmless while email is only a label — and becomes an
-- authentication bypass the day something treats it as identity. Whatever adds
-- email login must add ownership verification in the same change, not after.
ALTER TABLE app_user ADD COLUMN IF NOT EXISTS email       text;
ALTER TABLE app_user ADD COLUMN IF NOT EXISTS full_name   text NOT NULL DEFAULT '';
ALTER TABLE app_user ADD COLUMN IF NOT EXISTS department  text NOT NULL DEFAULT '';
ALTER TABLE app_user ADD COLUMN IF NOT EXISTS position    text NOT NULL DEFAULT '';

CREATE UNIQUE INDEX IF NOT EXISTS app_user_email_uniq
    ON app_user (lower(email)) WHERE email IS NOT NULL;
