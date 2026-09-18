-- Re-derive the link graph, because the PARSER changed, not the schema.
--
-- Until now `_PATH` rejected the hyphen, so every `[[infra.servers.video-production]]`
-- was classified as prose and dropped — not stored as a dangling edge, dropped —
-- while `[[proxies]]` from a TOML example indented as code was stored as a real
-- one. Both halves are fixed in `links.py`; neither fix reaches a body that is
-- already written, since edges are derived on write.
--
-- Clearing the flag makes the existing one-time backfill (`relink.rebuild`, run
-- at startup by `maybe_backfill`) do the pass again over every body. It rewrites
-- only the edge table: bodies, history and the hash chain are untouched, so this
-- is repeatable and costs nothing but a scan.
--
-- Additive by nature — an older client reading this database is no worse off
-- than it was, so the compatibility floor does not move.
-- ONCE. Every migration file runs again at every start (they are meant to be
-- idempotent), and a bare UPDATE here cleared the flag on every start — so every
-- start rebuilt the whole link graph, and two servers starting together raced on
-- it. The marker column makes the clearing happen the first time only; on a
-- database that has been through this file before, it happens one last time.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_schema = current_schema() AND table_name = 'memgres_meta'
                     AND column_name = 'relinked_after_0023') THEN
        ALTER TABLE memgres_meta ADD COLUMN relinked_after_0023 boolean NOT NULL DEFAULT true;
        UPDATE memgres_meta SET links_built = false;
    END IF;
END $$;
