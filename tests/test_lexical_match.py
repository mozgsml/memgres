"""Lexical AND/OR word-combination modes (MEMGRES_LEXICAL_MATCH / match arg).

`match="any"` (the shipped default) ORs the query words: a row matches if it
contains *any* of them. `match="all"` ANDs them: a row must contain *every*
word. Both run against a live Postgres FTS.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

psycopg = pytest.importorskip("psycopg")

from memgres.config import load  # noqa: E402
from memgres.schema import migrate  # noqa: E402
from memgres.store import Store  # noqa: E402

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
        cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")


def _clear_env(monkeypatch):
    for k in list(os.environ):
        if k.startswith("MEMGRES_"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("MEMGRES_DATABASE_URL", DSN)
    monkeypatch.setenv("MEMGRES_FTS_LANGUAGE", "simple")
    monkeypatch.setenv("MEMGRES_EMBED_PROVIDER", "none")


def _seed(monkeypatch):
    """Two rows, each containing only ONE of the two query terms."""
    _reset(); _clear_env(monkeypatch)
    cfg = load(); conn = psycopg.connect(DSN); migrate(conn, cfg)
    s = Store(cfg, conn=conn)
    s.write(body="alpha standalone note\n")   # has 'alpha', not 'omega'
    s.write(body="omega standalone note\n")   # has 'omega', not 'alpha'
    return s, conn


def test_any_returns_rows_matching_either_word(monkeypatch):
    s, conn = _seed(monkeypatch)
    hits = s.recall(None, "alpha omega", mode="lexical", match="any")
    bodies = " ".join(h.snippet for h in hits)   # short bodies → snippet==body
    assert len(hits) == 2 and "alpha" in bodies and "omega" in bodies
    conn.close()


def test_all_requires_every_word(monkeypatch):
    s, conn = _seed(monkeypatch)
    # No single row has BOTH words, so ANDing them yields nothing.
    hits = s.recall(None, "alpha omega", mode="lexical", match="all")
    assert hits == []
    conn.close()


def test_default_match_behaves_as_any(monkeypatch):
    s, conn = _seed(monkeypatch)
    # No explicit match -> falls back to cfg.lexical_match, whose default is any.
    hits = s.recall(None, "alpha omega", mode="lexical")
    assert len(hits) == 2
    conn.close()


def test_config_all_narrows_default(monkeypatch):
    _reset(); _clear_env(monkeypatch)
    monkeypatch.setenv("MEMGRES_LEXICAL_MATCH", "all")
    cfg = load(); conn = psycopg.connect(DSN); migrate(conn, cfg)
    s = Store(cfg, conn=conn)
    s.write(body="alpha standalone note\n")
    s.write(body="omega standalone note\n")
    # server config default = all, no per-call match -> AND -> nothing
    assert s.recall(None, "alpha omega", mode="lexical") == []
    conn.close()


def test_empty_query_does_not_crash(monkeypatch):
    s, conn = _seed(monkeypatch)
    assert s.recall(None, "", mode="lexical", match="any") == []
    conn.close()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
