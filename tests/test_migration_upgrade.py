"""The schema-v6 upgrade path (0005): flag existing rows for re-chunking exactly
once, on EITHER backend, and drop the old pgvector doc-vector column.

Fresh installs never need the backfill; this exercises the upgrade of an existing
deployment. The trigger is the stored ``schema_version < 6`` (backend-agnostic) —
NOT the pgvector ``embedding`` column, because a qdrant deployment never had that
column and a column-based guard would silently skip the backfill there, leaving
semantic recall empty. So both cases are covered: with the column (pgvector) and
without it (qdrant). Everything runs autocommit, so nothing can leave a lock.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

psycopg = pytest.importorskip("psycopg")

DSN = os.environ.get("MEMGRES_TEST_DSN",
                     "postgresql://memgres:memgres@localhost:55432/memgres")
SCHEMA = "memgres_upgrade_test"
MIG_0005 = (Path(__file__).resolve().parent.parent
            / "memgres" / "migrations" / "0005_chunk_index.sql").read_text()


def _reachable():
    try:
        psycopg.connect(DSN, connect_timeout=2).close()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _reachable(), reason="no test Postgres")


def _make_pre05(conn, *, stored_version, with_embedding_col):
    """Build a pre-0005 `memory` table + a `memgres_meta` stamped at
    ``stored_version`` (None = no row yet, i.e. a fresh install). ``memory`` gets
    the pgvector `embedding` column only when ``with_embedding_col`` (a qdrant
    deployment wouldn't have it). Three rows: non-empty, empty, NULL body."""
    emb = "embedding text," if with_embedding_col else ""
    with conn.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        cur.execute(f"CREATE SCHEMA {SCHEMA}")
        cur.execute(f"SET search_path TO {SCHEMA}")
        cur.execute("CREATE TABLE memgres_meta ("
                    "only_row boolean PRIMARY KEY DEFAULT true, schema_version int)")
        if stored_version is not None:
            cur.execute("INSERT INTO memgres_meta (only_row, schema_version) "
                        "VALUES (true, %s)", (stored_version,))
        cur.execute(f"""CREATE TABLE memory (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            body text, {emb} updated_at timestamptz DEFAULT now())""")
        cur.execute("INSERT INTO memory (body) VALUES ('has a body'), (''), (NULL)")


@pytest.fixture
def conn():
    c = psycopg.connect(DSN, autocommit=True)
    yield c
    with c.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
    c.close()


def _cols(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema=%s AND table_name='memory'", (SCHEMA,))
        return {r[0] for r in cur.fetchall()}


def _flagged_bodies(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT body FROM memory WHERE embed_pending")
        return [r[0] for r in cur.fetchall()]


def test_pgvector_upgrade_flags_and_drops_column(conn):
    _make_pre05(conn, stored_version=4, with_embedding_col=True)
    assert "embedding" in _cols(conn)
    with conn.cursor() as cur:
        cur.execute(MIG_0005)
    cols = _cols(conn)
    assert "embedding" not in cols and "embed_pending" in cols   # column retired
    assert _flagged_bodies(conn) == ["has a body"]               # only bodied row


def test_qdrant_upgrade_still_flags_without_embedding_column(conn):
    # The regression: a qdrant deployment has no `embedding` column, yet the
    # backfill MUST still fire (version-driven), or semantic recall goes empty.
    _make_pre05(conn, stored_version=4, with_embedding_col=False)
    assert "embedding" not in _cols(conn)
    with conn.cursor() as cur:
        cur.execute(MIG_0005)
    assert _flagged_bodies(conn) == ["has a body"]


def test_idempotent_when_already_v6(conn):
    _make_pre05(conn, stored_version=6, with_embedding_col=False)
    with conn.cursor() as cur:
        cur.execute(MIG_0005)
    assert _flagged_bodies(conn) == []          # already chunked → no re-flag


def test_fresh_install_flags_nothing(conn):
    _make_pre05(conn, stored_version=None, with_embedding_col=False)  # no meta row
    with conn.cursor() as cur:
        cur.execute(MIG_0005)
    assert _flagged_bodies(conn) == []


def _fresh_full_schema(conn):
    """Build a full memgres schema in the isolated test schema (tree + embed off →
    no ltree/vector extensions needed) and return the cfg used."""
    from dataclasses import replace

    from memgres.config import load
    from memgres.schema import migrate

    with conn.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        cur.execute(f"CREATE SCHEMA {SCHEMA}")
        cur.execute(f"SET search_path TO {SCHEMA}")
    conn.autocommit = False
    cfg = replace(load(), tree_enabled=False, embed_provider="none")
    migrate(conn, cfg)
    return cfg


def test_client_below_floor_refuses(conn):
    # The database's compatibility floor is raised past this build (as if a newer
    # memgres migrated the shared DB past a breaking change). migrate() must refuse
    # with an actionable "update this client" error, not proceed.
    from memgres.schema import SCHEMA_VERSION, SchemaMismatch, migrate

    cfg = _fresh_full_schema(conn)
    with conn.cursor() as cur:
        cur.execute("UPDATE memgres_meta SET min_reader_version=%s WHERE only_row",
                    (SCHEMA_VERSION + 1,))
    conn.commit()
    with pytest.raises(SchemaMismatch, match="Update this client"):
        migrate(conn, cfg)
    conn.rollback()
    conn.autocommit = True


def test_additive_newer_db_is_allowed_and_not_downgraded(conn):
    # A database at a HIGHER schema_version but whose floor this client still
    # satisfies (additive changes) must operate — and the newer stamp must NOT be
    # dropped back to this build's version (GREATEST keeps it).
    from memgres.schema import SCHEMA_VERSION, migrate

    cfg = _fresh_full_schema(conn)
    ahead = SCHEMA_VERSION + 5
    with conn.cursor() as cur:
        cur.execute("UPDATE memgres_meta SET schema_version=%s WHERE only_row",
                    (ahead,))                        # newer, but floor stays <= us
    conn.commit()
    migrate(conn, cfg)                               # no refusal
    with conn.cursor() as cur:
        cur.execute("SELECT schema_version FROM memgres_meta WHERE only_row")
        assert cur.fetchone()[0] == ahead            # not downgraded
    conn.rollback()
    conn.autocommit = True
