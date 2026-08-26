"""Retention is the deployment's policy, and it is actually enforced.

Two separate claims, and the second is the one that was missing entirely:

  * a caller cannot set or clear an expiry — there is no per-write TTL, so the
    operator's window is what every row gets;
  * an expired row is DELETED, not merely filtered out of results — including
    its vectors in an out-of-band backend, which no foreign key can cascade.
"""

import inspect
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

psycopg = pytest.importorskip("psycopg")

from memgres.config import load  # noqa: E402
from memgres.embeddings import Embedder  # noqa: E402
from memgres.periodic import RetentionSweeper, maybe_start_sweeper  # noqa: E402
from memgres.schema import migrate  # noqa: E402
from memgres.store import Store  # noqa: E402

DSN = os.environ.get("MEMGRES_TEST_DSN",
                     "postgresql://memgres:memgres@localhost:55432/memgres")
QURL = os.environ.get("MEMGRES_TEST_QDRANT", "http://localhost:56333")
COLL = "memgres_retention_test"


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


def _pg_reachable() -> bool:
    try:
        psycopg.connect(DSN, connect_timeout=2).close()
        return True
    except Exception:
        return False


def _qdrant_reachable() -> bool:
    if not _pg_reachable():
        return False
    try:
        import urllib.request
        urllib.request.urlopen(f"{QURL}/collections", timeout=2)
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _pg_reachable(), reason="no test Postgres")


def _fresh_pg():
    with psycopg.connect(DSN, autocommit=True) as c, c.cursor() as cur:
        cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")


def _env(monkeypatch, **extra):
    for k in list(os.environ):
        if k.startswith("MEMGRES_") or k == "QDRANT_URL":
            monkeypatch.delenv(k, raising=False)
    # Captions are not what most of this suite is about; the requirement
    # has its own file (test_require_title.py) covering both settings.
    monkeypatch.setenv("MEMGRES_REQUIRE_TITLE", "false")
    monkeypatch.setenv("MEMGRES_DATABASE_URL", DSN)
    monkeypatch.setenv("MEMGRES_FTS_LANGUAGE", "simple")
    monkeypatch.setenv("MEMGRES_EMBED_PROVIDER", "none")
    for k, v in extra.items():
        monkeypatch.setenv(k, v)


def _store(monkeypatch, **extra):
    _fresh_pg()
    _env(monkeypatch, **extra)
    conn = psycopg.connect(DSN)
    cfg = load()
    migrate(conn, cfg)
    return Store(cfg, conn=conn)


# ─── the caller has no TTL knob at all ───────────────────────────────────────
def test_write_takes_no_per_call_ttl():
    """The retention window is the operator's promise about how long client data
    is kept. A caller-settable TTL made it advisory — `0` meant "keep forever",
    a large value outran the policy — so the parameter is gone, not validated."""
    assert "ttl_days" not in inspect.signature(Store.write).parameters


def test_the_mcp_write_tool_advertises_no_ttl(monkeypatch):
    pytest.importorskip("mcp")
    pytest.importorskip("psycopg_pool")
    from memgres.mcp_server import build_server
    _fresh_pg()
    _env(monkeypatch, MEMGRES_EMBED_WORKER="false")
    mcp = build_server()
    tool = getattr(getattr(mcp, "_tool_manager", None), "_tools", {})["memory_write"]
    assert "ttl_days" not in tool.parameters["properties"]


# ─── retention off: nothing ever gets an expiry, and no edit invents one ─────
def test_retention_off_leaves_every_row_immortal(monkeypatch):
    s = _store(monkeypatch)                       # RETENTION_DAYS defaults to 0
    m = s.write(body="one", path="a.b")
    assert m.expires_at is None
    edited = s.write(id=m.id, body="two")
    assert edited.expires_at is None
    s._conn.close()


# ─── retention on: every write stamps the policy window, an edit slides it ───
def test_retention_on_stamps_and_slides_the_window(monkeypatch):
    s = _store(monkeypatch, MEMGRES_RETENTION_DAYS="2")
    m = s.write(body="one", path="a.b")
    assert m.expires_at is not None               # the policy applied itself
    first = m.expires_at
    edited = s.write(id=m.id, body="two")
    # "expire N days after last touch" — the documented sliding window. The edit
    # must move it FORWARD; the bug this replaces cleared it instead.
    assert edited.expires_at is not None and edited.expires_at > first
    s._conn.close()


# ─── expired means gone, not hidden ──────────────────────────────────────────
def _expire(s, mid):
    with s._conn.cursor() as cur:
        cur.execute("UPDATE memory SET expires_at = now() - interval '1 day' "
                    "WHERE id=%s", (mid,))
    s._conn.commit()


def test_purge_deletes_the_row_not_just_the_result(monkeypatch):
    s = _store(monkeypatch, MEMGRES_RETENTION_DAYS="2")
    m = s.write(body="apple", path="a.b")
    _expire(s, m.id)
    # Already invisible to reads — that is `build_filters`, and it is NOT
    # retention: the data is still held.
    assert s.recall(None, "apple") == []
    with s._conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM memory WHERE id=%s", (m.id,))
        assert cur.fetchone()[0] == 1             # hidden, still there

    assert s.purge_expired() == 1
    with s._conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM memory WHERE id=%s", (m.id,))
        assert cur.fetchone()[0] == 0             # now actually gone
    s._conn.close()


