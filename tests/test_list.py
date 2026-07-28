"""store.list: enumerate a subtree (browse, not search).

Skips unless MEMGRES_TEST_DSN (or the default local pgvector) is reachable.
"""

import dataclasses
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

psycopg = pytest.importorskip("psycopg")

from memgres.config import load  # noqa: E402
from memgres.schema import migrate  # noqa: E402
from memgres import identity as ident  # noqa: E402
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


@pytest.fixture
def store(monkeypatch):
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


def test_lists_subtree_ordered_by_path(store):
    store.write(body="gamma\n", path="decisions.c")
    store.write(body="alpha\n", path="decisions.a")
    store.write(body="beta\n", path="decisions.b")
    store.write(body="elsewhere\n", path="ops.x")

    rows = store.list(None, path_prefix="decisions")
    assert [r["path"] for r in rows] == ["decisions.a", "decisions.b", "decisions.c"]
    # the ops row is NOT in the decisions subtree
    assert all(r["path"].startswith("decisions") for r in rows)
    # shape of each row
    assert set(rows[0]) == {"id", "path", "tags", "preview", "created_at", "updated_at"}


def test_preview_is_first_line_truncated(store):
    store.write(body="first line here\nsecond line\nthird\n", path="root.a")
    [row] = store.list(None, path_prefix="root")
    assert row["preview"] == "first line here"   # only the first line, no newline

    # configured length truncates the first line
    monkeypatched = dataclasses.replace(store.cfg, list_preview_chars=5)
    store.cfg = monkeypatched
    [row] = store.list(None, path_prefix="root")
    assert row["preview"] == "first"             # first line cut to 5 chars


def test_tags_filter_narrows(store):
    store.write(body="a\n", path="t.a", tags=["keep"])
    store.write(body="b\n", path="t.b", tags=["drop"])
    store.write(body="c\n", path="t.c", tags=["keep", "extra"])

    rows = store.list(None, path_prefix="t", tags=["keep"])
    assert sorted(r["path"] for r in rows) == ["t.a", "t.c"]


def test_limit_and_offset_paginate(store):
    for i in range(5):
        store.write(body=f"body {i}\n", path=f"p.n{i}")
    page1 = store.list(None, path_prefix="p", limit=2, offset=0)
    page2 = store.list(None, path_prefix="p", limit=2, offset=2)
    page3 = store.list(None, path_prefix="p", limit=2, offset=4)
    assert [r["path"] for r in page1] == ["p.n0", "p.n1"]
    assert [r["path"] for r in page2] == ["p.n2", "p.n3"]
    assert [r["path"] for r in page3] == ["p.n4"]


# ─── multi-tenant isolation ──────────────────────────────────────────────────
@pytest.fixture
def managed(monkeypatch):
    with psycopg.connect(DSN, autocommit=True) as c, c.cursor() as cur:
        cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    for k in list(os.environ):
        if k.startswith("MEMGRES_"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("MEMGRES_DATABASE_URL", DSN)
    monkeypatch.setenv("MEMGRES_EMBED_PROVIDER", "none")
    monkeypatch.setenv("MEMGRES_FTS_LANGUAGE", "simple")
    base = load()
    setup = psycopg.connect(DSN, autocommit=True)
    migrate(setup, base)
    cfg = dataclasses.replace(base, key_mode="managed")
    s = Store(cfg, conn=psycopg.connect(DSN))
    s._own_conn = True
    yield setup, s
    s.close()
    setup.close()


def _user_with_token(setup, name, namespace):
    uid = ident.create_user(setup, name=name)
    nsid = ident.create_namespace(setup, uid, namespace)
    secret, _ = ident.issue_token(setup, uid, namespace_id=nsid, permission="write")
    return uid, secret, nsid


def test_list_isolated_between_tenants(managed):
    setup, s = managed
    _, alice_tok, _ = _user_with_token(setup, "alice", namespace="n")
    _, bob_tok, _ = _user_with_token(setup, "bob", namespace="n")

    s.write(alice_tok, body="alice secret\n", path="shared.doc", space="n")
    s.write(bob_tok, body="bob secret\n", path="shared.doc", space="n")

    alice_rows = s.list(alice_tok, path_prefix="shared", space="n")
    bob_rows = s.list(bob_tok, path_prefix="shared", space="n")

    assert [r["preview"] for r in alice_rows] == ["alice secret"]
    assert [r["preview"] for r in bob_rows] == ["bob secret"]
    # never each other's rows
    assert all("bob" not in r["preview"] for r in alice_rows)
    assert all("alice" not in r["preview"] for r in bob_rows)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
