"""Config-driven provenance size caps and the local-embedder max-seq override.

The store cases need a live Postgres (same skip rule as test_store_integration);
the config and max-seq wiring cases are pure unit tests (no DB, no real model).
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from memgres.config import load  # noqa: E402

psycopg = pytest.importorskip("psycopg")

from memgres.schema import migrate  # noqa: E402
from memgres.store import Store, TooLarge  # noqa: E402

DSN = os.environ.get("MEMGRES_TEST_DSN",
                     "postgresql://memgres:memgres@localhost:55432/memgres")


def _reachable() -> bool:
    try:
        psycopg.connect(DSN, connect_timeout=2).close()
        return True
    except Exception:
        return False


needs_pg = pytest.mark.skipif(not _reachable(), reason="no test Postgres")


@pytest.fixture
def store(monkeypatch):
    with psycopg.connect(DSN, autocommit=True) as c, c.cursor() as cur:
        cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    for k in list(os.environ):
        if k.startswith("MEMGRES_"):
            monkeypatch.delenv(k, raising=False)
    # Captions are not what most of this suite is about; the requirement
    # has its own file (test_require_title.py) covering both settings.
    monkeypatch.setenv("MEMGRES_REQUIRE_TITLE", "false")
    monkeypatch.setenv("MEMGRES_DATABASE_URL", DSN)
    monkeypatch.setenv("MEMGRES_EMBED_PROVIDER", "none")
    monkeypatch.setenv("MEMGRES_FTS_LANGUAGE", "simple")
    monkeypatch.setenv("MEMGRES_MAX_SOURCE_BYTES", "16")
    monkeypatch.setenv("MEMGRES_MAX_REASON_BYTES", "8")
    cfg = load()
    conn = psycopg.connect(DSN)
    migrate(conn, cfg)
    s = Store(cfg, conn=conn)
    yield s
    conn.close()


# ─── provenance size caps (need Postgres) ────────────────────────────────────
@needs_pg
def test_source_over_cap_rejected(store):
    with pytest.raises(TooLarge, match="MEMGRES_MAX_SOURCE_BYTES"):
        store.write(body="ok\n", source="x" * 17)  # cap is 16


@needs_pg
def test_reason_over_cap_rejected(store):
    with pytest.raises(TooLarge, match="MEMGRES_MAX_REASON_BYTES"):
        store.write(body="ok\n", reason="y" * 9)   # cap is 8


@needs_pg
def test_provenance_within_cap_ok(store):
    m = store.write(body="ok\n", source="x" * 16, reason="y" * 8)
    assert m.seq == 1


# ─── config validation ───────────────────────────────────────────────────────
def test_zero_source_cap_rejected(monkeypatch):
    for k in list(os.environ):
        if k.startswith("MEMGRES_"):
            monkeypatch.delenv(k, raising=False)
    # Captions are not what most of this suite is about; the requirement
    # has its own file (test_require_title.py) covering both settings.
    monkeypatch.setenv("MEMGRES_REQUIRE_TITLE", "false")
    monkeypatch.setenv("MEMGRES_MAX_SOURCE_BYTES", "0")
    with pytest.raises(ValueError, match="MAX_SOURCE_BYTES"):
        load()


# ─── local-embedder max-seq wiring (no real model) ───────────────────────────
def _install_fake_st(monkeypatch):
    """Intercept the lazy `from sentence_transformers import SentenceTransformer`
    inside _LocalEmbedder with a fake that records max_seq_length assignment."""
    import types

    class _FakeModel:
        def __init__(self, name, device=None):
            self.name = name
            self.max_seq_length = None       # None until _LocalEmbedder overrides it

        def get_sentence_embedding_dimension(self):
            return 384

    fake = types.ModuleType("sentence_transformers")
    fake.SentenceTransformer = _FakeModel
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake)
    return _FakeModel


def _cfg(monkeypatch, **kw):
    for k in list(os.environ):
        if k.startswith("MEMGRES_"):
            monkeypatch.delenv(k, raising=False)
    # Captions are not what most of this suite is about; the requirement
    # has its own file (test_require_title.py) covering both settings.
    monkeypatch.setenv("MEMGRES_REQUIRE_TITLE", "false")
    for k, v in kw.items():
        monkeypatch.setenv("MEMGRES_" + k, v)
    return load()


def test_max_seq_applied_when_set(monkeypatch):
    _install_fake_st(monkeypatch)
    from memgres.embeddings import get_embedder
    cfg = _cfg(monkeypatch, EMBED_PROVIDER="local", EMBED_MODEL="fake-model",
               EMBED_DIM="384", EMBED_MAX_SEQ="256")
    emb = get_embedder(cfg)
    assert emb._model.max_seq_length == 256


def test_max_seq_untouched_by_default(monkeypatch):
    _install_fake_st(monkeypatch)
    from memgres.embeddings import get_embedder
    cfg = _cfg(monkeypatch, EMBED_PROVIDER="local", EMBED_MODEL="fake-model",
               EMBED_DIM="384")
    emb = get_embedder(cfg)
    # default 0 => never assigned, stays at the fake's initial None
    assert emb._model.max_seq_length is None


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
