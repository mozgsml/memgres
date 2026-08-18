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

-- Retire the whole-body doc vector. Ranking is chunk-based now, so this column
-- and its HNSW index are dead weight. Bodies and history are untouched; the
-- chunk vectors are rebuilt from the bodies. Guarded so the one-time data
-- backfill (flag every existing row for re-chunking) runs ONLY on the upgrade
-- that still has the column — re-running migrate() after the drop is a no-op.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name = 'memory' AND column_name = 'embedding') THEN
        UPDATE memory SET embed_pending = true WHERE body IS NOT NULL AND body <> '';
    END IF;
END $$;
DROP INDEX IF EXISTS memory_embedding_hnsw;
ALTER TABLE memory DROP COLUMN IF EXISTS embedding;
