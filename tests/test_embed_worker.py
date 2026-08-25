"""Async embedding: the write path flags embed_pending, a worker drains it.

Covers both backends. Asserts the write-side flag, that semantic recall is blind
until a drain runs, that drain builds the chunks (and backfills), idempotency
(re-draining a current row re-embeds nothing), the content_hash guard when a body
changes mid-flight, and that a metadata-only edit never cancels a pending embed.
"""

import os
import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

psycopg = pytest.importorskip("psycopg")

from memgres.config import load  # noqa: E402
from memgres.embed_worker import EmbedWorker  # noqa: E402
from memgres.embeddings import Embedder  # noqa: E402
from memgres.indexing import drain  # noqa: E402
from memgres.schema import migrate  # noqa: E402
from memgres.store import Store  # noqa: E402

DSN = os.environ.get("MEMGRES_TEST_DSN",
                     "postgresql://memgres:memgres@localhost:55432/memgres")
QURL = os.environ.get("MEMGRES_TEST_QDRANT", "http://localhost:56333")
COLL = "memgres_worker_test"


class _CountingKeyword(Embedder):
    """Toy 3-dim embedder that counts embed_documents calls, so a test can prove
    a drain did (or didn't) re-embed."""
    dim = 3

    def __init__(self):
        self.doc_calls = 0

    def _vec(self, t):
        t = t.lower()
        v = [float(t.count("apple")), float(t.count("banana")), float(t.count("cherry"))]
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


@pytest.fixture
def async_pg(monkeypatch):
    if not _pg_reachable():
        pytest.skip("no test Postgres")
    _fresh_pg()
    _base_env(monkeypatch)
    conn = psycopg.connect(DSN)
    migrate(conn, load())
    emb = _CountingKeyword()
    s = Store(replace(load(), embed_dispatch="async"), embedder=emb, conn=conn)
    assert s.cfg.embed_dispatch == "async"     # defer embedding to an explicit drain
    yield s, emb
    conn.close()


@pytest.fixture
def async_qdrant(monkeypatch):
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
    emb = _CountingKeyword()
    s = Store(replace(load(), embed_dispatch="async"), embedder=emb, conn=conn)
    yield s, emb
    conn.close()


def _ns(store):
    return store._authorize(None, need="read")[0]


def _pending(store, mid) -> bool:
    with store._conn.cursor() as cur:
        cur.execute("SELECT embed_pending FROM memory WHERE id=%s", (mid,))
        return cur.fetchone()[0]


# ─── the core async cycle: flag → blind recall → drain → visible ─────────────
def _async_cycle(store, emb):
    ns = _ns(store)
    m = store.write(body="apple apple apple\n")
    # write only flagged it; nothing embedded yet
    assert _pending(store, m.id) is True
    assert emb.doc_calls == 0
    assert store._vectors.chunk_src_hash(store._conn, m.id, ns) is None
    # semantic recall is blind until the worker runs
    assert store.recall(None, "apple", mode="semantic") == []

    # drain builds the chunks
    n = drain(store._conn, store.cfg, store.embedder, store._vectors)
    assert n == 1 and emb.doc_calls == 1
    assert _pending(store, m.id) is False
    assert store._vectors.chunk_src_hash(store._conn, m.id, ns) == m.content_hash
    hits = store.recall(None, "apple", mode="semantic")
    assert len(hits) == 1 and hits[0].id == m.id

    # idempotent: re-flag + drain re-embeds nothing (chunks already current)
    with store._conn.cursor() as cur:
        cur.execute("UPDATE memory SET embed_pending=true WHERE id=%s", (m.id,))
    store._conn.commit()
    drain(store._conn, store.cfg, store.embedder, store._vectors)
    assert emb.doc_calls == 1                 # unchanged: skipped
    assert _pending(store, m.id) is False     # but the flag was cleared


def test_pg_async_cycle(async_pg):
    _async_cycle(*async_pg)


def test_qdrant_async_cycle(async_qdrant):
    _async_cycle(*async_qdrant)


# ─── content_hash guard: a body change before drain wins ─────────────────────
def _edit_before_drain(store, emb):
    ns = _ns(store)
    m = store.write(body="apple apple\n")               # pending, not embedded
    m2 = store.write(id=m.id, body="cherry cherry\n")   # still pending, new hash
    assert _pending(store, m.id) is True
    drain(store._conn, store.cfg, store.embedder, store._vectors)
    # chunks reflect the CURRENT body, not the stale one
    assert store._vectors.chunk_src_hash(store._conn, m.id, ns) == m2.content_hash
    assert [h.id for h in store.recall(None, "cherry", mode="semantic")] == [m.id]
    apple = store.recall(None, "apple", mode="semantic")   # stale body is gone
    assert apple == [] or apple[0].score < 0.1


def test_pg_edit_before_drain(async_pg):
    _edit_before_drain(*async_pg)


def test_qdrant_edit_before_drain(async_qdrant):
    _edit_before_drain(*async_qdrant)


# ─── a metadata-only edit must NOT cancel a pending embed ────────────────────
def _retag_preserves_pending(store, emb):
    m = store.write(body="apple apple\n", tags=["a"])   # pending
    assert _pending(store, m.id) is True
    store.write(id=m.id, tags=["a", "b"])               # retag, body unchanged
    assert _pending(store, m.id) is True                # still pending!
    drain(store._conn, store.cfg, store.embedder, store._vectors)
    assert _pending(store, m.id) is False
    assert len(store.recall(None, "apple", mode="semantic")) == 1


def test_pg_retag_preserves_pending(async_pg):
    _retag_preserves_pending(*async_pg)


def test_qdrant_retag_preserves_pending(async_qdrant):
    _retag_preserves_pending(*async_qdrant)


# ─── EmbedWorker.drain_once backfills a pre-existing pending row ──────────────
def _worker_backfill(store, emb):
    m = store.write(body="banana banana\n")             # pending
    assert _pending(store, m.id) is True
    # a worker with its OWN connection drains the backlog
    worker = EmbedWorker(store.cfg, store.embedder, store._vectors,
                         connect=lambda: psycopg.connect(DSN))
    try:
        assert worker.drain_once() == 1
    finally:
        worker.stop()
    assert _pending(store, m.id) is False
    assert len(store.recall(None, "banana", mode="semantic")) == 1


def test_pg_worker_backfill(async_pg):
    _worker_backfill(*async_pg)


def test_qdrant_worker_backfill(async_qdrant):
    _worker_backfill(*async_qdrant)


# ─── sync mode (embed_dispatch=inline) embeds inline, no drain needed ──────────────
def test_pg_sync_mode_inline(async_pg):
    store, emb = async_pg
    sync_cfg = replace(store.cfg, embed_dispatch="inline")
    s2 = Store(sync_cfg, embedder=emb, conn=store._conn)
    m = s2.write(body="cherry pie\n")
    assert _pending(s2, m.id) is False                  # embedded inline already
    assert len(s2.recall(None, "cherry", mode="semantic")) == 1


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
