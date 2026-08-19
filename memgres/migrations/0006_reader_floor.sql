-- memgres schema v7: record the compatibility FLOOR in the meta row.
--
-- `schema_version` already records the version the data is AT. `min_reader_version`
-- records the lowest client SCHEMA_VERSION allowed to operate against this data —
-- the "from" of the compatible range (the "to" is open-ended: a newer client
-- migrates the shared store forward).
--
-- It is raised ONLY by a backward-INCOMPATIBLE migration (see SCHEMA_BREAKING_VERSION
-- in schema.py). An additive change (new column/table/index) leaves it alone, so an
-- older client keeps working against a newer-but-additive schema. Default 1 = "any
-- reader" for rows that predate this column; _stamp then raises it to the current
-- breaking floor at the end of this same migrate().

ALTER TABLE memgres_meta
    ADD COLUMN IF NOT EXISTS min_reader_version integer NOT NULL DEFAULT 1;
