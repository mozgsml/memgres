"""Addressing a memory by its path, and what happens when the path is stale.

A path is unique within a namespace, so it is a real address — the one a person
or an agent actually knows (`decisions.x402.payai`), as opposed to a uuid they
have to look up first. But unlike a uuid it MOVES, and that is what these tests
are about.

The failure being designed against: a caller writes to the address a memory used
to live at, meaning to update it. Nothing is there any more, so a plain create
succeeds — and now there are two memories on one subject, the caller keeps
writing to the ghost, and no error is ever raised. That is the shape of every
expensive bug in this system: not a crash, a quiet divergence.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

psycopg = pytest.importorskip("psycopg")

from memgres.config import load  # noqa: E402
from memgres.schema import migrate  # noqa: E402
from memgres.store import (  # noqa: E402
    NotFound, PathMoved, PathTaken, Store,
)

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


# ─── the address works ───────────────────────────────────────────────────────
def test_read_and_edit_by_path(store):
    m = store.write(body="one\n", path="ops.postgres", title="PG")

    assert store.get(None, at="ops.postgres").id == m.id
    assert store.history(None, at="ops.postgres")[0]["op"] == "create"

    edited = store.write(at="ops.postgres", body="two\n")
    assert edited.id == m.id and edited.body == "two\n"
    assert edited.created is False


def test_at_and_id_are_the_same_address(store):
    m = store.write(body="x\n", path="ops.a")
    assert store.get(None, m.id).id == store.get(None, at="ops.a").id
    with pytest.raises(ValueError):
        store.get(None, m.id, at="ops.a")          # two addresses, one memory
    with pytest.raises(ValueError):
        store.get(None)                            # no address at all


def test_a_path_that_never_existed_is_not_found(store):
    with pytest.raises(NotFound):
        store.get(None, at="nothing.here")


# ─── `at` finds, `path` files: the two never blur ────────────────────────────
def test_creating_at_an_occupied_path_names_the_occupant(store):
    first = store.write(body="mine\n", path="ops.a")
    with pytest.raises(PathTaken) as e:
        store.write(body="also mine\n", path="ops.a")
    assert e.value.memory_id == first.id
    # the occupant is untouched — a refused create must not be a silent overwrite
    assert store.get(None, first.id).body == "mine\n"


def test_at_plus_path_is_a_move(store):
    m = store.write(body="x\n", path="ops.a")
    moved = store.write(at="ops.a", path="ops.b")
    assert moved.id == m.id and moved.path == "ops.b"


# ─── the stale address: the case this exists for ─────────────────────────────
def test_writing_to_a_vacated_path_is_refused_and_says_where_it_went(store):
    m = store.write(body="the real one\n", path="ops.old")
    store.move(None, m.id, "ops.new")

    with pytest.raises(PathMoved) as e:
        store.write(body="update, I thought\n", path="ops.old")
    assert e.value.moved_to == "ops.new" and e.value.memory_id == m.id
    assert "ops.new" in str(e.value)

    # crucially: no second memory was made
    assert [r["path"] for r in store.list(None)] == ["ops.new"]


def test_editing_a_vacated_path_is_refused_by_default_and_follows_on_request(store):
    m = store.write(body="one\n", path="ops.old")
    store.move(None, m.id, "ops.new")

    with pytest.raises(PathMoved):
        store.write(at="ops.old", body="two\n")

    edited = store.write(at="ops.old", body="two\n", if_moved="follow")
    assert edited.id == m.id and edited.body == "two\n"
    assert edited.moved_from == "ops.old"          # the answer says the address moved


def test_a_read_follows_a_move_and_says_so(store):
    m = store.write(body="one\n", path="ops.old")
    store.move(None, m.id, "ops.new")

    got = store.get(None, at="ops.old")
    assert got.id == m.id
    assert got.moved_from == "ops.old" and got.path == "ops.new"
    assert got.to_dict()["moved_from"] == "ops.old"
    # a reader who would rather be told than redirected can ask for that
    with pytest.raises(PathMoved):
        store.get(None, at="ops.old", if_moved="error")


def test_a_vacated_path_can_be_deliberately_reclaimed(store):
    m = store.write(body="the real one\n", path="ops.old")
    store.move(None, m.id, "ops.new")

    fresh = store.write(body="something else entirely\n", path="ops.old",
                        if_moved="create")
    assert fresh.id != m.id and fresh.created is True
    # both live, each at its own address
    assert sorted(r["path"] for r in store.list(None)) == ["ops.new", "ops.old"]
    # and the reclaimed path now resolves to the NEW memory, not through history
    got = store.get(None, at="ops.old")
    assert got.id == fresh.id and got.moved_from is None


def test_a_descendant_of_a_moved_subtree_resolves_by_its_old_path(store):
    """The reason a cascaded move records history on every node: without it only
    the explicitly-moved node's old address could be resolved, and a subtree move
    would silently strand every path beneath it."""
    root = store.write(body="root\n", path="a")
    child = store.write(body="child\n", path="a.b.c")
    store.move(None, root.id, "z")

    got = store.get(None, at="a.b.c")
    assert got.id == child.id and got.path == "z.b.c"
    with pytest.raises(PathMoved) as e:
        store.write(body="dupe\n", path="a.b.c")
    assert e.value.moved_to == "z.b.c"


def test_a_deleted_memory_frees_its_path_outright(store):
    """Erasure is real here — history cascades with the row — so a deleted
    memory leaves no redirect. That is correct rather than unfortunate: with
    nothing left to fork from, the address is simply free."""
    m = store.write(body="gone\n", path="ops.old")
    assert store.forget(None, m.id) is True

    fresh = store.write(body="new tenant\n", path="ops.old")   # no flag needed
    assert fresh.created is True and fresh.path == "ops.old"


def test_the_most_recent_departure_wins(store):
    """Two memories can have lived at one path over time. The redirect must
    point at the one that left LAST, not the first ever recorded."""
    first = store.write(body="first\n", path="ops.slot")
    store.move(None, first.id, "ops.first")
    second = store.write(body="second\n", path="ops.slot", if_moved="create")
    store.move(None, second.id, "ops.second")

    got = store.get(None, at="ops.slot")
    assert got.id == second.id and got.path == "ops.second"


def test_a_memory_that_moved_back_is_found_live(store):
    """A live path always wins over any redirect — including the memory's own."""
    m = store.write(body="x\n", path="ops.a")
    store.move(None, m.id, "ops.b")
    store.move(None, m.id, "ops.a")

    got = store.get(None, at="ops.a")
    assert got.id == m.id and got.moved_from is None


# ─── the flag itself ─────────────────────────────────────────────────────────
def test_if_moved_rejects_a_value_it_does_not_know(store):
    with pytest.raises(ValueError):
        store.write(body="x\n", path="ops.a", if_moved="whatever")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
