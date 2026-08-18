"""The schema-v6 upgrade path (0005): drop the old whole-body doc vector and flag
existing rows for re-chunking — exactly once.

Fresh installs never have the `memory.embedding` column, so this exercises the
one path they don't: an existing table that DOES have it (a pre-0.4 deployment).
The 0005 migration must flag only rows with a non-empty body, drop the column,
and — crucially — NOT re-flag anything when migrate() runs again.

0005 only checks the column's *existence* and drops it, so the fixture models it
with a plain `text` column — no pgvector extension needed (and no CASCADE that
would drop it). Everything runs autocommit, so nothing can leave a lock.
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


@pytest.fixture
def pre05():
    """An autocommit connection with search_path on an isolated schema holding a
    pre-0005 `memory` table: the old `embedding` column (plain text is enough —
    0005 only tests existence + drops), no `embed_pending`, three rows (non-empty
    body, empty body, NULL body)."""
    conn = psycopg.connect(DSN, autocommit=True)
    with conn.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        cur.execute(f"CREATE SCHEMA {SCHEMA}")
        cur.execute(f"SET search_path TO {SCHEMA}")
        cur.execute("""CREATE TABLE memory (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            body text, embedding text, updated_at timestamptz DEFAULT now())""")
        cur.execute("INSERT INTO memory (body, embedding) VALUES "
                    "('has a body', 'vec'), ('', NULL), (NULL, NULL)")
    yield conn
    with conn.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
    conn.close()


def _cols(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema=%s AND table_name='memory'", (SCHEMA,))
        return {r[0] for r in cur.fetchall()}


def _flagged_bodies(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT body FROM memory WHERE embed_pending")
        return [r[0] for r in cur.fetchall()]


def test_0005_backfills_then_drops_embedding(pre05):
    assert "embedding" in _cols(pre05)
    with pre05.cursor() as cur:
        cur.execute(MIG_0005)
    cols = _cols(pre05)
    assert "embedding" not in cols and "embed_pending" in cols
    # only the non-empty-body row was flagged for re-chunking
    assert _flagged_bodies(pre05) == ["has a body"]


def test_0005_is_idempotent_no_reflag(pre05):
    with pre05.cursor() as cur:
        cur.execute(MIG_0005)                                 # first upgrade
        cur.execute("UPDATE memory SET embed_pending=false")  # pretend all drained
        cur.execute(MIG_0005)                                 # re-run: guard skips backfill
    assert _flagged_bodies(pre05) == []                       # nothing re-flagged
