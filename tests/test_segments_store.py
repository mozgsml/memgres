"""Segment-storage round-trips against live backends (pgvector + qdrant).

Storage only: upsert / get / stale-invalidate / replace / delete, plus the
`forget` cascade and per-namespace isolation. No recall/snippet flow here.
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
    return store._authorize(token, need="read")


def _approx(a, b, eps=1e-5):
    return len(a) == len(b) and all(abs(x - y) < eps for x, y in zip(a, b))


def _seg_set(rows):
    """Compare (seq, start, end) sets ignoring vector float noise."""
    return {(r[0], r[1], r[2]) for r in rows}


# Unit vectors: qdrant normalizes stored vectors under cosine distance, so
# using already-normalized vectors lets both backends round-trip them exactly.
SEGS = [(0, 0, 5, [1.0, 0.0, 0.0]),
        (1, 3, 11, [0.0, 1.0, 0.0]),
        (2, 9, 14, [0.0, 0.0, 1.0])]


def _roundtrip(store):
    v = store._vectors
    conn = store._conn
    ns = _ns(store)
    m = store.write(body="apple banana cherry body\n")
    h = m.content_hash

    # round-trip
    v.upsert_segments(conn, m.id, ns, h, SEGS)
    got = v.get_segments(conn, m.id, ns, h)
    assert got is not None
    assert _seg_set(got) == _seg_set(SEGS)
    by_seq = {r[0]: r for r in got}
    for seq, s, e, vec in SEGS:
        assert by_seq[seq][1] == s and by_seq[seq][2] == e
        assert _approx(by_seq[seq][3], vec)

    # stale invalidation: a different src_hash → None (caller recomputes)
    assert v.get_segments(conn, m.id, ns, "deadbeefdeadbeef") is None
    # wrong namespace → None even with the right memory_id + src_hash (defense-in-depth)
    assert v.get_segments(conn, m.id, "no-such-ns", h) is None

    # re-upsert replaces (old seqs gone)
    v.upsert_segments(conn, m.id, ns, h, [(0, 0, 4, [5.0, 0.0, 0.0])])
    got2 = v.get_segments(conn, m.id, ns, h)
    assert _seg_set(got2) == {(0, 0, 4)}

    # explicit delete
    v.delete_segments(conn, m.id)
    assert v.get_segments(conn, m.id, ns, h) is None
    return m


def _forget_cascade(store):
    v = store._vectors
    conn = store._conn
    ns = _ns(store)
    m = store.write(body="apple pie to forget\n")
    v.upsert_segments(conn, m.id, ns, m.content_hash, SEGS)
    assert v.get_segments(conn, m.id, ns, m.content_hash) is not None
    store.forget(None, m.id)
    assert v.get_segments(conn, m.id, ns, m.content_hash) is None


# ─── pgvector ────────────────────────────────────────────────────────────────
def test_pg_roundtrip(pg_store):
    _roundtrip(pg_store)


def test_pg_forget_cascade(pg_store):
    _forget_cascade(pg_store)


def test_pg_segments_carry_namespace(pg_store):
    # Two namespaces (open mode would need tokens); here confirm the column is
    # stamped with the writing namespace and a foreign memory_id never leaks in.
    v = pg_store._vectors
    conn = pg_store._conn
    ns = _ns(pg_store)
    a = pg_store.write(body="apple a\n")
    b = pg_store.write(body="banana b\n")
    v.upsert_segments(conn, a.id, ns, a.content_hash, [(0, 0, 3, [1.0, 0.0, 0.0])])
    v.upsert_segments(conn, b.id, ns, b.content_hash, [(0, 0, 3, [0.0, 1.0, 0.0])])
    with conn.cursor() as cur:
        cur.execute("SELECT namespace FROM memory_segment WHERE memory_id=%s", (a.id,))
        assert {r[0] for r in cur.fetchall()} == {ns}
    # querying b's id never returns a's segments (memory_id isolation)
    assert _seg_set(v.get_segments(conn, b.id, ns, b.content_hash)) == {(0, 0, 3)}


# ─── qdrant ──────────────────────────────────────────────────────────────────
def test_qdrant_roundtrip(qdrant_store):
    _roundtrip(qdrant_store)


def test_qdrant_forget_cascade(qdrant_store):
    _forget_cascade(qdrant_store)


def test_qdrant_segments_collection_and_index(qdrant_store):
    from qdrant_client import QdrantClient
    qc = QdrantClient(url=QURL)
    seg_coll = f"{COLL}_segments"
    assert qc.collection_exists(seg_coll)
    schema = qc.get_collection(seg_coll).payload_schema or {}
    assert "memory_id" in schema


def test_qdrant_segments_carry_namespace(qdrant_store):
    v = qdrant_store._vectors
    conn = qdrant_store._conn
    ns = _ns(qdrant_store)
    a = qdrant_store.write(body="apple a\n")
    b = qdrant_store.write(body="banana b\n")
    v.upsert_segments(conn, a.id, ns, a.content_hash, [(0, 0, 3, [1.0, 0.0, 0.0])])
    v.upsert_segments(conn, b.id, ns, b.content_hash, [(0, 0, 3, [0.0, 1.0, 0.0])])
    # namespace lives in each segment point's payload
    from qdrant_client import QdrantClient
    from qdrant_client.models import FieldCondition, Filter, MatchValue
    qc = QdrantClient(url=QURL)
    pts, _ = qc.scroll(
        f"{COLL}_segments",
        scroll_filter=Filter(must=[FieldCondition(key="memory_id",
                                                  match=MatchValue(value=a.id))]),
        with_payload=True, limit=100)
    assert pts and all(p.payload["namespace"] == ns for p in pts)
    # b's id never yields a's segments
    assert _seg_set(v.get_segments(conn, b.id, ns, b.content_hash)) == {(0, 0, 3)}


def test_qdrant_two_namespaces_isolated(qdrant_store, monkeypatch):
    """Open mode: two tokens → two namespaces. A segment set written under ns A
    is keyed to A's memory and never surfaces for a memory in ns B."""
    from memgres.identity import new_token
    # rebuild the store in open mode on the same fresh PG + qdrant collections
    monkeypatch.setenv("MEMGRES_KEY_MODE", "open")
    cfg = load()
    conn = qdrant_store._conn
    migrate(conn, cfg)
    s = Store(cfg, embedder=_Keyword(), conn=conn)
    tok_a, tok_b = new_token(), new_token()
    a = s.write(tok_a, body="apple in A\n")
    b = s.write(tok_b, body="banana in B\n")
    ns_a = s._authorize(tok_a, need="read")
    ns_b = s._authorize(tok_b, need="read")
    assert ns_a != ns_b
    v = s._vectors
    v.upsert_segments(conn, a.id, ns_a, a.content_hash, [(0, 0, 3, [1.0, 0.0, 0.0])])
    v.upsert_segments(conn, b.id, ns_b, b.content_hash, [(0, 0, 3, [0.0, 1.0, 0.0])])
    assert _seg_set(v.get_segments(conn, a.id, ns_a, a.content_hash)) == {(0, 0, 3)}
    assert _seg_set(v.get_segments(conn, b.id, ns_b, b.content_hash)) == {(0, 0, 3)}
    # A's segments are invisible under B's namespace even with A's memory_id
    assert v.get_segments(conn, a.id, ns_b, a.content_hash) is None
    # forget A drops A's segments; B's remain
    s.forget(tok_a, a.id)
    assert v.get_segments(conn, a.id, ns_a, a.content_hash) is None
    assert v.get_segments(conn, b.id, ns_b, b.content_hash) is not None


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
