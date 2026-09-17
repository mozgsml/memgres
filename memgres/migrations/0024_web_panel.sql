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
