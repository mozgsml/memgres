-- memgres service roles (schema v9, additive — old readers ignore the column).
--
-- A user's *service* role, orthogonal to the per-namespace membership lattice
-- (read/write/admin) which governs data access WITHIN a space. The service role
-- governs the CONTROL PLANE — provisioning users/tokens and, for a superadmin,
-- reaching across namespaces:
--
--   user          (default) — owns namespaces, manages access to its OWN spaces
--                             (via request/approve); no cross-tenant powers.
--   user_manager            — user + create users + (re)issue tokens on loss.
--                             Provisioning only; does NOT read others' data nor
--                             manage other spaces' access.
--   superadmin              — full root: read/write ANY namespace, grant any
--                             access, grant/revoke service roles. Principal.is_admin
--                             derives from this (replaces the anonymous env-root).
--
-- Bootstrap seeds the FIRST admin user (see identity.bootstrap_admin); the role
-- it seeds is MEMGRES_ADMIN_ROLE (default user_manager). Later escalation is the
-- memgres-grant-superadmin CLI, not this migration.
ALTER TABLE app_user
    ADD COLUMN IF NOT EXISTS role text NOT NULL DEFAULT 'user'
        CHECK (role IN ('user', 'user_manager', 'superadmin'));

-- Find superadmins fast (anti-lockout counts them; bootstrap checks for zero
-- admins of any kind). Partial index — the admin rows are a tiny minority.
CREATE INDEX IF NOT EXISTS app_user_role_idx ON app_user (role)
    WHERE role <> 'user';
