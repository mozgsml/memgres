"""Apply the schema and reconcile it with the running config.

`migrate(conn, cfg)` is idempotent: it applies the core SQL, then adds the
config-dependent pieces (ltree tree, pgvector column) only when they're turned
on, and finally *stamps* the collection with the embedding model/dim + FTS dict
it was built with.

The stamp is the safety rail. Indexing with one embedding model and querying
with another returns meaningless results with no error — a silent-failure class
we refuse to allow. On a mismatch `migrate` raises :class:`SchemaMismatch`
instead of quietly corrupting recall.
"""

from __future__ import annotations

from pathlib import Path

from .config import Config

SCHEMA_VERSION = 6

# Dev layout: repo/migrations next to the package. When packaged, migrations are
# shipped inside the package (see pyproject) and this still resolves.
_HERE = Path(__file__).resolve().parent
for _cand in (_HERE / "migrations", _HERE.parent / "migrations"):
    if _cand.is_dir():
        MIGRATIONS_DIR = _cand
        break
else:  # pragma: no cover - packaging guarantees one of the above
    MIGRATIONS_DIR = _HERE.parent / "migrations"


class SchemaMismatch(RuntimeError):
    """The DB was built with settings incompatible with the current config."""


def migrate(conn, cfg: Config) -> None:
    """Bring the database at `conn` to the schema `cfg` describes.

    `conn` is a psycopg connection. Runs in one transaction; raises
    :class:`SchemaMismatch` (and rolls back) on an incompatible existing stamp.

    The numbered ``NNNN_*.sql`` files in ``migrations/`` are applied in filename
    order — each is idempotent (``IF NOT EXISTS`` / guarded data steps), so a
    re-run is a no-op and adding a migration is just dropping a new file. The
    config-dependent pieces (tree, vector index, model/FTS stamp) run after, in
    Python, because their DDL depends on the live config (dimension, dictionary).
    """
    with conn.transaction():
        with conn.cursor() as cur:
            _guard_client_version(cur)
            for path in sorted(MIGRATIONS_DIR.glob("[0-9]*.sql")):
                cur.execute(path.read_text(encoding="utf-8"))
            _apply_tree(cur, cfg)
            _apply_vector(cur, cfg)
            _stamp(cur, cfg)


def _guard_client_version(cur) -> None:
    """Refuse to run an OUTDATED client against a NEWER database — checked FIRST,
    before any migration touches the schema.

    Migrations are forward-only: this build carries migrations up to
    ``SCHEMA_VERSION``. If the database is stamped at a HIGHER version, a newer
    memgres already migrated it into a shape this one doesn't understand (dropped
    columns, new tables, changed semantics) — proceeding would misread or corrupt
    it. Since state is shared across machines, this is exactly what happens when
    one machine upgrades and another still runs the old client: the old one stops
    with a clear ask to update, instead of silently breaking.

    A fresh database (no ``memgres_meta`` yet) or one at/below this version passes
    through — the normal forward migrate then brings it up and re-stamps."""
    cur.execute("SELECT to_regclass('memgres_meta')")
    if cur.fetchone()[0] is None:
        return                       # brand-new database — nothing to compare
    cur.execute("SELECT schema_version FROM memgres_meta WHERE only_row")
    row = cur.fetchone()
    stored = row[0] if row else None
    if stored is not None and stored > SCHEMA_VERSION:
        raise SchemaMismatch(
            f"this memgres speaks schema v{SCHEMA_VERSION}, but the database is at "
            f"v{stored} — it was migrated by a NEWER memgres. Update this client "
            f"(in its venv: pip install -U 'memgres[local,qdrant,mcp]', or pull the "
            f"repo for an editable install) and restart it. Refusing to run an "
            f"outdated client against a newer schema."
        )


def _apply_tree(cur, cfg: Config) -> None:
    if not cfg.tree_enabled:
        return
    cur.execute("CREATE EXTENSION IF NOT EXISTS ltree")
    # Add the path column + its indexes only once; ALTER … ADD COLUMN IF NOT
    # EXISTS keeps this idempotent across restarts.
    cur.execute("ALTER TABLE memory ADD COLUMN IF NOT EXISTS path ltree")
    cur.execute(
        "CREATE INDEX IF NOT EXISTS memory_path_gist ON memory "
        "USING gist (path) WHERE path IS NOT NULL"
    )
    # A path is a node's address: unique within a namespace when present.
    cur.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS memory_ns_path_uniq ON memory "
        "(namespace, path) WHERE path IS NOT NULL"
    )


