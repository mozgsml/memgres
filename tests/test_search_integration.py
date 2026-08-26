"""Recall against a live Postgres: lexical, semantic (stub), hybrid + filters."""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

psycopg = pytest.importorskip("psycopg")

from memgres.config import load  # noqa: E402
from memgres.schema import migrate  # noqa: E402
from memgres.store import Store  # noqa: E402
from memgres.embeddings import Embedder  # noqa: E402

DSN = os.environ.get("MEMGRES_TEST_DSN",
                     "postgresql://memgres:memgres@localhost:55432/memgres")


def _reachable() -> bool:
    try:
        psycopg.connect(DSN, connect_timeout=2).close()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _reachable(), reason="no test Postgres")


def _reset():
    with psycopg.connect(DSN, autocommit=True) as c, c.cursor() as cur:
        # Close out anything still holding the schema before dropping it. Tests
        # in this file close their connection on the LAST line, so a failing
        # assertion leaks one — and the next test's DROP then blocks on its
        # locks, turning one red test into a hung suite. Fail fast instead.
        cur.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = current_database() "
                    "  AND pid <> pg_backend_pid()")
        cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")


def _clear_env(monkeypatch):
    for k in list(os.environ):
        if k.startswith("MEMGRES_"):
            monkeypatch.delenv(k, raising=False)
    # Captions are not what most of this suite is about; the requirement
    # has its own file (test_require_title.py) covering both settings.
    monkeypatch.setenv("MEMGRES_REQUIRE_TITLE", "false")
    monkeypatch.setenv("MEMGRES_DATABASE_URL", DSN)
    monkeypatch.setenv("MEMGRES_FTS_LANGUAGE", "simple")


class _Keyword(Embedder):
    """Toy embedder: 3 dims counting apple/banana/cherry mentions. Enough to
    make semantic ordering deterministic without a real model."""
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


def test_lexical_recall_and_tag_filter(monkeypatch):
    _reset(); _clear_env(monkeypatch)
    monkeypatch.setenv("MEMGRES_EMBED_PROVIDER", "none")
    cfg = load(); conn = psycopg.connect(DSN); migrate(conn, cfg)
    s = Store(cfg, conn=conn)
    s.write(body="the quick brown fox jumps\n", tags=["animal"])
    s.write(body="a slow green turtle swims\n", tags=["animal"])
    s.write(body="quick database indexing tips\n", tags=["tech"])

    hits = s.recall(None, "quick", mode="lexical")
    bodies = [h.snippet for h in hits]   # short bodies → snippet==body
    assert any("fox" in b for b in bodies) and any("database" in b for b in bodies)

    tech = s.recall(None, "quick", mode="lexical", tags=["tech"])
    assert len(tech) == 1 and "database" in tech[0].snippet
    conn.close()


def test_semantic_recall_and_subtree(monkeypatch):
    _reset(); _clear_env(monkeypatch)
    monkeypatch.setenv("MEMGRES_EMBED_PROVIDER", "openai")   # cloud shape, but we inject a stub
    monkeypatch.setenv("MEMGRES_EMBED_MODEL", "stub")
    monkeypatch.setenv("MEMGRES_EMBED_DIM", "3")
    monkeypatch.setenv("MEMGRES_EMBED_API_KEY", "x")
    cfg = load(); conn = psycopg.connect(DSN); migrate(conn, cfg)
    s = Store(cfg, embedder=_Keyword(), conn=conn)
    s.write(body="I love apple pie\n", path="food.fruit.apple", tags=["t"])
    s.write(body="banana bread recipe\n", path="food.fruit.banana", tags=["t"])
    s.write(body="cherry orchard notes\n", path="misc.cherry", tags=["t"])

    top = s.recall(None, "apple apple", mode="semantic", k=1)
    assert len(top) == 1 and "apple" in top[0].snippet     # nearest by vector

    # subtree scope: only food.fruit, cherry (in misc) excluded even if queried
    sub = s.recall(None, "cherry", mode="semantic", path_prefix="food.fruit")
    assert all(h.path.startswith("food.fruit") for h in sub)
    conn.close()


