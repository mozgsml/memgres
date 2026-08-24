-- memgres identity, tenancy & tokens (schema v2, payment-agnostic).
--
-- Always applied and fully idempotent (CREATE … IF NOT EXISTS). In `single`
-- mode these tables simply stay empty — memory.namespace keeps holding '' and no
-- auth happens. In `open`/`managed` mode identity.py fills them and
-- memory.namespace holds a namespace **id as text** (no column retype: the
-- existing (namespace, path) uniqueness and namespace index carry over).
--
-- Nothing here conflates payment: no wallet/principal column exists. A gateway
-- or service layer is just an ordinary `app_user` that routes on top.

-- ─── users: the top identity; owns namespaces, holds tokens ──────────────────
CREATE TABLE IF NOT EXISTS app_user (
    id                   uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    name                 text        NOT NULL DEFAULT '',
    description          text        NOT NULL DEFAULT '',
    default_namespace_id uuid,       -- FK added below once namespace exists (nullable)
    created_at           timestamptz NOT NULL DEFAULT now()
);

-- ─── namespaces: the isolation unit, owned by a user ─────────────────────────
-- name is unique PER OWNER, not globally: two users may both own 'notes'.
-- description/instruction tell an agent how to use the space.
CREATE TABLE IF NOT EXISTS namespace (
    id            uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    name          text        NOT NULL,
    description   text        NOT NULL DEFAULT '',
    instruction   text        NOT NULL DEFAULT '',
    owner_user_id uuid        NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
    created_at    timestamptz NOT NULL DEFAULT now(),
    UNIQUE (owner_user_id, name)
);

CREATE INDEX IF NOT EXISTS namespace_owner_idx ON namespace (owner_user_id);

-- default_namespace_id → namespace(id); SET NULL if that namespace is deleted.
--
-- Guarded on the column still being there, because 0011 drops it: every
-- migration is re-applied on every start (idempotency comes from IF NOT EXISTS,
-- not from a version ledger), so on an already-migrated database this block runs
-- again after the column is gone. Without the guard the whole migration chain
-- fails on the second start — which is to say, on every restart after upgrading.
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name = 'app_user'
                 AND column_name = 'default_namespace_id') THEN
        ALTER TABLE app_user
            ADD CONSTRAINT app_user_default_ns_fk
            FOREIGN KEY (default_namespace_id) REFERENCES namespace(id) ON DELETE SET NULL;
    END IF;
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- ─── cross-user sharing: a member gets access to a namespace they don't own ──
CREATE TABLE IF NOT EXISTS namespace_member (
    namespace_id uuid        NOT NULL REFERENCES namespace(id) ON DELETE CASCADE,
    user_id      uuid        NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
    permission   text        NOT NULL CHECK (permission IN ('read','write','admin')),
    created_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (namespace_id, user_id)
);

CREATE INDEX IF NOT EXISTS namespace_member_user_idx ON namespace_member (user_id);

-- ─── tokens: rotatable, temporary, restrictable credentials for a user ───────
-- Stored only as sha256(secret). namespace_id NULL = reaches all the user's
-- namespaces; set = scoped to one. permission is a ceiling min()'d with the
-- user's membership on the target space.
CREATE TABLE IF NOT EXISTS token (
    id           uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    token_hash   text        NOT NULL UNIQUE,          -- sha256(secret) hex
    user_id      uuid        NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
    namespace_id uuid        REFERENCES namespace(id) ON DELETE CASCADE,  -- NULL = all
    permission   text        NOT NULL DEFAULT 'write'
                             CHECK (permission IN ('read','write','admin')),
    label        text        NOT NULL DEFAULT '',
    expires_at   timestamptz,                          -- NULL = no expiry
    revoked_at   timestamptz,                          -- non-NULL = dead
    last_used_at timestamptz,
    created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS token_user_idx ON token (user_id);

-- ─── access requests: ask an admin to join an existing namespace ─────────────
CREATE TABLE IF NOT EXISTS access_request (
    id                   uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    requester_user_id    uuid        NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
    namespace_id         uuid        NOT NULL REFERENCES namespace(id) ON DELETE CASCADE,
    requested_permission text        NOT NULL DEFAULT 'read'
                                     CHECK (requested_permission IN ('read','write','admin')),
    status               text        NOT NULL DEFAULT 'pending'
                                     CHECK (status IN ('pending','approved','denied')),
    created_at           timestamptz NOT NULL DEFAULT now(),
    decided_at           timestamptz,
    UNIQUE (requester_user_id, namespace_id)
);

CREATE INDEX IF NOT EXISTS access_request_ns_idx
    ON access_request (namespace_id) WHERE status = 'pending';