def _apply_vector(cur, cfg: Config) -> None:
    if cfg.embed_provider == "none":
        return
    if cfg.vector_backend != "pgvector":
        return  # qdrant holds vectors out-of-band; nothing to add here
    if cfg.embed_dim <= 0:
        # config.validate() already guards this; belt and suspenders.
        raise SchemaMismatch("pgvector semantic search needs MEMGRES_EMBED_DIM > 0")
    cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
    # Chunk vectors ARE the semantic index (no whole-body doc vector). Each row is
    # one chunk of one memory: its offset span, its vector, the memory's
    # content_hash (`src_hash`) so a body edit — a new hash — replaces the set, and
    # its namespace so ranking never crosses tenants. `forget` cascades them away.
    # Offsets, not text: the snippet is sliced from the live body.
    cur.execute(
        f"""CREATE TABLE IF NOT EXISTS memory_segment (
                memory_id uuid NOT NULL REFERENCES memory(id) ON DELETE CASCADE,
                seq       int  NOT NULL,
                seg_start int  NOT NULL,
                seg_end   int  NOT NULL,
                embedding vector({cfg.embed_dim}) NOT NULL,
                src_hash  text NOT NULL,
                namespace text NOT NULL,
                PRIMARY KEY (memory_id, seq)
            )"""
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS memory_segment_mid "
        "ON memory_segment (memory_id)"
    )
    # HNSW over the chunk vectors — this is the global ANN ranking index now, not a
    # per-memory scan, so it needs one. Plus a btree on namespace for the tenant
    # filter that rides along every recall.
    cur.execute(
        "CREATE INDEX IF NOT EXISTS memory_segment_hnsw ON memory_segment "
        "USING hnsw (embedding vector_cosine_ops)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS memory_segment_ns "
        "ON memory_segment (namespace)"
    )


def _stamp(cur, cfg: Config) -> None:
    """Insert the meta row, or verify the existing one and hard-fail on drift."""
    cur.execute(
        "SELECT embed_provider, embed_model, embed_dim, fts_language, tree_enabled "
        "FROM memgres_meta WHERE only_row"
    )
    row = cur.fetchone()
    if row is None:
        cur.execute(
            "INSERT INTO memgres_meta "
            "(only_row, schema_version, embed_provider, embed_model, embed_dim, "
            " fts_language, tree_enabled) VALUES (true, %s, %s, %s, %s, %s, %s)",
            (SCHEMA_VERSION, cfg.embed_provider, cfg.embed_model, cfg.embed_dim,
             cfg.fts_language, cfg.tree_enabled),
        )
        return

    provider, model, dim, fts_lang, tree = row

    if fts_lang != cfg.fts_language:
        raise SchemaMismatch(
            f"FTS dictionary changed: collection built with '{fts_lang}', config "
            f"says '{cfg.fts_language}'. The stored tsvectors were computed with the "
            f"old dictionary; recompute them (reindex) before switching."
        )

    # Embeddings: going none → enabled is fine (adopt the new stamp). But changing
    # an *existing* model or dimension silently invalidates every stored vector.
    had_vectors = dim > 0
    if had_vectors and (model != cfg.embed_model or dim != cfg.embed_dim):
        raise SchemaMismatch(
            f"embedding model/dim changed: collection built with "
            f"'{model}' (dim {dim}), config says '{cfg.embed_model}' (dim "
            f"{cfg.embed_dim}). Existing vectors are meaningless under the new "
            f"model — re-embed the corpus, don't mix models."
        )

    if not had_vectors and cfg.embed_provider != "none":
        cur.execute(
            "UPDATE memgres_meta SET embed_provider=%s, embed_model=%s, "
            "embed_dim=%s, updated_at=now() WHERE only_row",
            (cfg.embed_provider, cfg.embed_model, cfg.embed_dim),
        )

    # Keep the recorded layout version current, so the meta row (and any drift
    # check built on it) reflects the migrations actually applied, not just the
    # version first stamped.
    cur.execute("UPDATE memgres_meta SET schema_version=%s WHERE only_row "
                "AND schema_version <> %s", (SCHEMA_VERSION, SCHEMA_VERSION))
