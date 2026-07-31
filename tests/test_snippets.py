"""Snippet recall flow against live backends (pgvector + qdrant).

Covers the semantic best-segment path (embed + cache + reuse + invalidate), the
lexical ts_headline path, and the snippet/full_body flags. Ranking itself is
tested elsewhere — here we only assert what gets hung on each hit.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

psycopg = pytest.importorskip("psycopg")

from memgres.config import load  # noqa: E402
from memgres.diffing import content_hash  # noqa: E402
from memgres.embeddings import Embedder  # noqa: E402
from memgres.schema import migrate  # noqa: E402
from memgres.store import Store  # noqa: E402

DSN = os.environ.get("MEMGRES_TEST_DSN",
                     "postgresql://memgres:memgres@localhost:55432/memgres")
QURL = os.environ.get("MEMGRES_TEST_QDRANT", "http://localhost:56333")
COLL = "memgres_snippet_test"

# Three one-sentence lines, each keyed to a distinct fruit; apples on line 2.
BODY = ("Bananas grow on tall plants here.\n"
        "Red apples taste sweet and crisp.\n"
        "Cherries are small round fruits.\n")
BODY2 = ("Grapes hang in heavy purple bunches.\n"
         "Fresh apples ripen on the orchard branch.\n"
         "Lemons are bright yellow and quite sour.\n")
QUERY = "apples"


class _Keyword(Embedder):
    """Toy embedder: dims counting apple/banana/cherry mentions. Deterministic
    best-segment selection without a real model or network."""
    dim = 3

    def _vec(self, t):
        t = t.lower()
        v = [float(t.count("apple")), float(t.count("banana")),
             float(t.count("cherr"))]
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
    # small segments so each sentence is its own tile (default 400 = one tile)
    monkeypatch.setenv("MEMGRES_SNIPPET_SEG_CHARS", "45")
    monkeypatch.setenv("MEMGRES_SNIPPET_SEG_OVERLAP", "0")


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


def _ns(store):
    return store._authorize(None, need="read")[0]


def _find(hits, mid):
    for h in hits:
        if h.id == mid:
            return h
    raise AssertionError(f"memory {mid} not in hits")


# ─── semantic best-segment ───────────────────────────────────────────────────
def _semantic_best_segment(store):
    m = store.write(body=BODY)
    hits = store.recall(None, QUERY, mode="semantic")
    h = _find(hits, m.id)
    assert "apples" in h.snippet
    assert "Bananas" not in h.snippet and "Cherries" not in h.snippet
    assert h.line == 2                      # the apples sentence is line 2


def test_pg_semantic_best_segment(pg_store):
    _semantic_best_segment(pg_store)


def test_qdrant_semantic_best_segment(qdrant_store):
    _semantic_best_segment(qdrant_store)


# ─── cache populate + reuse ──────────────────────────────────────────────────
def _cache_populate_reuse(store):
    m = store.write(body=BODY)
    first = _find(store.recall(None, QUERY, mode="semantic"), m.id).snippet
    # segments were computed + stored under the body's content_hash
    segs = store._vectors.get_segments(store._conn, m.id, _ns(store), content_hash(BODY))
    assert segs is not None and len(segs) >= 2
    # a second recall reuses them and returns the same snippet
    second = _find(store.recall(None, QUERY, mode="semantic"), m.id).snippet
    assert first == second and "apples" in second


def test_pg_cache_populate_reuse(pg_store):
    _cache_populate_reuse(pg_store)


def test_qdrant_cache_populate_reuse(qdrant_store):
    _cache_populate_reuse(qdrant_store)


# ─── invalidation on edit ────────────────────────────────────────────────────
def _invalidate_on_edit(store):
    m = store.write(body=BODY)
    store.recall(None, QUERY, mode="semantic")       # caches segs for BODY
    old_hash = content_hash(BODY)
    m2 = store.write(id=m.id, body=BODY2)             # new body, new hash
    new_hash = m2.content_hash
    h = _find(store.recall(None, QUERY, mode="semantic"), m.id)
    # stale segments recomputed: old hash gone, new hash present
    assert store._vectors.get_segments(store._conn, m.id, _ns(store), old_hash) is None
    assert store._vectors.get_segments(store._conn, m.id, _ns(store), new_hash) is not None
    # snippet now reflects the edited body
    assert "apples" in h.snippet and "orchard" in h.snippet
    assert "Bananas" not in h.snippet


def test_pg_invalidate_on_edit(pg_store):
    _invalidate_on_edit(pg_store)


def test_qdrant_invalidate_on_edit(qdrant_store):
    _invalidate_on_edit(qdrant_store)


# ─── lexical ts_headline ─────────────────────────────────────────────────────
def _lexical_ts_headline(store):
    m = store.write(body=BODY)
    h = _find(store.recall(None, QUERY, mode="lexical"), m.id)
    # ts_headline marks the matched word (default <b>…</b>)
    assert "apples" in h.snippet
    assert "<b>" in h.snippet or "<b>apples</b>" in h.snippet
    assert h.line is None                    # ts_headline has no offset


def test_pg_lexical_ts_headline(pg_store):
    _lexical_ts_headline(pg_store)


def test_qdrant_lexical_ts_headline(qdrant_store):
    _lexical_ts_headline(qdrant_store)


# ─── flags: snippet off / full_body off ──────────────────────────────────────
def _flags(store):
    m = store.write(body=BODY)

    no_snip = _find(store.recall(None, QUERY, mode="semantic", snippet=False), m.id)
    assert no_snip.snippet is None and no_snip.body is not None

    no_body = _find(store.recall(None, QUERY, mode="semantic", full_body=False), m.id)
    assert no_body.body is None and no_body.snippet is not None
    assert "apples" in no_body.snippet


def test_pg_flags(pg_store):
    _flags(pg_store)


def test_qdrant_flags(qdrant_store):
    _flags(qdrant_store)


# ─── MEMGRES_SNIPPET_SEMANTIC=false → ts_headline, no segments created ────────
def _semantic_disabled_no_segments(store):
    m = store.write(body=BODY)
    h = _find(store.recall(None, QUERY, mode="semantic"), m.id)
    # fell back to ts_headline: a snippet, but no segment cache for this id
    assert h.snippet is not None
    assert store._vectors.get_segments(store._conn, m.id, _ns(store), content_hash(BODY)) is None


def test_pg_semantic_disabled_no_segments(monkeypatch, pg_store):
    monkeypatch.setenv("MEMGRES_SNIPPET_SEMANTIC", "false")
    # rebuild the store's cfg with the flag off
    pg_store.cfg = load()
    _semantic_disabled_no_segments(pg_store)


def test_qdrant_semantic_disabled_no_segments(monkeypatch, qdrant_store):
    monkeypatch.setenv("MEMGRES_SNIPPET_SEMANTIC", "false")
    qdrant_store.cfg = load()
    _semantic_disabled_no_segments(qdrant_store)
