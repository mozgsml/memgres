-- What was searched for, and what the caller opened next.
--
-- A deployment cannot tune retrieval it has never measured, and the queries
-- people and agents actually ask are the one thing no corpus can supply by
-- itself. This table records them, so `memgres-eval` can build its cases from
-- real traffic instead of only from titles and literals.
--
-- Two kinds of row: a 'recall' carries the query and the ranked ids it
-- returned; a 'get' carries the memory that was opened. Joining a get to the
-- recall shortly before it is what tells you WHICH result answered the query.
--
-- Deliberately off by default (MEMGRES_SEARCH_LOG) and swept after
-- MEMGRES_SEARCH_LOG_DAYS: a query is content, and often says more about the
-- asker than the memory does.
--
-- No foreign keys, for the same reason `memory_usage` has none: a log is an
-- observation of what happened, and deleting a memory must not rewrite history
-- or fail on a reference.
CREATE TABLE IF NOT EXISTS search_log (
    id          bigserial PRIMARY KEY,
    at          timestamptz NOT NULL DEFAULT now(),
    kind        text        NOT NULL,   -- 'recall' | 'get'
    source      text,                   -- mcp | web | rest | lib: which door
    user_id     uuid,
    token_id    uuid,
    namespaces  text[],
    query       text,                   -- recall only
    mode        text,                   -- the mode it RESOLVED to, not 'auto'
    k           integer,
    results     uuid[],                 -- recall: the ranked ids returned
    memory_id   uuid,                   -- get: what was opened
    ms          integer
);

CREATE INDEX IF NOT EXISTS search_log_at ON search_log (at DESC);
-- the join a log analysis makes: this caller's rows, in order
CREATE INDEX IF NOT EXISTS search_log_caller
    ON search_log (user_id, token_id, at DESC);
