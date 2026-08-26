-- How much a memory is actually used: how often it SURFACES in search, and how
-- often it is READ in full.
--
-- Two counts rather than one, because they answer different questions. Surfacing
-- says the memory is findable — the words in it match what people ask. Being
-- fetched says it was worth opening once found. A memory with many surfacings and
-- no gets is noise in every result list it appears in; one with neither is
-- reachable only by knowing it exists, which is how 42 of this corpus's 97
-- memories already sat.
--
-- A SEPARATE TABLE, not columns on `memory`, and the reason is mechanical:
-- Postgres writes a new version of the WHOLE ROW on every UPDATE, and a memory
-- row carries its body. Counting a read on `memory` would rewrite kilobytes per
-- read, bloat the largest table in the schema, and make every read contend with
-- real writes for the same row lock. It would also muddy `updated_at`, which is
-- supposed to mean "the content changed" — statistics are not content, and for
-- the same reason none of this is folded into the hash chain: `verify_history`
-- must not become a function of how often a memory was read.
--
-- Rows are created lazily on first use, so a memory nobody has touched has no row
-- at all — "never used" and "used zero times" are the same fact, and neither
-- deserves storage.
CREATE TABLE IF NOT EXISTS memory_usage (
    memory_id      uuid PRIMARY KEY REFERENCES memory(id) ON DELETE CASCADE,
    recall_count   bigint      NOT NULL DEFAULT 0,   -- times it came back as a hit
    get_count      bigint      NOT NULL DEFAULT 0,   -- times it was fetched in full
    last_recall_at timestamptz,
    last_get_at    timestamptz
);