def test_hybrid_fuses_both(monkeypatch):
    _reset(); _clear_env(monkeypatch)
    monkeypatch.setenv("MEMGRES_EMBED_PROVIDER", "openai")
    monkeypatch.setenv("MEMGRES_EMBED_MODEL", "stub")
    monkeypatch.setenv("MEMGRES_EMBED_DIM", "3")
    monkeypatch.setenv("MEMGRES_EMBED_API_KEY", "x")
    cfg = load(); conn = psycopg.connect(DSN); migrate(conn, cfg)
    s = Store(cfg, embedder=_Keyword(), conn=conn)
    s.write(body="apple identifier XZ-900 exact code\n")
    s.write(body="apple apple apple everywhere\n")
    hits = s.recall(None, "apple", mode="hybrid", k=5)
    assert len(hits) == 2                       # both surfaced, fused, deduped
    ids = {h.id for h in hits}
    assert len(ids) == 2
    conn.close()


def test_auto_mode_picks_lexical_without_embedder(monkeypatch):
    _reset(); _clear_env(monkeypatch)
    monkeypatch.setenv("MEMGRES_EMBED_PROVIDER", "none")
    cfg = load(); conn = psycopg.connect(DSN); migrate(conn, cfg)
    s = Store(cfg, conn=conn)
    s.write(body="findable content here\n")
    hits = s.recall(None, "findable")           # auto -> lexical
    assert len(hits) == 1
    conn.close()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))


# ─── a tsvector column can be NULL, and NULL is not empty ────────────────────
def test_a_legacy_row_without_a_title_vector_does_not_break_recall(monkeypatch):
    """`0004_title.sql` left pre-existing rows at title='' with title_fts NULL,
    judged harmless because nothing read the column. Recall reads it now:
    `ts_rank(NULL, q)` is NULL, a summed score becomes NULL, NULL sorts FIRST
    under `ORDER BY score DESC`, and `float(None)` is a TypeError that no
    transport converts into anything but a 500. One legacy row would take down
    lexical recall for the whole deployment — and 50 of 95 rows in this repo's
    own corpus carried that NULL."""
    _reset()
    _clear_env(monkeypatch)
    monkeypatch.setenv("MEMGRES_EMBED_PROVIDER", "none")
    conn = psycopg.connect(DSN)
    from memgres.schema import migrate as _migrate
    _migrate(conn, load())
    store = Store(load(), conn=conn)
    a = store.write(body="apples are sweet", title="Fruit")
    legacy = store.write(body="apples grow on trees", title="Tree")
    with store._conn.cursor() as cur:
        cur.execute("UPDATE memory SET title_fts = NULL WHERE id=%s", (legacy.id,))
    store._conn.commit()

    hits = store.recall(None, "apples", mode="lexical")
    assert {h.id for h in hits} == {a.id, legacy.id}
    assert all(isinstance(h.score, float) for h in hits)
    conn.close()


def test_the_migration_leaves_no_null_title_vectors(monkeypatch):
    from memgres.schema import migrate
    _reset()
    _clear_env(monkeypatch)
    monkeypatch.setenv("MEMGRES_EMBED_PROVIDER", "none")
    conn = psycopg.connect(DSN)
    migrate(conn, load())
    store = Store(load(), conn=conn)
    m = store.write(body="one", title="One")
    with store._conn.cursor() as cur:
        cur.execute("UPDATE memory SET title_fts = NULL WHERE id=%s", (m.id,))
    store._conn.commit()

    migrate(store._conn, store.cfg)
    store._conn.commit()
    with store._conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM memory WHERE title_fts IS NULL")
        assert cur.fetchone()[0] == 0
        # and it is the TITLE that was indexed, not an empty vector
        cur.execute("SELECT title_fts @@ plainto_tsquery(%s::regconfig, 'One') "
                    "FROM memory WHERE id=%s", (store.cfg.fts_language, m.id))
        assert cur.fetchone()[0] is True
    conn.close()
