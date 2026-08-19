-- memgres schema v6: chunks become the semantic index; the whole-body doc
-- vector is gone.
--
-- Before, each memory carried ONE embedding of its whole body (`memory.embedding`)
-- for ranking, plus a lazily-filled per-memory segment cache used only for
-- snippets. That wasted the tail of long bodies (a single vector can't represent
-- 60 KB) and embedded the entire body on the write path (slow). Now the CHUNKS
-- are the index: recall max-pools chunk scores per memory (best chunk wins) and
-- the winning chunk is the snippet — one vector store, whole body covered.
--
-- A write no longer embeds inline on the server: it flags `embed_pending`, and a
-- background worker (or, in synchronous/library mode, the write itself) segments
-- the body, embeds the chunks, and clears the flag. Crash-safe: an unfinished
-- row stays flagged and is re-drained on the next start.

ALTER TABLE memory ADD COLUMN IF NOT EXISTS embed_pending boolean NOT NULL DEFAULT false;
-- Partial index over just the pending rows: the worker drains oldest-first.
CREATE INDEX IF NOT EXISTS memory_embed_pending ON memory (updated_at) WHERE embed_pending;

-- One-time data backfill: on any deployment created BEFORE chunks existed
-- (stored schema_version < 6, on EITHER backend), flag every bodied row so the
-- worker rebuilds the chunk index from the bodies. The trigger is the stored
-- version, not the pgvector `embedding` column — a qdrant deployment never had
-- that column, so a column-based guard would skip the backfill there and leave
-- semantic recall silently empty. Idempotent: _stamp bumps the version to 6 at
-- the end of this same migrate(), so a re-run sees 6 (not < 6) and does nothing;
-- a fresh install has no meta row yet (NULL), so it flags nothing either.
DO $$
DECLARE v integer;
BEGIN
    SELECT schema_version INTO v FROM memgres_meta WHERE only_row;
    IF v IS NOT NULL AND v < 6 THEN
        UPDATE memory SET embed_pending = true WHERE body IS NOT NULL AND body <> '';
    END IF;
END $$;

-- Retire the whole-body doc vector (pgvector only — ranking is chunk-based now,
-- so the column and its HNSW index are dead weight). No-op on qdrant, where the
-- column never existed.
DROP INDEX IF EXISTS memory_embedding_hnsw;
ALTER TABLE memory DROP COLUMN IF EXISTS embedding;
