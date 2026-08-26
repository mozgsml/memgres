"""How much a memory is actually used: surfacings and reads.

Two counts, because they answer different questions. Surfacing says the memory is
FINDABLE — its words match what people ask. Being fetched says it was worth
opening once found. A memory with many surfacings and no reads is noise in every
result list it appears in; one with neither is reachable only by knowing it
exists, which is how 42 of this corpus's 97 memories already sat.

The other half of this file is what counting must NOT disturb. Statistics are not
content: they must not move `updated_at`, must not bump `seq`, must not enter the
tamper-evident chain, and must never be the reason a read fails. A counter that
made `verify_history` a function of how often a memory was read would have traded
something load-bearing for something merely interesting.
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


def _env(monkeypatch, **extra):
    with psycopg.connect(DSN, autocommit=True) as c, c.cursor() as cur:
        cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    for k in list(os.environ):
        if k.startswith("MEMGRES_"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("MEMGRES_DATABASE_URL", DSN)
    monkeypatch.setenv("MEMGRES_EMBED_PROVIDER", "none")
    monkeypatch.setenv("MEMGRES_FTS_LANGUAGE", "simple")
    monkeypatch.setenv("MEMGRES_REQUIRE_TITLE", "false")
    for k, v in extra.items():
        monkeypatch.setenv(k, v)


@pytest.fixture
def store(monkeypatch):
    _env(monkeypatch)
    conn = psycopg.connect(DSN)
    cfg = load()
    migrate(conn, cfg)
    yield Store(cfg, conn=conn)
    conn.close()


def _row(store, mid) -> dict:
    """Counts as stored, read WITHOUT going through `get` — which would count."""
    with store._conn.cursor() as cur:
        cur.execute("SELECT recall_count, get_count FROM memory_usage "
                    "WHERE memory_id=%s", (mid,))
        r = cur.fetchone()
    return {"recalled": r[0], "gets": r[1]} if r else {"recalled": 0, "gets": 0}


# ─── counting reads ──────────────────────────────────────────────────────────
def test_a_fetch_is_counted_and_the_answer_includes_it(store):
    """The number comes back with the read that caused it — reporting the count
    from before would describe a state that no longer exists by the time anyone
    sees it."""
    m = store.write(body="the runbook", path="ops.deploy")
    assert store.get(None, m.id).usage["gets"] == 1
    assert store.get(None, m.id).usage["gets"] == 2
    assert _row(store, m.id)["gets"] == 2


def test_a_partial_read_still_counts(store):
    m = store.write(body="one\ntwo\nthree\n", path="ops.deploy")
    store.get(None, m.id, lines="2")
    assert _row(store, m.id)["gets"] == 1


def test_reading_by_path_counts_the_same(store):
    m = store.write(body="the runbook", path="ops.deploy")
    store.get(None, at="ops.deploy")
    assert _row(store, m.id)["gets"] == 1


def test_a_read_of_something_that_is_not_there_counts_nothing(store):
    store.write(body="the runbook", path="ops.deploy")
    with pytest.raises(Exception):
        store.get(None, at="ops.nothing")
    with store._conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM memory_usage")
        assert cur.fetchone()[0] == 0


# ─── counting surfacings ─────────────────────────────────────────────────────
def test_every_hit_that_came_back_is_counted(store):
    a = store.write(body="apple pie", path="food.a")
    b = store.write(body="apple crumble", path="food.b")
    c = store.write(body="beef stew", path="food.c")

    assert {h.id for h in store.recall(None, "apple")} == {a.id, b.id}
    assert _row(store, a.id)["recalled"] == 1
    assert _row(store, b.id)["recalled"] == 1
    assert _row(store, c.id)["recalled"] == 0     # never surfaced


def test_what_the_ranking_discarded_is_not_counted(store):
    """Counted for what came BACK, not for what was considered: a candidate the
    ranking dropped showed nobody anything, and counting it would report reach
    the memory never had."""
    ids = [store.write(body=f"apple number {i}", path=f"food.n{i}").id
           for i in range(5)]
    hits = store.recall(None, "apple", k=2)
    assert len(hits) == 2
    surfaced = {h.id for h in hits}
    for mid in ids:
        assert _row(store, mid)["recalled"] == (1 if mid in surfaced else 0)


def test_the_light_pass_counts_too(store):
    """`bodies=False` returns no text, but the memory still showed itself — it
    was named in a result list, which is the whole meaning of surfacing."""
    m = store.write(body="apple pie", path="food.a")
    store.recall(None, "apple", bodies=False)
    assert _row(store, m.id)["recalled"] == 1


def test_surfacing_and_reading_are_counted_separately(store):
    """The pair is the signal: found-often-but-never-opened is a different
    problem from never-found-at-all, and one number cannot say which."""
    m = store.write(body="apple pie", path="food.a")
    store.recall(None, "apple")
    store.recall(None, "pie")
    store.get(None, m.id)
    assert _row(store, m.id) == {"recalled": 2, "gets": 1}


# ─── what the store does to itself is not usage ──────────────────────────────
def test_the_write_path_reading_a_row_back_is_not_a_read(store):
    """A bare touch re-reads the memory internally to return it. Counting that
    would measure the store working, not the memory being used."""
    m = store.write(body="the runbook", path="ops.deploy")
    store.write(id=m.id)                      # pure touch: no content change
    assert _row(store, m.id)["gets"] == 0


# ─── what counting must not disturb ──────────────────────────────────────────
def test_counting_does_not_touch_the_memory_row(store):
    """Statistics are not content. If they moved `updated_at` or `seq`, every
    read would look like an edit — and in Postgres, writing a counter onto
    `memory` would rewrite the whole row, body included."""
    m = store.write(body="the runbook", path="ops.deploy")
    before = store.get(None, m.id, _count=False)

    for _ in range(3):
        store.get(None, m.id)
    store.recall(None, "runbook")

    after = store.get(None, m.id, _count=False)
    assert (after.seq, after.updated_at, after.content_hash) == \
           (before.seq, before.updated_at, before.content_hash)


def test_counting_stays_out_of_the_tamper_evident_chain(store):
    """Reading must not append history, and the chain must verify exactly as it
    did — otherwise `verify_history` becomes a function of how often a memory was
    read, which is not what it is for."""
    m = store.write(body="the runbook", path="ops.deploy")
    for _ in range(3):
        store.get(None, m.id)
        store.recall(None, "runbook")

    assert [r["op"] for r in store.history(None, m.id)] == ["create"]
    assert store.verify_history(None, m.id) is True


def test_a_broken_counter_does_not_break_the_read(store):
    """Best-effort, and it has to be: a read that fails because a STATISTIC could
    not be written trades something load-bearing for something interesting."""
    m = store.write(body="the runbook", path="ops.deploy")
    store._conn.commit()
    with store._conn.cursor() as cur:
        cur.execute("DROP TABLE memory_usage")
    store._conn.commit()

    got = store.get(None, m.id)               # must still answer
    assert got.body == "the runbook"
    assert got.usage is None                  # and say it has no numbers
    assert [h.id for h in store.recall(None, "runbook")] == [m.id]


def test_a_deployment_can_turn_counting_off(monkeypatch):
    """A read-only replica cannot write anywhere, and a deployment may simply not
    want one small write per read."""
    _env(monkeypatch, MEMGRES_USAGE_COUNTERS="false")
    conn = psycopg.connect(DSN)
    cfg = load()
    migrate(conn, cfg)
    assert cfg.usage_counters is False
    s = Store(cfg, conn=conn)

    m = s.write(body="apple pie", path="food.a")
    assert s.get(None, m.id).usage is None
    s.recall(None, "apple")
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM memory_usage")
        assert cur.fetchone()[0] == 0
    conn.close()


def test_erasing_a_memory_takes_its_counts_with_it(store):
    m = store.write(body="apple pie", path="food.a")
    store.get(None, m.id)
    store.forget(None, m.id)
    with store._conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM memory_usage WHERE memory_id=%s", (m.id,))
        assert cur.fetchone()[0] == 0


# ─── where the numbers are visible ───────────────────────────────────────────
def test_browsing_shows_the_counts_so_dead_weight_can_be_found(store):
    """Browse is where usage becomes actionable — it is how you find the subtree
    nobody reads. Never used is zero, not a missing key."""
    read = store.write(body="apple pie", path="food.a")
    never = store.write(body="beef stew", path="food.b")
    store.get(None, read.id)
    store.recall(None, "apple")

    rows = {r["path"]: r for r in store.list(None, path_prefix="food")}
    assert (rows["food.a"]["recalled"], rows["food.a"]["gets"]) == (1, 1)
    assert (rows["food.b"]["recalled"], rows["food.b"]["gets"]) == (0, 0)
    assert never.id                            # (kept for the reader's sake)


def test_the_answer_carries_the_numbers_through_serialisation(store):
    m = store.write(body="the runbook", path="ops.deploy")
    d = store.get(None, m.id).to_dict(stringify_dates=True)
    assert d["usage"]["gets"] == 1
    assert isinstance(d["usage"]["last_get_at"], str)
    assert d["usage"]["last_recall_at"] is None


def test_the_timestamp_is_the_moment_of_the_read(store):
    """`now()` in Postgres is the TRANSACTION's start time, so several reads in
    one transaction would all claim the same instant — and an embedded caller
    holding a transaction open would see "last read" frozen at whenever it began.
    A statistic wants the wall clock."""
    m = store.write(body="the runbook", path="ops.deploy")
    first = store.get(None, m.id).usage["last_get_at"]
    second = store.get(None, m.id).usage["last_get_at"]
    assert second > first
