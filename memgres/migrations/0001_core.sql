-- memgres core schema (config-independent parts).
--
-- This file is always applied. Parts that depend on runtime config are added by
-- memgres/schema.py after this runs:
--   * ltree `path` column + GiST index      — only when MEMGRES_TREE=true
--   * pgvector `embedding` column + HNSW     — only when embeddings are enabled
--   * the meta row is stamped/verified there — hard-fail on model/dim drift
--
-- Nothing here needs a contrib extension: tags (text[]+GIN), FTS (tsvector+GIN)
-- and gen_random_uuid() are all core Postgres (>=13). ltree/vector are pulled in
-- conditionally by schema.py so a deployment without them still migrates.

-- ─── one memory = one mutable body ───────────────────────────────────────────
CREATE TABLE IF NOT EXISTS memory (
    id            uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    namespace     text        NOT NULL DEFAULT '',   -- '' = single space; else hash(token)
    body          text        NOT NULL,
    content_hash  text        NOT NULL,              -- sha256(body) hex; OCC + dedup
    tags          text[]      NOT NULL DEFAULT '{}', -- cross-cutting labels
    fts           tsvector,                          -- store fills with the configured dict
    seq           integer     NOT NULL DEFAULT 0,    -- current version; == latest history.seq
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    expires_at    timestamptz                        -- NULL = keep forever
);

CREATE INDEX IF NOT EXISTS memory_ns_idx      ON memory (namespace);
CREATE INDEX IF NOT EXISTS memory_tags_idx    ON memory USING gin (tags);
CREATE INDEX IF NOT EXISTS memory_fts_idx     ON memory USING gin (fts);
CREATE INDEX IF NOT EXISTS memory_expires_idx ON memory (expires_at) WHERE expires_at IS NOT NULL;

-- ─── hash-chained, deletable history (git-like provenance, GDPR-erasable) ─────
-- Deleted with the memory (ON DELETE CASCADE) → real erasure, not "hidden".
-- row_hash chains over the *history* rows so tampering is detectable; the chain
-- is for audit, it does not prevent deletion (that is the whole point vs git).
CREATE TABLE IF NOT EXISTS memory_history (
    id            bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    memory_id     uuid        NOT NULL REFERENCES memory(id) ON DELETE CASCADE,
    seq           integer     NOT NULL,              -- 1,2,3… per memory
    op            text        NOT NULL,              -- create|diff|replace|move|retag|delete
    diff          text,                              -- unified diff of body (NULL = metadata-only)
    hash_before   text,                              -- body content_hash before (NULL on create)
    hash_after    text,                              -- body content_hash after  (NULL on delete)
    path_before   text,                              -- ltree as text; records a move
    path_after    text,
    tags_before   text[],
    tags_after    text[],
    source        text,                              -- provenance: where this change came from
    reason        text,                              -- provenance: why
    prev_row_hash text,                              -- previous row_hash (NULL for seq=1)
    row_hash      text        NOT NULL,              -- H(prev_row_hash‖memory_id‖seq‖op‖…)
    created_at    timestamptz NOT NULL DEFAULT now(),
    UNIQUE (memory_id, seq)
);

CREATE INDEX IF NOT EXISTS memory_history_mid_idx ON memory_history (memory_id, seq);

-- ─── single-row meta: stamps what the collection was built with ──────────────
-- schema.py verifies config against this and HARD-FAILS on drift (indexing with
-- one embedding model and querying with another silently returns garbage —
-- a documented silent-failure class this guard exists to prevent).
CREATE TABLE IF NOT EXISTS memgres_meta (
    only_row       boolean     PRIMARY KEY DEFAULT true CHECK (only_row),
    schema_version integer     NOT NULL,
    embed_provider text        NOT NULL,
    embed_model    text        NOT NULL DEFAULT '',
    embed_dim      integer     NOT NULL DEFAULT 0,
    fts_language   text        NOT NULL,
    tree_enabled   boolean     NOT NULL,
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now()
);