def test_purge_spares_the_unexpired(monkeypatch):
    s = _store(monkeypatch, MEMGRES_RETENTION_DAYS="2")
    doomed = s.write(body="one", path="a.doomed")
    keep = s.write(body="two", path="a.keep")
    _expire(s, doomed.id)
    assert s.purge_expired() == 1
    assert s.get(None, keep.id).id == keep.id
    s._conn.close()


# ─── the out-of-band backend is the half a foreign key cannot cover ──────────
@pytest.mark.skipif(not _qdrant_reachable(), reason="no test Qdrant")
def test_purge_takes_qdrant_chunks_with_it(monkeypatch):
    """pgvector segments cascade on the FK; qdrant points do not. Left behind
    they carry no body, keep taking candidate slots, and are then dropped when
    `fetch_hit_rows` finds no row — recall thinning out with nothing raised."""
    pytest.importorskip("qdrant_client")
    from qdrant_client import QdrantClient
    from qdrant_client.models import Filter, FieldCondition, MatchValue

    _fresh_pg()
    qc = QdrantClient(url=QURL)
    for coll in (COLL, f"{COLL}_segments"):
        if qc.collection_exists(coll):
            qc.delete_collection(coll)
    _env(monkeypatch,
         MEMGRES_EMBED_PROVIDER="openai", MEMGRES_EMBED_MODEL="stub",
         MEMGRES_EMBED_DIM="3", MEMGRES_EMBED_API_KEY="x",
         MEMGRES_VECTOR_BACKEND="qdrant", QDRANT_URL=QURL,
         MEMGRES_QDRANT_COLLECTION=COLL, MEMGRES_RETENTION_DAYS="2",
         MEMGRES_SNIPPET_SEG_CHARS="30", MEMGRES_SNIPPET_SEG_OVERLAP="0")
    conn = psycopg.connect(DSN)
    cfg = load()
    migrate(conn, cfg)
    s = Store(cfg, embedder=_Keyword(), conn=conn)

    m = s.write(body="apple. " * 20, path="a.b")

    def _points() -> int:
        return qc.count(
            f"{COLL}_segments",
            count_filter=Filter(must=[FieldCondition(
                key="memory_id", match=MatchValue(value=str(m.id)))]),
            exact=True).count

    assert _points() > 0                          # indexed to begin with
    _expire(s, m.id)
    assert s.purge_expired() == 1
    assert _points() == 0                         # and swept out with the row
    conn.close()


# ─── the sweeper runs on its own policy, not on the embed worker's ───────────
def test_no_sweeper_when_the_deployment_keeps_everything(monkeypatch):
    _fresh_pg()
    _env(monkeypatch)                             # RETENTION_DAYS = 0
    assert maybe_start_sweeper(load(), lambda: psycopg.connect(DSN)) is None


def test_sweeper_starts_and_sweeps_without_any_embedder(monkeypatch):
    """A lexical-only deployment has no embed worker. Retention must still run —
    otherwise the promise fails silently because an unrelated feature is off."""
    s = _store(monkeypatch, MEMGRES_RETENTION_DAYS="2")
    m = s.write(body="one", path="a.b")
    _expire(s, m.id)

    sweeper = RetentionSweeper(load(), lambda: psycopg.connect(DSN))
    try:
        assert sweeper.sweep_once() == 1
    finally:
        sweeper.stop()
    with s._conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM memory WHERE id=%s", (m.id,))
        assert cur.fetchone()[0] == 0
    s._conn.close()


def test_a_process_can_be_told_not_to_sweep(monkeypatch):
    """Every server process starts a sweeper, and in a stdio-MCP deployment a
    process is a client session — a dozen sessions would mean a dozen threads
    issuing the same deployment-wide DELETE. Harmless but redundant, so a
    deployment with a dedicated sweeper can silence the rest."""
    _fresh_pg()
    _env(monkeypatch, MEMGRES_RETENTION_DAYS="2", MEMGRES_RETENTION_SWEEP="false")
    assert maybe_start_sweeper(load(), lambda: psycopg.connect(DSN)) is None
    _env(monkeypatch, MEMGRES_RETENTION_DAYS="2")
    s = maybe_start_sweeper(load(), lambda: psycopg.connect(DSN))
    assert s is not None
    s.stop()


def test_one_sweep_is_bounded(monkeypatch):
    """A large expired backlog must not become one long transaction holding row
    locks; the sweeper simply comes back."""
    s = _store(monkeypatch, MEMGRES_RETENTION_DAYS="2")
    for i in range(5):
        m = s.write(body=f"body {i}", path=f"a.n{i}")
        _expire(s, m.id)
    assert s.purge_expired(limit=2) == 2
    assert s.purge_expired(limit=2) == 2
    assert s.purge_expired(limit=2) == 1
    assert s.purge_expired(limit=2) == 0
    s._conn.close()
