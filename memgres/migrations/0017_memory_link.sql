-- The link graph: which memory points at which.
--
-- `[[wiki links]]` were already the corpus convention (238 of them across 97
-- memories with no tool support), but living only inside body text they could
-- answer just one question — "where does this go?" — by reading. They could not
-- answer "what points HERE", which is the question that matters when a fact
-- changes and you need to know who is relying on it. Nor could anything notice a
-- link that stopped resolving.
--
-- Edges are RESOLVED AT WRITE and pin `dst_id`. That is what makes a link
-- survive its target moving, or its old path being claimed by something else: the
-- id is the identity, the text in the body is only a label. Notion, Confluence,
-- org-mode's `id:` links and Logseq block refs all work this way.
--
-- `dst_id` is nullable and ON DELETE SET NULL, which covers the two states that
-- are not "resolved":
--   * a link written before its target exists — a deliberate "this deserves a
--     memory" marker, which must be allowed to stand;
--   * a target that was later erased. `forget` is real deletion and leaves no
--     redirect, so without this the edge would either vanish or, worse, point at
--     whatever later took the path. Nulling it makes the loss VISIBLE instead.
-- `raw_target` is kept either way, so a dangling edge still says what it wanted.
CREATE TABLE IF NOT EXISTS memory_link (
    src_id     uuid    NOT NULL REFERENCES memory(id) ON DELETE CASCADE,
    ord        integer NOT NULL,               -- order of appearance in the body
    dst_id     uuid    REFERENCES memory(id) ON DELETE SET NULL,
    raw_target text    NOT NULL,               -- as written: 'ops.x402.deploy'
    label      text,                           -- after '|', shown instead
    anchor     text,                           -- after '#', a section hint
    scheme     text,                           -- NULL = a path here; else 'idea'/'file'
    PRIMARY KEY (src_id, ord)
);

-- "what points here" — the whole reason the table exists.
CREATE INDEX IF NOT EXISTS memory_link_dst ON memory_link (dst_id);
-- late binding: when a memory appears at a path, find the edges that were
-- waiting for it. Partial, because only unresolved edges are ever looked up
-- this way and they are the small minority.
CREATE INDEX IF NOT EXISTS memory_link_pending ON memory_link (raw_target)
    WHERE dst_id IS NULL AND scheme IS NULL;
