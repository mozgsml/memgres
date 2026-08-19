"""Enterprise pieces: claim-based draining (many workers, no double work) and
re-embedding the corpus under a new model.

- SKIP LOCKED claim: a row locked by one worker is skipped by another's drain
  (not embedded twice, not blocked on), and picked up once the lock releases.
- reembed(): switching the embedding model/dimension wipes the vector index,
  recreates it at the new dim, re-flags every memory, and rebuilds — on both
  backends — while bodies/history stay intact.
"""

import os
import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

psycopg = pytest.importorskip("psycopg")

from memgres.config import load  # noqa: E402
from memgres.embeddings import Embedder  # noqa: E402
from memgres.indexing import drain  # noqa: E402
from memgres.reembed import reembed  # noqa: E402
from memgres.schema import migrate  # noqa: E402
from memgres.store import Store  # noqa: E402

DSN = os.environ.get("MEMGRES_TEST_DSN",
                     "postgresql://memgres:memgres@localhost:55432/memgres")
QURL = os.environ.get("MEMGRES_TEST_QDRANT", "http://localhost:56333")
COLL = "memgres_claim_test"
_KEYS = ["apple", "banana", "cherry", "date"]


class _KW(Embedder):
    """Keyword stub of a chosen dimension, counting embed_documents calls."""
    def __init__(self, dim):
        self.dim = dim
        self.doc_calls = 0

    def _vec(self, t):
        t = t.lower()
        v = [float(t.count(_KEYS[i])) for i in range(self.dim)]
        n = sum(x * x for x in v) ** 0.5 or 1.0
        return [x / n for x in v]

    def embed_documents(self, texts):
        self.doc_calls += 1
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


def _env(monkeypatch, dim, backend="pgvector"):
    for k in list(os.environ):
        if k.startswith("MEMGRES_") or k == "QDRANT_URL":
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("MEMGRES_DATABASE_URL", DSN)
    monkeypatch.setenv("MEMGRES_FTS_LANGUAGE", "simple")
    monkeypatch.setenv("MEMGRES_EMBED_PROVIDER", "openai")   # shape only; stub injected
    monkeypatch.setenv("MEMGRES_EMBED_MODEL", f"stub{dim}")
    monkeypatch.setenv("MEMGRES_EMBED_DIM", str(dim))
    monkeypatch.setenv("MEMGRES_EMBED_API_KEY", "x")
    if backend == "qdrant":
        monkeypatch.setenv("MEMGRES_VECTOR_BACKEND", "qdrant")
        monkeypatch.setenv("QDRANT_URL", QURL)
        monkeypatch.setenv("MEMGRES_QDRANT_COLLECTION", COLL)


def _drop_qdrant():
    from qdrant_client import QdrantClient
    qc = QdrantClient(url=QURL)
    for c in (COLL, f"{COLL}_segments"):
        if qc.collection_exists(c):
            qc.delete_collection(c)


def _pending(conn, mid):
    with conn.cursor() as cur:
        cur.execute("SELECT embed_pending FROM memory WHERE id=%s", (mid,))
        return cur.fetchone()[0]


# ─── SKIP LOCKED claim: a locked row is skipped, embedded once, retried later ──
def test_skip_locked_claim(monkeypatch):
    if not _pg_reachable():
        pytest.skip("no test Postgres")
    _fresh_pg()
    _env(monkeypatch, 3)
    conn = psycopg.connect(DSN)
    migrate(conn, load())
    emb = _KW(3)
    s = Store(replace(load(), embed_dispatch="async"), embedder=emb, conn=conn)
    a = s.write(body="apple apple\n")           # pending (async)
    b = s.write(body="banana banana\n")         # pending
    assert _pending(conn, a.id) and _pending(conn, b.id)

    # A second connection locks row `a`, as if another worker were embedding it.
    other = psycopg.connect(DSN)
    with other.cursor() as oc:
        oc.execute("SELECT id FROM memory WHERE id=%s FOR UPDATE", (a.id,))  # tx stays open

    # This worker drains: it must SKIP the locked `a` and embed only `b`.
    n = drain(conn, s.cfg, s.embedder, s._vectors)
    assert n == 1 and emb.doc_calls == 1
    assert _pending(conn, a.id) is True         # skipped, still pending
    assert _pending(conn, b.id) is False        # embedded once

    other.rollback()                            # release the lock on `a`
    other.close()
    n = drain(conn, s.cfg, s.embedder, s._vectors)
    assert n == 1 and _pending(conn, a.id) is False   # now claimed + embedded
    conn.close()


