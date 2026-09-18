-- The web panel: browser sessions, sign-in identities, a language (schema v25).
--
-- Everything here is additive. An older client neither reads nor writes these
-- tables, and nothing it relies on changes shape.

-- A browser session. The cookie carries a random id; only its sha256 is stored,
-- so a copy of this table does not let anyone walk in as the people listed in
-- it — the same reason tokens are stored as hashes.
--
-- user_id NULL is the env break-glass root (MEMGRES_ADMIN_TOKEN resolving to no
-- account). Such a session is tied to that secret by `root_fp`, its sha256: if
-- the operator rotates the env token, every session it opened ends with it.
--
-- `token_id` is the credential a token sign-in used. A session opened with a
-- token must not outlive the token, so it is checked again on every request
-- rather than trusted from the moment of sign-in.
CREATE TABLE IF NOT EXISTS web_session (
    id_hash      text        PRIMARY KEY,
    user_id      uuid        REFERENCES app_user(id) ON DELETE CASCADE,
    via          text        NOT NULL,          -- 'token' | 'oidc:<provider>'
    token_id     uuid        REFERENCES token(id) ON DELETE CASCADE,
    root_fp      text,
    csrf         text        NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now(),
    last_seen_at timestamptz NOT NULL DEFAULT now(),
    expires_at   timestamptz NOT NULL,
    revoked_at   timestamptz
);
CREATE INDEX IF NOT EXISTS web_session_user ON web_session (user_id);

-- A way of signing in, attached to an account. The identity is (issuer,
-- subject) — never the email, which is only how a FIRST link may be found.
-- One account may hold any number of these; one (issuer, subject) belongs to
-- exactly one account.
CREATE TABLE IF NOT EXISTS app_user_identity (
    issuer        text        NOT NULL,
    subject       text        NOT NULL,
    user_id       uuid        NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
    provider      text        NOT NULL,         -- the config key it came through
    email         text,
    linked_at     timestamptz NOT NULL DEFAULT now(),
    last_login_at timestamptz,
    PRIMARY KEY (issuer, subject)
);
CREATE INDEX IF NOT EXISTS app_user_identity_user ON app_user_identity (user_id);

-- The panel language a person chose. NULL = follow the browser.
ALTER TABLE app_user ADD COLUMN IF NOT EXISTS ui_language text;

-- A handle for one sign-in method that is not its (issuer, subject): the panel
-- shows and unlinks methods by it, and an issuer URL in a path is awkward.
ALTER TABLE app_user_identity ADD COLUMN IF NOT EXISTS id uuid NOT NULL DEFAULT gen_random_uuid();
CREATE UNIQUE INDEX IF NOT EXISTS app_user_identity_id ON app_user_identity (id);

-- One OIDC sign-in in progress: between the redirect to the provider and its
-- redirect back. Single use, short-lived. `state` is looked up by hash, and the
-- flow is bound to the browser that started it (`browser_hash` = sha256 of a
-- cookie set at the start), so a callback URL carried to another browser — or
-- planted in a victim's — completes nothing.
--
-- `link_user_id` is set when a signed-in person is adding a method to their own
-- account. It is recorded here at the start because the callback arrives as a
-- cross-site navigation, which does not carry the SameSite=Strict session.
CREATE TABLE IF NOT EXISTS oidc_flow (
    state_hash    text        PRIMARY KEY,
    browser_hash  text        NOT NULL,
    provider      text        NOT NULL,
    nonce         text        NOT NULL,
    code_verifier text        NOT NULL,
    link_user_id  uuid        REFERENCES app_user(id) ON DELETE CASCADE,
    created_at    timestamptz NOT NULL DEFAULT now(),
    expires_at    timestamptz NOT NULL
);

-- Someone a provider vouched for, whom no rule let straight in. No account is
-- created until an administrator decides: link them to an existing account
-- (the one whose email matched is suggested), create one, or reject.
CREATE TABLE IF NOT EXISTS signin_request (
    id                uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    provider          text        NOT NULL,
    issuer            text        NOT NULL,
    subject           text        NOT NULL,
    email             text,
    email_verified    boolean     NOT NULL DEFAULT false,
    name              text        NOT NULL DEFAULT '',
    suggested_user_id uuid        REFERENCES app_user(id) ON DELETE SET NULL,
    status            text        NOT NULL DEFAULT 'pending'
                                  CHECK (status IN ('pending', 'approved', 'rejected')),
    created_at        timestamptz NOT NULL DEFAULT now(),
    last_seen_at      timestamptz NOT NULL DEFAULT now(),
    decided_at        timestamptz,
    decided_by        uuid        REFERENCES app_user(id) ON DELETE SET NULL,
    user_id           uuid        REFERENCES app_user(id) ON DELETE SET NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS signin_request_open
    ON signin_request (issuer, subject) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS signin_request_seen ON signin_request (issuer, subject, decided_at);

-- The sign-in method a provider session was opened with, so unlinking one
-- method ends exactly its sessions and not those of another method from the
-- same provider.
ALTER TABLE web_session ADD COLUMN IF NOT EXISTS identity_id uuid;

-- A link flow is authorised by the session that started it, and that session
-- must still be alive when the provider sends the person back: signing out,
-- revoking the token or disabling the account in those minutes stops the link.
ALTER TABLE oidc_flow ADD COLUMN IF NOT EXISTS link_session_hash text;

-- An invitation into a space for an address that has no account yet. Someone
-- who already has an account with that email is added straight away and no row
-- is written. The invitation waits until a sign-in arrives whose provider
-- vouches for the address (email_verified), and is applied then — it opens the
-- space, never the server: whether that sign-in is let in at all is still
-- decided by the provider's rules and the administrators.
CREATE TABLE IF NOT EXISTS space_invite (
    id           uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    namespace_id uuid        NOT NULL REFERENCES namespace(id) ON DELETE CASCADE,
    email        text        NOT NULL,
    permission   text        NOT NULL CHECK (permission IN ('read', 'write', 'admin')),
    invited_by   uuid        REFERENCES app_user(id) ON DELETE SET NULL,
    created_at   timestamptz NOT NULL DEFAULT now(),
    expires_at   timestamptz NOT NULL,
    accepted_at  timestamptz,
    user_id      uuid        REFERENCES app_user(id) ON DELETE SET NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS space_invite_open
    ON space_invite (namespace_id, lower(email)) WHERE accepted_at IS NULL;
CREATE INDEX IF NOT EXISTS space_invite_email
    ON space_invite (lower(email)) WHERE accepted_at IS NULL;

-- A person's write activity, by day: the profile chart reads one author's
-- history over a window, which the author-only index answers with a sort.
CREATE INDEX IF NOT EXISTS memory_history_author_time
    ON memory_history (author_user_id, created_at) WHERE author_user_id IS NOT NULL;
