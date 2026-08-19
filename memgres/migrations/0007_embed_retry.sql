-- memgres schema v8: bounded retry / dead-letter for the embed queue.
--
-- The worker claims a pending row, embeds it, and clears the flag in one
-- transaction. If embedding a specific row keeps failing (a poison body, a
-- provider rejection, an oversized batch), it must NOT wedge the queue behind it
-- — every newer row would starve. So a failed attempt is recorded here: the
-- claim skips a row during a back-off window and stops claiming it once it has
-- failed too many times (a dead letter, left flagged but out of the rotation and
-- logged). A successful embed resets both, so a later legitimate edit re-embeds
-- cleanly.

ALTER TABLE memory ADD COLUMN IF NOT EXISTS embed_attempts  integer NOT NULL DEFAULT 0;
ALTER TABLE memory ADD COLUMN IF NOT EXISTS embed_failed_at timestamptz;
