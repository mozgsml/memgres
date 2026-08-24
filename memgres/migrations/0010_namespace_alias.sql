-- A per-user name for a namespace (schema v11).
--
-- A namespace's `name` is unique per OWNER, so two people may both own 'notes'
-- — and once one of them shares theirs with you, the bare name 'notes' can mean
-- two things in your account. That collision is created by SOMEONE ELSE's act of
-- sharing, after you had already named your own spaces, so it cannot be refused
-- at creation without letting your names block other people from sharing. It is
-- resolved here instead: the affected user gives the space a name of their own.
--
-- An alias is private to one user and points at one namespace. It grants
-- nothing: the target must already be reachable, and every read still goes
-- through the same membership check. It is a label, not a permission.
CREATE TABLE IF NOT EXISTS namespace_alias (
    user_id      uuid        NOT NULL REFERENCES app_user(id)  ON DELETE CASCADE,
    alias        text        NOT NULL,
    namespace_id uuid        NOT NULL REFERENCES namespace(id) ON DELETE CASCADE,
    created_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, alias)
);

-- "which of my aliases point at this namespace" — needed when listing spaces
-- and when a namespace goes away.
CREATE INDEX IF NOT EXISTS namespace_alias_ns_idx
    ON namespace_alias (namespace_id);
