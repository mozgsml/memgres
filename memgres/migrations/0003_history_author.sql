-- memgres schema v4: authoritative authorship on every history row.
--
-- Until now a history row carried only source/reason — free text the client
-- supplies. In a shared namespace (several users on one space) that can't say
-- who *actually* made an edit. The server knows the authenticated principal
-- (token → user) on every write; these columns record it, server-stamped, so
-- authorship is authoritative and separate from the where/why of source/reason.
--
-- Deliberately NO foreign key to app_user / token: this is an immutable audit
-- record folded into the hash chain. Deleting a user must not mutate (and thus
-- break the verifiability of) unrelated memories' history. The id is retained
-- as a pseudonymous stamp; a name is resolved by LEFT JOIN at read time, and a
-- deleted user simply reads back as its bare id. (A dedicated author-purge is a
-- separate future admin op, out of scope here.)
--
-- Both columns are NULL for user-less writes — single mode (no identity) and the
-- global-admin env token (not an app_user). Those rows hash exactly as before,
-- which is what keeps pre-upgrade chains verifiable (see store._row_hash).

ALTER TABLE memory_history ADD COLUMN IF NOT EXISTS author_user_id  uuid;
ALTER TABLE memory_history ADD COLUMN IF NOT EXISTS author_token_id uuid;

-- Audit-by-author ("what did this user change") stays cheap on a large history.
CREATE INDEX IF NOT EXISTS memory_history_author_idx
    ON memory_history (author_user_id) WHERE author_user_id IS NOT NULL;
