"""Qdrant vector backend against a live Qdrant + Postgres.

Skips unless both a test Postgres and QDRANT_URL are reachable.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

psycopg = pytest.importorskip("psycopg")
pytest.importorskip("qdrant_client")

from memgres.config import load  # noqa: E402
from memgres.schema import migrate  # noqa: E402
from memgres.store import Store  # noqa: E402
from memgres.embeddings import Embedder  # noqa: E402

DSN = os.environ.get("MEMGRES_TEST_DSN",
                     "postgresql://memgres:memgres@localhost:55432/memgres")
QURL = os.environ.get("MEMGRES_TEST_QDRANT", "http://localhost:56333")
COLL = "memgres_test"


def _reachable() -> bool:
    try:
        psycopg.connect(DSN, connect_timeout=2).close()
    except Exception:
        return False
    try:
        import urllib.request
        urllib.request.urlopen(f"{QURL}/collections", timeout=2)
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _reachable(), reason="no test Postgres or Qdrant")


class _Keyword(Embedder):
    dim = 3

    def _vec(self, t):
        t = t.lower()
        v = [float(t.count("apple")), float(t.count("banana")), float(t.count("cherry"))]
        n = sum(x * x for x in v) ** 0.5 or 1.0
        return [x / n for x in v]

    def embed_documents(self, texts):
        return [self._vec(t) for t in texts]

    def embed_query(self, t):
        return self._vec(t)


@pytest.fixture
def store(monkeypatch):
    # fresh PG schema + fresh Qdrant collection
    with psycopg.connect(DSN, autocommit=True) as c, c.cursor() as cur:
        cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    from qdrant_client import QdrantClient
    qc = QdrantClient(url=QURL)
    if qc.collection_exists(COLL):
        qc.delete_collection(COLL)

    for k in list(os.environ):
        if k.startswith("MEMGRES_") or k == "QDRANT_URL":
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("MEMGRES_DATABASE_URL", DSN)
    monkeypatch.setenv("MEMGRES_FTS_LANGUAGE", "simple")
    monkeypatch.setenv("MEMGRES_VECTOR_BACKEND", "qdrant")
    monkeypatch.setenv("MEMGRES_EMBED_PROVIDER", "openai")   # shape only; stub injected
    monkeypatch.setenv("MEMGRES_EMBED_MODEL", "stub")
    monkeypatch.setenv("MEMGRES_EMBED_DIM", "3")
    monkeypatch.setenv("MEMGRES_EMBED_API_KEY", "x")
    monkeypatch.setenv("QDRANT_URL", QURL)
    monkeypatch.setenv("MEMGRES_QDRANT_COLLECTION", COLL)

    conn = psycopg.connect(DSN)
    migrate(conn, load())
    s = Store(load(), embedder=_Keyword(), conn=conn)
    yield s
    conn.close()


def test_no_pgvector_column_when_qdrant(store):
    # vectors live in Qdrant, so the memory table has no embedding column
    with store._conn.cursor() as cur:
        cur.execute("SELECT column_name FROM information_schema.columns "
                    "WHERE table_name='memory'")
        cols = {r[0] for r in cur.fetchall()}
    assert "embedding" not in cols


def test_namespace_payload_index_created(store):
    # tenant-scoped ANN search filters on `namespace`; a keyword payload index
    # keeps that filter fast as the collection grows (isolation holds either way)
    from qdrant_client import QdrantClient
    qc = QdrantClient(url=QURL)
    schema = qc.get_collection(COLL).payload_schema or {}
    assert "namespace" in schema
    assert str(schema["namespace"].data_type).lower().endswith("keyword")


def test_semantic_recall_via_qdrant(store):
    store.write(body="I love apple pie\n", path="food.apple", tags=["fruit"])
    store.write(body="banana bread recipe\n", path="food.banana", tags=["fruit"])
    store.write(body="cherry orchard notes\n", path="misc.cherry", tags=["tree"])

    top = store.recall(None, "apple apple", mode="semantic", k=1)
    assert len(top) == 1 and "apple" in top[0].snippet

    # tag filter (enforced in PG after Qdrant ranks)
    fruit = store.recall(None, "cherry", mode="semantic", tags=["fruit"])
    assert all("tree" not in h.tags for h in fruit)

    # subtree filter
    sub = store.recall(None, "banana", mode="semantic", path_prefix="food")
    assert all(h.path.startswith("food") for h in sub)


def test_forget_removes_qdrant_point(store):
    m = store.write(body="apple to delete\n")
    assert store.recall(None, "apple", mode="semantic")           # present
    store.forget(None, m.id)
    assert store.recall(None, "apple", mode="semantic") == []     # gone from Qdrant too


def test_edit_reembeds(store):
    m = store.write(body="apple apple apple\n")                   # strongly 'apple'
    # rewrite to cherry; the stored vector must move (re-embedded on the edit)
    store.write(id=m.id, body="cherry cherry cherry\n", base_hash=m.content_hash)
    cherry = store.recall(None, "cherry", mode="semantic", k=1)
    assert cherry[0].id == m.id and cherry[0].score > 0.9        # now a cherry vector
    apple = store.recall(None, "apple", mode="semantic", k=1)
    assert apple[0].score < 0.1                                  # no longer apple-like


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
