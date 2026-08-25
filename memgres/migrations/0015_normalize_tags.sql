-- One spelling per tag.
--
-- Tags are matched byte-for-byte by a GIN index, so `X402` and `x402` were two
-- unrelated labels, and so were the two Unicode spellings of "й" (U+0439 vs
-- "и" + a combining breve) that Cyrillic text arrives in depending on the
-- editor. Runtime normalisation (memgres/tags.py) handles new writes and every
-- filter; this pass brings the rows already stored into the same spelling, so an
-- old tag still matches a new filter.
--
-- `lower(normalize(...))` deliberately mirrors what tags.py does — Python's
-- casefold() is the better caseless primitive, but it does NOT always agree with
-- SQL lower(), and the two sides must produce identical strings or this
-- migration silently splits the vocabulary it was written to merge.
--
-- ORDER IS PRESERVED, first occurrence wins. `array_agg(DISTINCT …)` would have
-- been shorter, but DISTINCT sorts — and `normalize_tags` keeps the caller's
-- order (there is a test pinning that). Sorting here would mean:
--   * stored tags silently diverging from `memory_history.tags_after`, with
--     `verify_history` still reporting True — nothing anywhere would flag it;
--   * a client re-sending its own unchanged tag list being read as a CHANGE:
--     a phantom `retag` row, a seq bump, an expiry renewal — and then the next
--     start re-sorting it again, forever;
--   * a full-table UPDATE on every boot, since migrations re-apply on every
--     start and the docstring promises a re-run is a no-op.
-- The `IS DISTINCT FROM` guard is what makes that promise true: a row already in
-- the canonical spelling and order is not written at all.
UPDATE memory m
   SET tags = COALESCE((
         SELECT array_agg(d.norm ORDER BY d.first_at)
           FROM (SELECT DISTINCT ON (u.norm) u.norm, u.ord AS first_at
                   FROM (SELECT lower(normalize(btrim(x), NFC)) AS norm, ord
                           FROM unnest(m.tags) WITH ORDINALITY AS t(x, ord)
                          WHERE btrim(x) <> '') u
                  ORDER BY u.norm, u.ord) d
       ), '{}')
 WHERE m.tags <> '{}'
   AND m.tags IS DISTINCT FROM COALESCE((
         SELECT array_agg(d.norm ORDER BY d.first_at)
           FROM (SELECT DISTINCT ON (u.norm) u.norm, u.ord AS first_at
                   FROM (SELECT lower(normalize(btrim(x), NFC)) AS norm, ord
                           FROM unnest(m.tags) WITH ORDINALITY AS t(x, ord)
                          WHERE btrim(x) <> '') u
                  ORDER BY u.norm, u.ord) d
       ), '{}');
