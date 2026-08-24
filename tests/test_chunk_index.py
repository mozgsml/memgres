"""Chunk-based ranking: dedup, tail coverage, and the anti-flooding loop.

Chunks are the semantic index, so recall must (a) return ONE hit per memory even
when many of its chunks match, (b) find a match in the TAIL of a long body that a
single whole-body vector would drown out, and (c) not let one many-chunk document
crowd distinct memories out of the top-k (the iterative-exclude loop). Both
backends. Sync mode (embed inline) so a write is immediately searchable.
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
from memgres.identity import new_token  # noqa: E402
from memgres.schema import migrate  # noqa: E402
from memgres.store import Store  # noqa: E402

DSN = os.environ.get("MEMGRES_TEST_DSN",
                     "postgresql://memgres:memgres@localhost:55432/memgres")
QURL = os.environ.get("MEMGRES_TEST_QDRANT", "http://localhost:56333")
COLL = "memgres_chunkidx_test"


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
    # small chunks so a body splits into many, exercising grouping/tail/flooding
    monkeypatch.setenv("MEMGRES_SNIPPET_SEG_CHARS", "30")
    monkeypatch.setenv("MEMGRES_SNIPPET_SEG_OVERLAP", "0")


@pytest.fixture
def pg_store(monkeypatch):
    if not _pg_reachable():
        pytest.skip("no test Postgres")
    _fresh_pg()
    _base_env(monkeypatch)
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


# ─── one hit per memory even when many of its chunks match ───────────────────
def _dedup_one_hit(store):
    # a body of nothing but 'apple', split into several chunks
    m = store.write(body="apple. " * 20)
    hits = store.recall(None, "apple", mode="semantic", k=10)
    assert len(hits) == 1 and hits[0].id == m.id     # not one-per-chunk


def test_pg_dedup_one_hit(pg_store):
    _dedup_one_hit(pg_store)


def test_qdrant_dedup_one_hit(qdrant_store):
    _dedup_one_hit(qdrant_store)


# ─── a match in the TAIL of a long body is found (whole-body vector can't) ────
def _tail_coverage(store):
    # 40 banana sentences would dominate a single doc vector; the lone apple
    # sentence is at the very end. Chunk ranking still finds it at ~1.0.
    m = store.write(body=("banana. " * 40) + "apple sits right at the very end.")
    hits = store.recall(None, "apple", mode="semantic", k=5)
    ids = [h.id for h in hits]
    assert m.id in ids
    top = next(h for h in hits if h.id == m.id)
    assert top.score > 0.9        # matched the pure-apple tail chunk, not a blend


def test_pg_tail_coverage(pg_store):
    _tail_coverage(pg_store)


def test_qdrant_tail_coverage(qdrant_store):
    _tail_coverage(qdrant_store)


# ─── one many-chunk doc must not crowd distinct memories out of top-k ─────────
def _flood_does_not_starve(store):
    # A huge all-apple document with MORE chunks than one overfetch window
    # (seg_chars=30 → "apple. "*300 ≈ 70 chunks > overfetch 30 for k=3), so round 1
    # groups to just the flood and the iterative-exclude loop MUST run to reach the
    # other memories. Plus three small apple notes.
    store.write(body="apple. " * 300, source="flood")
    smalls = [store.write(body=f"apple note number {i}.\n").id for i in range(3)]
    hits = store.recall(None, "apple", mode="semantic", k=3)
    ids = [h.id for h in hits]
    # k=3 distinct memories, and the flood didn't swallow every slot: at least two
    # of the small notes surface (iterative-exclude past the many-chunk doc).
    assert len(ids) == 3 and len(set(ids)) == 3
    assert len(set(ids) & set(smalls)) >= 2


def test_pg_flood_does_not_starve(pg_store):
    _flood_does_not_starve(pg_store)


def test_qdrant_flood_does_not_starve(qdrant_store):
    _flood_does_not_starve(qdrant_store)


# ─── filters (tags/subtree) still apply on top of grouped chunk ranking ───────
def _filters_apply(store):
    a = store.write(body="apple in tech.\n", path="tech.note", tags=["tech"])
    store.write(body="apple in food.\n", path="food.note", tags=["food"])
    tech = store.recall(None, "apple", mode="semantic", tags=["tech"])
    assert [h.id for h in tech] == [a.id]
    sub = store.recall(None, "apple", mode="semantic", path_prefix="food")
    assert all(h.path.startswith("food") for h in sub)


def test_pg_filters_apply(pg_store):
    _filters_apply(pg_store)


def test_qdrant_filters_apply(qdrant_store):
    _filters_apply(qdrant_store)


# ─── adversarial: semantic recall never crosses tenants (open mode) ───────────
def _cross_tenant_semantic_isolation(store):
    # Rebuild the store in OPEN mode on the same fresh DB + backend: two tokens →
    # two namespaces. A semantic query in one namespace must never rank, snippet,
    # or return the other's memory — exercising grouped_chunk_search's namespace
    # filter AND the fetch_hit_rows namespace re-filter on BOTH backends.
    cfg = replace(store.cfg, key_mode="open")
    conn = store._conn
    migrate(conn, cfg)
    s = Store(cfg, embedder=store.embedder, conn=conn, backend=store._vectors)
    from memgres import identity as ident

    ta, tb = new_token(), new_token()
    for t in (ta, tb):       # open mode: each token asks for its own space
        ident.create_own_namespace(conn, ident.resolve(conn, cfg, t), "mine")
    a = s.write(ta, body="apple secret belonging to tenant A.\n")
    b = s.write(tb, body="apple secret belonging to tenant B.\n")
    assert s._authorize(ta, need="read")[0] != s._authorize(tb, need="read")[0]

    a_hits = s.recall(ta, "apple", mode="semantic")
    assert [h.id for h in a_hits] == [a.id]                 # only A's own
    assert all("tenant B" not in (h.snippet or "") for h in a_hits)

    b_hits = s.recall(tb, "apple", mode="semantic")
    assert [h.id for h in b_hits] == [b.id]                 # only B's own
    assert a.id not in [h.id for h in b_hits]


def test_pg_cross_tenant_semantic_isolation(pg_store):
    _cross_tenant_semantic_isolation(pg_store)


def test_qdrant_cross_tenant_semantic_isolation(qdrant_store):
    _cross_tenant_semantic_isolation(qdrant_store)


# ─── semantic recall across several namespaces (both backends) ───────────────
def _multi_space_semantic(store):
    """`space="all"` must widen the SEMANTIC path to exactly the namespaces the
    caller reaches — no more, no fewer. This is the one place where the ranking
    filter (Qdrant payload / pgvector SQL) and the row filter (Postgres) could
    disagree: a narrower ranking filter silently loses hits, a wider one is
    caught by `fetch_hit_rows`. Running it on both backends is what keeps them
    honest against each other.
    """
    from memgres import identity as ident

    cfg = replace(store.cfg, key_mode="managed")
    conn = store._conn
    migrate(conn, cfg)
    s = Store(cfg, embedder=store.embedder, conn=conn, backend=store._vectors)

    uid = ident.create_user(conn, name="owner")
    ident.create_namespace(conn, uid, "work")
    ident.create_namespace(conn, uid, "home")
    tok, _ = ident.issue_token(conn, uid)

    other = ident.create_user(conn, name="stranger")
    ident.create_namespace(conn, other, "theirs")
    other_tok, _ = ident.issue_token(conn, other)

    w = s.write(tok, body="apple pie recipe from work.\n", space="work")
    h = s.write(tok, body="apple tree in the home garden.\n", space="home")
    s.write(other_tok, body="apple orchard that is none of your business.\n",
            space="theirs")

    hits = s.recall(tok, "apple", mode="semantic", space="all")
    assert {x.id for x in hits} == {w.id, h.id}          # both of mine, only mine
    assert all("none of your business" not in (x.snippet or "") for x in hits)
    # every hit says which namespace it came from, by id AND by name
    assert {x.space for x in hits} == {"work", "home"}
    assert all(x.namespace for x in hits)

    # naming a subset narrows it back down
    only_work = s.recall(tok, "apple", mode="semantic", space="work")
    assert [x.id for x in only_work] == [w.id]

    # the same namespace named twice (by name and by id) is searched once
    work_id = next(x.namespace for x in only_work)
    twice = s.recall(tok, "apple", mode="semantic", space="work", space_id=work_id)
    assert [x.id for x in twice] == [w.id]


def test_pg_multi_space_semantic(pg_store):
    _multi_space_semantic(pg_store)


def test_qdrant_multi_space_semantic(qdrant_store):
    _multi_space_semantic(qdrant_store)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
