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

SCHEMA_VERSION = 4

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


def _sql(name: str) -> str:
    return (MIGRATIONS_DIR / name).read_text(encoding="utf-8")


def migrate(conn, cfg: Config) -> None:
    """Bring the database at `conn` to the schema `cfg` describes.

    `conn` is a psycopg connection. Runs in one transaction; raises
    :class:`SchemaMismatch` (and rolls back) on an incompatible existing stamp.
    """
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(_sql("0001_core.sql"))
            # identity tables (always applied, idempotent; empty in single mode)
            cur.execute(_sql("0002_identity.sql"))
            # author columns on history (always applied, idempotent; NULL in
            # single mode). Folded into the hash chain only when present, so
            # pre-upgrade chains stay verifiable — see store._row_hash.
            cur.execute(_sql("0003_history_author.sql"))
            _apply_tree(cur, cfg)
            _apply_vector(cur, cfg)
            _stamp(cur, cfg)


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
    cur.execute(
        f"ALTER TABLE memory ADD COLUMN IF NOT EXISTS embedding vector({cfg.embed_dim})"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS memory_embedding_hnsw ON memory "
        "USING hnsw (embedding vector_cosine_ops)"
    )
    # Per-memory segment vectors: a durable cache the (future) snippet flow fills
    # lazily, keyed by the memory's content_hash (`src_hash`) so a body edit — a
    # new hash — invalidates the cache and `forget` cascades them away. Offsets,
    # not text: the snippet is sliced from the live body. A memory has few
    # segments, ranked by a plain scan, so no HNSW here.
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
