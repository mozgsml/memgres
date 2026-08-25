-- One spelling per tag.
--
-- Tags are matched byte-for-byte by a GIN index, so `X402` and `x402` were two
-- unrelated labels, and so were the two Unicode spellings of "й" (U+0439 vs
-- "и" + a combining breve) that Cyrillic text arrives in depending on the
-- editor. The result in this repo's own corpus: 265 distinct tags across 97
-- memories, filtering by them useless — not because the index was broken but
-- because nothing agreed on the spelling.
--
-- Runtime normalisation (memgres/tags.py) handles new writes and every filter.
-- This pass brings the rows already stored into the same spelling, so an old tag
-- still matches a new filter.
--
-- `lower(normalize(...))` deliberately mirrors what tags.py does — Python's
-- casefold() is the better caseless primitive, but it does NOT always agree with
-- SQL lower(), and the two sides must produce identical strings or this
-- migration silently splits the vocabulary it was written to merge.
--
-- DISTINCT because normalising can collide two tags of one row into one; without
-- it the row would carry a duplicate.
UPDATE memory
   SET tags = COALESCE((
         SELECT array_agg(DISTINCT lower(normalize(btrim(t), NFC)) ORDER BY lower(normalize(btrim(t), NFC)))
           FROM unnest(tags) AS t
          WHERE btrim(t) <> ''
       ), '{}')
 WHERE tags <> '{}'
   AND tags IS DISTINCT FROM COALESCE((
         SELECT array_agg(DISTINCT lower(normalize(btrim(t), NFC)) ORDER BY lower(normalize(btrim(t), NFC)))
           FROM unnest(tags) AS t
          WHERE btrim(t) <> ''
       ), '{}');
