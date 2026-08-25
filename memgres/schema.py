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

# The version this build migrates the database TO (the latest migration it carries).
SCHEMA_VERSION = 16

# The compatibility FLOOR: the schema version of the most recent backward-
# INCOMPATIBLE migration — one that changed the shape/semantics old code relied on
# (a dropped column, a changed ranking model), so a client older than this can no
# longer read the data correctly. A client operating on a database records this
# into `memgres_meta.min_reader_version`; any client whose SCHEMA_VERSION is below a
# database's stored floor refuses to run (see `_guard_client_version`).
#
# BUMP THIS to a new migration's version ONLY when that migration breaks old
# readers. An additive migration (new column/table/index that old code ignores)
# must leave it as-is, so older clients keep working against the newer schema.
#   v6 (0005): dropped the whole-body doc vector, moved ranking to chunks → BREAKING.
#   v7 (0006): added min_reader_version column → additive, floor stays 6.
#   v8 (0007): added embed_attempts/embed_failed_at → additive, floor stays 6.
#   v9 (0008): added app_user.role service role → additive, floor stays 6.
#   v10 (0009): added app_user.can_create_namespace → additive, floor stays 6.
#     Older clients ignore the column and keep creating namespaces freely, which
#     is exactly what they did before; the restriction is enforced by new code.
#   v11 (0010): added the namespace_alias table → additive, floor stays 6.
#   v12 (0011): DROPPED app_user.default_namespace_id → BREAKING. Every 0.5.x
#     read path selects that column and fails outright without it.
#   v13 (0012): added the user profile columns → additive.
#   v14 (0013): added memory_history.hash_version → additive in SQL, BREAKING in
#     MEANING: it changed what a stored `row_hash` says. A pre-0013 client
#     recomputes every row with the v1 recipe and reports an untampered v2 chain
#     as TAMPERED — a wrong answer from the one function whose entire job is to
#     be trusted, and a silent one. Writing stays compatible in both directions
#     (an old client writes v1 rows, which the column defaults to), so this is
#     the floor purely because of what verification would claim.
#   v15 (0014): dropped a foreign key → nothing old code reads changes.
#   v16 (0015): normalised stored tags to one spelling (NFC + lower) → BREAKING,
#     and the reasoning is the same as v14's. Nothing about the SHAPE changes; a
#     pre-0015 client neither normalises nor knows to, so its filter for `X402`
#     stops matching a row now stored as `x402` and it gets an empty result where
#     rows exist. Silence shaped like an answer, from a filter that looks fine.
SCHEMA_BREAKING_VERSION = 16

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


def _column_exists(cur, table: str, column: str) -> bool:
    cur.execute(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = %s AND column_name = %s",
        (table, column))
    return cur.fetchone() is not None


def _guard_client_version(cur) -> None:
    """Refuse to run a client OLDER than the database's compatibility floor —
    checked FIRST, before any migration touches the schema.

    The database's operable range is ``[min_reader_version, ∞)`` in terms of client
    ``SCHEMA_VERSION``: a client at or above the stored floor may operate (and, if
    newer, migrates the shared store forward), but a client BELOW it cannot — a
    backward-incompatible migration changed the data into a shape this build can't
    read. Since state is shared across machines, this is what a stale machine hits
    after another upgrades past a breaking change: it stops with a clear ask to
    update, instead of silently misreading the newer schema.

    A newer-but-ADDITIVE database (higher ``schema_version`` but a floor this client
    still satisfies) passes through and operates normally — that's the point of a
    range rather than exact-match. A fresh database (no ``memgres_meta`` yet), or a
    pre-floor one (no ``min_reader_version`` column → floor 1), imposes no bar."""
    cur.execute("SELECT to_regclass('memgres_meta')")
    if cur.fetchone()[0] is None:
        return                       # brand-new database — nothing to compare
    if not _column_exists(cur, "memgres_meta", "min_reader_version"):
        return                       # pre-v7 database — no floor recorded yet (== 1)
    cur.execute("SELECT min_reader_version, schema_version "
                "FROM memgres_meta WHERE only_row")
    row = cur.fetchone()
    if row is None:
        return
    floor, stored = row
    if floor is not None and floor > SCHEMA_VERSION:
        raise SchemaMismatch(
            f"this memgres speaks schema v{SCHEMA_VERSION}, but the database "
            f"(at v{stored}) requires a client of at least v{floor} — it was "
            f"migrated past a backward-incompatible change by a newer memgres. "
            f"Update this client (in its venv: pip install -U "
            f"'memgres[local,qdrant,mcp]', or pull the repo for an editable "
            f"install) and restart it. Refusing to run below the database's "
            f"compatibility floor."
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
            "(only_row, schema_version, min_reader_version, embed_provider, "
            " embed_model, embed_dim, fts_language, tree_enabled) "
            "VALUES (true, %s, %s, %s, %s, %s, %s, %s)",
            (SCHEMA_VERSION, SCHEMA_BREAKING_VERSION, cfg.embed_provider,
             cfg.embed_model, cfg.embed_dim, cfg.fts_language, cfg.tree_enabled),
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

    # Advance the recorded versions to what this migrate() applied — with GREATEST
    # so an older client operating on a newer-but-additive database never DROPS the
    # stamp: schema_version tracks the newest schema present, min_reader_version the
    # highest breaking floor any client has imposed. Both are monotonic.
    cur.execute(
        "UPDATE memgres_meta SET "
        "schema_version = GREATEST(schema_version, %s), "
        "min_reader_version = GREATEST(min_reader_version, %s) "
        "WHERE only_row",
        (SCHEMA_VERSION, SCHEMA_BREAKING_VERSION))
