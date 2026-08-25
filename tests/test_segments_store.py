"""Chunk-store round-trips against live backends (pgvector + qdrant).

Storage contract only: index_chunks / chunk_src_hash / replace-all / delete, plus
the `forget` cascade and per-namespace isolation. Ranking/snippet behaviour lives
in test_snippets and test_search_integration; here we assert what the backend
stores and scopes. Chunks are the semantic index now (no lazy read-cache).
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

psycopg = pytest.importorskip("psycopg")

from memgres.config import load  # noqa: E402
from memgres.embeddings import Embedder  # noqa: E402
from memgres.schema import migrate  # noqa: E402
from memgres.store import Store  # noqa: E402

DSN = os.environ.get("MEMGRES_TEST_DSN",
                     "postgresql://memgres:memgres@localhost:55432/memgres")
QURL = os.environ.get("MEMGRES_TEST_QDRANT", "http://localhost:56333")
COLL = "memgres_seg_test"


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


def _pg_reachable():
    try:
        psycopg.connect(DSN, connect_timeout=2).close()
        return True
    except Exception:
        return False


def _qdrant_reachable():
    if not _pg_reachable():
        return False
    try:
        import urllib.request
        urllib.request.urlopen(f"{QURL}/collections", timeout=2)
        return True
    except Exception:
        return False


def _fresh_pg():
    with psycopg.connect(DSN, autocommit=True) as c, c.cursor() as cur:
        cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")


def _base_env(monkeypatch):
    for k in list(os.environ):
        if k.startswith("MEMGRES_") or k == "QDRANT_URL":
            monkeypatch.delenv(k, raising=False)
    # Captions are not what most of this suite is about; the requirement
    # has its own file (test_require_title.py) covering both settings.
    monkeypatch.setenv("MEMGRES_REQUIRE_TITLE", "false")
    monkeypatch.setenv("MEMGRES_DATABASE_URL", DSN)
    monkeypatch.setenv("MEMGRES_FTS_LANGUAGE", "simple")
    monkeypatch.setenv("MEMGRES_EMBED_PROVIDER", "openai")   # shape only; stub injected
    monkeypatch.setenv("MEMGRES_EMBED_MODEL", "stub")
    monkeypatch.setenv("MEMGRES_EMBED_DIM", "3")
    monkeypatch.setenv("MEMGRES_EMBED_API_KEY", "x")


# ─── fixtures: one store per backend ─────────────────────────────────────────
@pytest.fixture
def pg_store(monkeypatch):
    if not _pg_reachable():
        pytest.skip("no test Postgres")
    _fresh_pg()
    _base_env(monkeypatch)   # pgvector is the default backend
    conn = psycopg.connect(DSN)
    migrate(conn, load())
    s = Store(load(), embedder=_Keyword(), conn=conn)
    yield s
    conn.close()


@pytest.fixture
def qdrant_store(monkeypatch):
    if not _qdrant_reachable():
        pytest.skip("no test Postgres or Qdrant")
    pytest.importorskip("qdrant_client")
    _fresh_pg()
    from qdrant_client import QdrantClient
    qc = QdrantClient(url=QURL)
    for coll in (COLL, f"{COLL}_segments"):
        if qc.collection_exists(coll):
            qc.delete_collection(coll)
    _base_env(monkeypatch)
    monkeypatch.setenv("MEMGRES_VECTOR_BACKEND", "qdrant")
    monkeypatch.setenv("QDRANT_URL", QURL)
    monkeypatch.setenv("MEMGRES_QDRANT_COLLECTION", COLL)
    conn = psycopg.connect(DSN)
    migrate(conn, load())
    s = Store(load(), embedder=_Keyword(), conn=conn)
    yield s
    conn.close()


# ─── helpers ─────────────────────────────────────────────────────────────────
def _ns(store, token=None):
    return store._authorize(token, need="read")[0]


def _chunk_spans(store, memory_id):
    """Backend-agnostic read-back of a memory's stored chunk spans as a set of
    (seq, start, end), for asserting replace-all / index_chunks fidelity."""
    if store.cfg.vector_backend == "qdrant":
        from qdrant_client.models import (FieldCondition, Filter, MatchValue)
        pts, _ = store._vectors.client.scroll(
            store._vectors.chunks,
            scroll_filter=Filter(must=[FieldCondition(
                key="memory_id", match=MatchValue(value=str(memory_id)))]),
            with_payload=True, limit=1000)
        return {(int(p.payload["seq"]), int(p.payload["seg_start"]),
                 int(p.payload["seg_end"])) for p in pts}
    with store._conn.cursor() as cur:
        cur.execute("SELECT seq, seg_start, seg_end FROM memory_segment "
                    "WHERE memory_id=%s", (memory_id,))
        return {(r[0], r[1], r[2]) for r in cur.fetchall()}


# Already-normalized unit vectors: qdrant normalizes under cosine, so both
# backends round-trip the spans exactly.
CHUNKS = [(0, 0, 5, [1.0, 0.0, 0.0]),
          (1, 3, 11, [0.0, 1.0, 0.0]),
          (2, 9, 14, [0.0, 0.0, 1.0])]


def _index_contract(store):
    v = store._vectors
    conn = store._conn
    ns = _ns(store)
    m = store.write(body="apple banana cherry body\n")   # writes chunks inline
    h = m.content_hash

    # a plain write already indexed the body's chunks under its content_hash
    assert v.chunk_src_hash(conn, m.id, ns) == h
    # wrong namespace never reads another tenant's chunks (defense-in-depth)
    assert v.chunk_src_hash(conn, m.id, "no-such-ns") is None

    # index_chunks replaces the whole set (explicit call, fixed spans)
    v.index_chunks(conn, m.id, ns, "hash-two", CHUNKS)
    assert _chunk_spans(store, m.id) == {(0, 0, 5), (1, 3, 11), (2, 9, 14)}
    assert v.chunk_src_hash(conn, m.id, ns) == "hash-two"

    # re-index with fewer chunks → old seqs gone (replace-all, not merge)
    v.index_chunks(conn, m.id, ns, "hash-three", [(0, 0, 4, [1.0, 0.0, 0.0])])
    assert _chunk_spans(store, m.id) == {(0, 0, 4)}
    assert v.chunk_src_hash(conn, m.id, ns) == "hash-three"

    # explicit delete drops them all
    v.delete_chunks(conn, m.id, ns)
    assert v.chunk_src_hash(conn, m.id, ns) is None
    assert _chunk_spans(store, m.id) == set()


def _forget_cascade(store):
    v = store._vectors
    conn = store._conn
    ns = _ns(store)
    m = store.write(body="apple pie to forget\n")
    assert v.chunk_src_hash(conn, m.id, ns) is not None
    store.forget(None, m.id)
    assert v.chunk_src_hash(conn, m.id, ns) is None
    assert _chunk_spans(store, m.id) == set()


# ─── pgvector ────────────────────────────────────────────────────────────────
def test_pg_index_contract(pg_store):
    _index_contract(pg_store)


def test_pg_forget_cascade(pg_store):
    _forget_cascade(pg_store)


def test_pg_chunks_carry_namespace(pg_store):
    v = pg_store._vectors
    conn = pg_store._conn
    ns = _ns(pg_store)
    a = pg_store.write(body="apple a\n")
    b = pg_store.write(body="banana b\n")
    with conn.cursor() as cur:
        cur.execute("SELECT DISTINCT namespace FROM memory_segment WHERE memory_id=%s",
                    (a.id,))
        assert {r[0] for r in cur.fetchall()} == {ns}
    # a foreign namespace never reads a's chunks
    assert v.chunk_src_hash(conn, a.id, "elsewhere") is None
    assert v.chunk_src_hash(conn, b.id, ns) == b.content_hash


# ─── qdrant ──────────────────────────────────────────────────────────────────
def test_qdrant_index_contract(qdrant_store):
    _index_contract(qdrant_store)


def test_qdrant_forget_cascade(qdrant_store):
    _forget_cascade(qdrant_store)


def test_qdrant_chunk_collection_and_indexes(qdrant_store):
    from qdrant_client import QdrantClient
    qc = QdrantClient(url=QURL)
    chunks_coll = f"{COLL}_segments"
    assert qc.collection_exists(chunks_coll)
    schema = qc.get_collection(chunks_coll).payload_schema or {}
    assert "memory_id" in schema and "namespace" in schema


def test_qdrant_chunks_carry_namespace(qdrant_store):
    v = qdrant_store._vectors
    conn = qdrant_store._conn
    ns = _ns(qdrant_store)
    a = qdrant_store.write(body="apple a\n")
    qdrant_store.write(body="banana b\n")          # a second memory, for realism
    # namespace lives in each chunk point's payload
    from qdrant_client import QdrantClient
    from qdrant_client.models import FieldCondition, Filter, MatchValue
    qc = QdrantClient(url=QURL)
    pts, _ = qc.scroll(
        f"{COLL}_segments",
        scroll_filter=Filter(must=[FieldCondition(key="memory_id",
                                                  match=MatchValue(value=str(a.id)))]),
        with_payload=True, limit=100)
    assert pts and all(p.payload["namespace"] == ns for p in pts)
    assert v.chunk_src_hash(conn, a.id, "elsewhere") is None


def test_qdrant_two_namespaces_isolated(qdrant_store, monkeypatch):
    """Open mode: two tokens → two namespaces. A memory's chunks are keyed to its
    namespace and never surface under the other's."""
    from memgres.identity import new_token
    monkeypatch.setenv("MEMGRES_KEY_MODE", "open")
    cfg = load()
    conn = qdrant_store._conn
    migrate(conn, cfg)
    s = Store(cfg, embedder=_Keyword(), conn=conn)
    from memgres import identity as ident

    tok_a, tok_b = new_token(), new_token()
    for t in (tok_a, tok_b):     # open mode: each token asks for its own space
        ident.create_own_namespace(conn, ident.resolve(conn, cfg, t), "mine")
    a = s.write(tok_a, body="apple in A\n")
    b = s.write(tok_b, body="banana in B\n")
    ns_a = s._authorize(tok_a, need="read")[0]
    ns_b = s._authorize(tok_b, need="read")[0]
    assert ns_a != ns_b
    v = s._vectors
    assert v.chunk_src_hash(conn, a.id, ns_a) == a.content_hash
    assert v.chunk_src_hash(conn, b.id, ns_b) == b.content_hash
    # A's chunks are invisible under B's namespace even with A's memory_id
    assert v.chunk_src_hash(conn, a.id, ns_b) is None
    # forget A drops A's chunks; B's remain
    s.forget(tok_a, a.id)
    assert v.chunk_src_hash(conn, a.id, ns_a) is None
    assert v.chunk_src_hash(conn, b.id, ns_b) is not None


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
