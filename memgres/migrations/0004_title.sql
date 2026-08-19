-- memgres schema v5: a curated `title` on each memory + audited title changes.
--
-- `title` is a short, human-curated caption, distinct from the body's first-line
-- preview. It is set as a whole value (no diff) and is searchable on its own.
--
-- title_fts is a SEPARATE tsvector (not folded into the body `fts`) so a title
-- search doesn't dilute body ranking and vice-versa. The store maintains it with
-- the configured FTS dictionary, exactly like `fts`. Existing rows get title=''
-- and title_fts NULL (no title yet) — harmless.
--
-- Title changes are audited in the hash-chained history the same shape as
-- tags/path: title_before/title_after are NULL on rows that didn't touch the
-- title, and store._row_hash folds them into the chain ONLY when present (a
-- domain-separated wrapper, like author), so every pre-title row keeps its exact
-- digest and still verifies.

ALTER TABLE memory ADD COLUMN IF NOT EXISTS title    text NOT NULL DEFAULT '';
ALTER TABLE memory ADD COLUMN IF NOT EXISTS title_fts tsvector;
CREATE INDEX IF NOT EXISTS memory_title_fts_idx ON memory USING gin (title_fts);

ALTER TABLE memory_history ADD COLUMN IF NOT EXISTS title_before text;
ALTER TABLE memory_history ADD COLUMN IF NOT EXISTS title_after  text;