# ─── reembed(): switch model/dimension, rebuild the index ─────────────────────
def _reembed_dim_change(backend):
    def run(monkeypatch):
        if backend == "qdrant":
            if not _qdrant_reachable():
                pytest.skip("no test Postgres or Qdrant")
            pytest.importorskip("qdrant_client")
        elif not _pg_reachable():
            pytest.skip("no test Postgres")
        _fresh_pg()
        if backend == "qdrant":
            _drop_qdrant()
        # start at dim 3, write + embed inline
        _env(monkeypatch, 3, backend)
        conn = psycopg.connect(DSN)
        migrate(conn, load())
        s3 = Store(load(), embedder=_KW(3), conn=conn)   # inline dispatch
        m = s3.write(body="apple apple apple\n")
        assert s3._vectors.chunk_src_hash(conn, m.id, "") == m.content_hash
        conn.commit()   # close the read tx so reembed's DDL isn't blocked by it

        # switch to a dim-4 model and re-embed
        _env(monkeypatch, 4, backend)
        cfg4 = load()
        flagged, embedded = reembed(cfg4, embedder=_KW(4))
        assert flagged == 1 and embedded == 1

        # meta now records dim 4, and recall works under the new model
        with conn.cursor() as cur:
            cur.execute("SELECT embed_dim FROM memgres_meta WHERE only_row")
            assert cur.fetchone()[0] == 4
        s4 = Store(cfg4, embedder=_KW(4), conn=conn)
        hits = s4.recall(None, "apple", mode="semantic")
        assert len(hits) == 1 and hits[0].id == m.id
        assert s4.get(None, m.id).body == "apple apple apple\n"   # body intact
        conn.close()
    return run


# ─── a poison row (always fails to embed) must not wedge the queue ────────────
class _Poison(_KW):
    def embed_documents(self, texts):
        if any("poison" in t.lower() for t in texts):
            raise RuntimeError("embedder blew up on this body")
        return super().embed_documents(texts)


def test_poison_row_does_not_wedge_queue(monkeypatch):
    if not _pg_reachable():
        pytest.skip("no test Postgres")
    _fresh_pg()
    _env(monkeypatch, 3)
    conn = psycopg.connect(DSN)
    migrate(conn, load())
    # backoff 0 so both attempts happen in one pass; max 2 → quick dead-letter
    cfg = replace(load(), embed_dispatch="async",
                  embed_max_attempts=2, embed_retry_backoff_s=0.0)
    s = Store(cfg, embedder=_Poison(3), conn=conn)
    good1 = s.write(body="apple apple\n")
    bad = s.write(body="poison apple\n")     # embedder raises on this one
    good2 = s.write(body="banana banana\n")

    n = drain(conn, cfg, s.embedder, s._vectors)
    # the poison row did NOT block the good rows on either side of it
    assert s._vectors.chunk_src_hash(conn, good1.id, "") is not None
    assert s._vectors.chunk_src_hash(conn, good2.id, "") is not None
    assert n == 2
    # the poison row is still pending, dead-lettered at max attempts
    assert _pending(conn, bad.id) is True
    with conn.cursor() as cur:
        cur.execute("SELECT embed_attempts FROM memory WHERE id=%s", (bad.id,))
        assert cur.fetchone()[0] == 2

    # further drains touch nothing (poison out of rotation, goods done)
    assert drain(conn, cfg, s.embedder, s._vectors) == 0
    with conn.cursor() as cur:
        cur.execute("SELECT embed_attempts FROM memory WHERE id=%s", (bad.id,))
        assert cur.fetchone()[0] == 2       # not retried past the cap
    conn.close()


def test_pg_reembed_dim_change(monkeypatch):
    _reembed_dim_change("pgvector")(monkeypatch)


def test_qdrant_reembed_dim_change(monkeypatch):
    _reembed_dim_change("qdrant")(monkeypatch)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
