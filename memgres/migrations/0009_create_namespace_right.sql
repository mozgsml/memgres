-- Schema v10: who may bring a namespace into existence.
--
-- Until now any unscoped write token could: addressing a namespace by a name
-- that did not exist created it, and writing with no address at all created a
-- "default" one. That is convenient for a single-user install and wrong for a
-- shared one, where an agent's typo silently forks a second store nobody is
-- looking at and where the operator has no way to say "use the space you were
-- given".
--
-- The right is per user rather than per role: an ordinary member may be trusted
-- to organize their own corner without being handed the ability to provision
-- other people. Admin roles (user_manager, superadmin) may always create and do
-- not consult this column.
--
-- Backfill deliberately grants it to every EXISTING account. They have had the
-- right since the deployment was created; taking it away during an upgrade
-- would break running writes with a permission error. New accounts start
-- without it, which is the behaviour we actually want going forward.
ALTER TABLE app_user
    ADD COLUMN IF NOT EXISTS can_create_namespace boolean NOT NULL DEFAULT false;

UPDATE app_user SET can_create_namespace = true
 WHERE can_create_namespace = false;
