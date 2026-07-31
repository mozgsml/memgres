"""Store lifecycle against a live pgvector Postgres.

Skips unless MEMGRES_TEST_DSN (or the default local pgvector) is reachable.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

psycopg = pytest.importorskip("psycopg")

from memgres.config import load  # noqa: E402
from memgres.diffing import make_diff, content_hash, DiffConflict  # noqa: E402
from memgres.schema import migrate  # noqa: E402
from memgres.store import Store, Conflict, NotFound, TooLarge, NoParent  # noqa: E402

DSN = os.environ.get("MEMGRES_TEST_DSN",
                     "postgresql://memgres:memgres@localhost:55432/memgres")


def _reachable() -> bool:
    try:
        psycopg.connect(DSN, connect_timeout=2).close()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _reachable(), reason="no test Postgres")


@pytest.fixture
def store(monkeypatch):
    # fresh schema each test
    with psycopg.connect(DSN, autocommit=True) as c, c.cursor() as cur:
        cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    for k in list(os.environ):
        if k.startswith("MEMGRES_"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("MEMGRES_DATABASE_URL", DSN)
    monkeypatch.setenv("MEMGRES_EMBED_PROVIDER", "none")
    monkeypatch.setenv("MEMGRES_FTS_LANGUAGE", "simple")
    cfg = load()
    conn = psycopg.connect(DSN)
    migrate(conn, cfg)
    s = Store(cfg, conn=conn)
    yield s
    conn.close()


def test_create_get_history(store):
    m = store.write(body="first line\nsecond line\n", tags=["a", "b"],
                    path="root.notes", source="unit", reason="seed")
    assert m.seq == 1 and m.content_hash == content_hash("first line\nsecond line\n")
    got = store.get(None, m.id)
    assert got.body == m.body and got.tags == ["a", "b"] and got.path == "root.notes"
    h = store.history(None, m.id)
    assert len(h) == 1 and h[0]["op"] == "create" and h[0]["source"] == "unit"


def test_diff_apply_and_occ(store):
    m = store.write(body="alpha\nbeta\n")
    new = "alpha\nBETA\n"
    d = make_diff(m.body, new)
    # correct base hash applies
    m2 = store.write(id=m.id, diff=d, base_hash=m.content_hash, reason="edit")
    assert m2.body == new and m2.seq == 2
    # stale diff (old base) rejected
    with pytest.raises(Conflict):
        store.write(id=m.id, diff=d, base_hash=m.content_hash)


def test_diff_requires_base_hash(store):
    m = store.write(body="x\n")
    with pytest.raises(ValueError, match="base_hash"):
        store.write(id=m.id, diff=make_diff("x\n", "y\n"))


def test_malformed_diff_raises_and_does_not_bump_seq(store):
    # Regression (diff_silently_noops): a diff whose hunk header doesn't parse
    # used to silently apply nothing — body unchanged but seq++ / updated_at
    # moved, looking like a successful write. Now it raises and touches nothing.
    m = store.write(body="alpha\nbeta\n")
    assert m.seq == 1
    bad = "@@\n-alpha\n+ALPHA\n"          # bare @@ — not a valid unified-diff hunk
    with pytest.raises(DiffConflict):
        store.write(id=m.id, diff=bad, base_hash=m.content_hash)
    again = store.get(None, m.id)
    assert again.body == "alpha\nbeta\n"          # body untouched
    assert again.content_hash == m.content_hash   # hash unchanged
    assert again.seq == 1                          # seq NOT bumped


def test_replace_and_retag(store):
    m = store.write(body="body\n", tags=["old"])
    m2 = store.write(id=m.id, body="new body\n", tags=["new"], reason="replace")
    assert m2.body == "new body\n" and m2.tags == ["new"] and m2.seq == 2
    ops = [r["op"] for r in store.history(None, m.id)]
    assert ops == ["create", "replace"]


def test_move_cascades_subtree(store):
    root = store.write(body="root\n", path="a")
    child = store.write(body="child\n", path="a.b")
    grand = store.write(body="grand\n", path="a.b.c")
    store.move(None, root.id, "z")
    assert store.get(None, root.id).path == "z"
    assert store.get(None, child.id).path == "z.b"       # cascaded
    assert store.get(None, grand.id).path == "z.b.c"     # cascaded deep
    assert store.history(None, root.id)[-1]["op"] == "move"


def test_hash_chain_verifies(store):
    m = store.write(body="1\n", source="s")
    store.write(id=m.id, body="2\n", reason="r2")
    store.write(id=m.id, path="p", reason="moved")
    assert store.verify_history(None, m.id) is True


def test_forget_erases_history(store):
    m = store.write(body="secret\n")
    assert store.forget(None, m.id) is True
    with pytest.raises(NotFound):
        store.get(None, m.id)
    with pytest.raises(NotFound):
        store.history(None, m.id)


def test_write_size_limit(store):
    store.cfg = store.cfg.__class__(**{**store.cfg.__dict__, "max_write_bytes": 8})
    with pytest.raises(TooLarge):
        store.write(body="this is definitely longer than eight bytes\n")


def test_sparse_paths_allowed_by_default(store):
    # default: MEMGRES_REQUIRE_PARENT off — deep path with no parent row is fine
    m = store.write(body="deep\n", path="a.b.c")
    assert m.path == "a.b.c"


def test_require_parent_enforced(monkeypatch):
    with psycopg.connect(DSN, autocommit=True) as c, c.cursor() as cur:
        cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    for k in list(os.environ):
        if k.startswith("MEMGRES_"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("MEMGRES_DATABASE_URL", DSN)
    monkeypatch.setenv("MEMGRES_EMBED_PROVIDER", "none")
    monkeypatch.setenv("MEMGRES_FTS_LANGUAGE", "simple")
    monkeypatch.setenv("MEMGRES_REQUIRE_PARENT", "true")
    cfg = load(); conn = psycopg.connect(DSN); migrate(conn, cfg)
    s = Store(cfg, conn=conn)
    s.write(body="root\n", path="food")                 # root ok, no parent needed
    with pytest.raises(NoParent):
        s.write(body="orphan\n", path="food.fruit.apple")  # food.fruit missing
    s.write(body="fruit\n", path="food.fruit")          # now create the parent
    child = s.write(body="apple\n", path="food.fruit.apple")  # ok now
    assert child.path == "food.fruit.apple"
    conn.close()


def test_default_token_from_config(monkeypatch):
    """MEMGRES_TOKEN is the default identity when a call passes no token
    (single-tenant endpoints); a different token is a different user."""
    from memgres.identity import new_token, SpaceNotFound
    with psycopg.connect(DSN, autocommit=True) as c, c.cursor() as cur:
        cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    for k in list(os.environ):
        if k.startswith("MEMGRES_"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("MEMGRES_DATABASE_URL", DSN)
    monkeypatch.setenv("MEMGRES_KEY_MODE", "open")
    default_tok = new_token()
    monkeypatch.setenv("MEMGRES_TOKEN", default_tok)              # env default
    monkeypatch.setenv("MEMGRES_EMBED_PROVIDER", "none")
    monkeypatch.setenv("MEMGRES_FTS_LANGUAGE", "simple")
    cfg = load(); conn = psycopg.connect(DSN); migrate(conn, cfg)
    s = Store(cfg, conn=conn)
    # no token passed -> falls back to MEMGRES_TOKEN, so writes/reads work
    m = s.write(None, body="via default token\n")
    assert s.get(None, m.id).body == "via default token\n"
    # a different token is a different user -> can't see it
    with pytest.raises((NotFound, SpaceNotFound)):
        s.get(new_token(), m.id)
    conn.close()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
