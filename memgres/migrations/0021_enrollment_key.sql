-- One-time enrollment keys: bind a SELF-GENERATED token to an existing account
-- (schema v22).
--
-- The problem this closes: every way of handing someone their first token puts
-- the secret somewhere it should not be. Returned in a reply, it lands in an
-- LLM transcript. Mailed, it rests forever in a mailbox, its backups and its
-- compliance archive. Written to a file on the server, it is unreachable for
-- anyone without shell access.
--
-- So the secret stops travelling. The person generates their own `mgk_` token
-- on their own machine, puts it in their client's configuration, and redeems a
-- short-lived `mge_` key that says WHICH account it belongs to. The server
-- stores the token's hash, exactly as if it had minted it. This is the shape
-- ssh, Tailscale, `kubeadm join` and Vault's AppRole all settled on: a
-- one-time grant to bind, a durable credential created client-side.
--
-- The key IS a credential while it lives, which is why it is short-lived, may
-- be redeemed once, and records `used_at` — a theft shows up as the legitimate
-- owner finding the key already spent, which a stolen token never does.
--
-- FKs: `user_id` cascades (a deleted account's pending keys are meaningless).
-- `created_by` and `used_token_id` deliberately have NONE — they are audit,
-- and an audit trail that disappears when its subject does is not one.
CREATE TABLE IF NOT EXISTS enrollment_key (
    id            uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    key_hash      text        NOT NULL UNIQUE,
    user_id       uuid        NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
    namespace_id  uuid,       -- scope of the token this will create (NULL = all theirs)
    permission    text        NOT NULL CHECK (permission IN ('read','write','admin')),
    label         text        NOT NULL DEFAULT '',
    created_by    uuid,       -- who issued it (audit; no FK on purpose)
    created_at    timestamptz NOT NULL DEFAULT now(),
    expires_at    timestamptz NOT NULL,
    used_at       timestamptz,
    used_token_id uuid,       -- what redeeming it produced (audit; no FK on purpose)
    revoked_at    timestamptz
);

-- "show me this person's pending keys" is the only listing there is.
CREATE INDEX IF NOT EXISTS enrollment_key_user_idx ON enrollment_key (user_id);
