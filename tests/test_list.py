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
    # Captions are not what most of this suite is about; the requirement
    # has its own file (test_require_title.py) covering both settings.
    monkeypatch.setenv("MEMGRES_REQUIRE_TITLE", "false")
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
    assert set(rows[0]) == {"id", "path", "tags", "title", "preview",
                            "created_at", "updated_at", "space_id", "space",
                            "recalled", "gets"}


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
    # Captions are not what most of this suite is about; the requirement
    # has its own file (test_require_title.py) covering both settings.
    monkeypatch.setenv("MEMGRES_REQUIRE_TITLE", "false")
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

def test_bodies_reads_a_whole_subtree_in_one_call(store):
    store.write(body="alpha body\nsecond line\n", path="decisions.a")
    store.write(body="beta body\n", path="decisions.b")

    rows = store.list(None, path_prefix="decisions", bodies=True)
    assert [r["body"] for r in rows] == ["alpha body\nsecond line\n", "beta body\n"]
    assert all(r["body_omitted"] is False for r in rows)
    assert "preview" not in rows[0]        # one view of the text, never both


def test_bodies_past_the_cap_are_announced_not_dropped(store, monkeypatch):
    store.cfg = dataclasses.replace(store.cfg, list_bodies_max_bytes=20)
    store.write(body="x" * 15 + "\n", path="t.a")
    store.write(body="y" * 15 + "\n", path="t.b")
    store.write(body="z" * 15 + "\n", path="t.c")

    rows = store.list(None, path_prefix="t", bodies=True)
    assert len(rows) == 3                          # every row still comes back
    assert rows[0]["body"] is not None and rows[0]["body_omitted"] is False
    assert [r["body_omitted"] for r in rows[1:]] == [True, True]
    assert all(r["body"] is None for r in rows[1:])


def test_a_first_body_larger_than_the_cap_still_comes_back(store):
    store.cfg = dataclasses.replace(store.cfg, list_bodies_max_bytes=5)
    store.write(body="a much longer body than the cap\n", path="t.a")

    [row] = store.list(None, path_prefix="t", bodies=True)
    assert row["body"].startswith("a much longer") and row["body_omitted"] is False


def test_bodies_over_the_cap_are_never_fetched(store):
    """The cap has to bound the SERVER, not just the answer. Selecting every
    body and discarding the overflow afterwards still moves every byte — at the
    default ceilings a 500-row page is 128 MB fetched to return 200 KB. So the
    page carries sizes, and only the bodies that fit are asked for."""
    store.cfg = dataclasses.replace(store.cfg, list_bodies_max_bytes=20)
    for name in ("a", "b", "c"):
        store.write(body=name * 15 + "\n", path=f"t.{name}")

    seen = []
    real_execute = store._conn.cursor

    class _Spy:
        def __init__(self, inner): self._inner = inner
        def __getattr__(self, k): return getattr(self._inner, k)
        def execute(self, sql, args=None):
            seen.append((sql, args))
            return self._inner.execute(sql, args)

    store._conn.cursor = lambda *a, **kw: _Spy(real_execute(*a, **kw))
    try:
        rows = store.list(None, path_prefix="t", bodies=True)
    finally:
        store._conn.cursor = real_execute

    assert [r["body_omitted"] for r in rows] == [False, True, True]
    # the page query asks for sizes, not bodies
    page = next(sql for sql, _ in seen if "octet_length(body)" in sql)
    assert "left(split_part" not in page
    # and exactly one id — the row that fits — has its body fetched
    fetch = [a for sql, a in seen if "SELECT id, body FROM memory" in sql]
    assert len(fetch) == 1 and fetch[0][-1] == [rows[0]["id"]]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
