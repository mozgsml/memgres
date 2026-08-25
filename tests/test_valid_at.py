"""`valid_at`: the day the content was last known to be ACCURATE.

`created_at`/`updated_at` answer a different question and cannot stand in for
this one. Fixing a typo moves `updated_at` without anyone having checked that the
content is still true, and a fact distilled today from a letter dated 2021 is not
fresh because the row is new. So one date, on the history row, saying how far
forward the evidence reaches.

The other half of this file is the hash chain. Adding an optional dimension must
leave every row that does not use it byte-identical — that property is what makes
the addition additive instead of a floor bump, and it is worth proving rather
than assuming.
"""

import datetime as dt
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

psycopg = pytest.importorskip("psycopg")

from memgres.config import load  # noqa: E402
from memgres.schema import migrate  # noqa: E402
from memgres.store import (OPTIONAL_DIMENSIONS, Store, _row_hash)  # noqa: E402

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
    monkeypatch.setenv("MEMGRES_REQUIRE_TITLE", "false")
    conn = psycopg.connect(DSN)
    cfg = load()
    migrate(conn, cfg)
    s = Store(cfg, conn=conn)
    yield s
    conn.close()


# ─── recording it ────────────────────────────────────────────────────────────
def test_a_write_can_say_when_its_content_was_accurate(store):
    m = store.write(body="PayAI's free tier is 1000 settles", path="ops.payai",
                    valid_at="2021-03-04")
    [row] = store.history(None, m.id)
    assert row["valid_at"] == dt.date(2021, 3, 4)


def test_omitting_it_means_as_of_now_not_unknown(store):
    """Null is the ordinary case — a routine edit is accurate as of when it was
    made. Making null mean "unknown" would turn every normal write into a gap."""
    m = store.write(body="one", path="a.b")
    [row] = store.history(None, m.id)
    assert row["valid_at"] is None


def test_the_date_may_point_into_the_past(store):
    """A fact distilled today from a five-year-old letter belongs to the letter's
    date. Nothing about the sequence is monotonic and nothing should enforce it."""
    m = store.write(body="one", path="a.b", valid_at="2026-08-25")
    store.write(id=m.id, body="from an old letter", valid_at="2021-01-01")
    dates = [r["valid_at"] for r in store.history(None, m.id)]
    assert dates == [dt.date(2026, 8, 25), dt.date(2021, 1, 1)]


def test_a_date_alone_records_a_re_confirmation(store):
    """Re-checking a fact changes no content but IS an assertion. Without its own
    operation the only way to record "still true" would be a fake edit."""
    m = store.write(body="one", path="a.b")
    again = store.write(id=m.id, valid_at="2026-08-25")
    rows = store.history(None, m.id)
    assert [r["op"] for r in rows] == ["create", "revalidate"]
    assert rows[-1]["valid_at"] == dt.date(2026, 8, 25)
    assert again.body == "one"                      # body untouched
    assert store.verify_history(None, m.id) is True


def test_a_bare_touch_still_writes_no_history(store):
    m = store.write(body="one", path="a.b")
    store.write(id=m.id)
    assert [r["op"] for r in store.history(None, m.id)] == ["create"]


def test_a_malformed_date_is_refused_with_a_usable_message(store):
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        store.write(body="one", path="a.b", valid_at="4 March 2021")


def test_a_date_object_is_accepted_too(store):
    m = store.write(body="one", path="a.b", valid_at=dt.date(2021, 3, 4))
    assert store.history(None, m.id)[0]["valid_at"] == dt.date(2021, 3, 4)


# ─── the chain ───────────────────────────────────────────────────────────────
def test_the_date_is_covered_by_the_hash(store):
    """Provenance already folds into the digest (`source`, `reason`, author). A
    date that did not would look like part of the tamper-evident record while
    being freely rewritable — worse than not having it."""
    m = store.write(body="one", path="a.b", valid_at="2021-03-04")
    assert store.verify_history(None, m.id) is True

    with store._conn.cursor() as cur:
        cur.execute("UPDATE memory_history SET valid_at = %s WHERE memory_id=%s",
                    (dt.date(2026, 1, 1), m.id))
    store._conn.commit()
    assert store.verify_history(None, m.id) is False


# Digests produced by the recipe BEFORE `valid_at` existed, taken from the
# committed implementation at that commit. They are the only thing that can catch
# a change to the base recipe: comparing the current code with itself passes
# whatever it does, and a mutation that folded the new dimension unconditionally
# survived exactly that way before these constants were added.
#
# If a future change makes these fail, that change is NOT additive — it alters
# digests already written, every existing chain reads as tampered, and it needs a
# `hash_version` bump plus a compatibility floor, not a fixed test.
GOLDEN_ARGS = ("prev", "11111111-1111-1111-1111-111111111111", 2, "diff", "d",
               "hash", "a.b", ["t"], "src", "why")
GOLDEN = {
    "base": "9b98ec8213315864450da4bb5b95e731aa2ead82c6a5a5ded7af3fa7c31d0cfd",
    "title": "ef66d4896ac8809e7d0ba1c9e1df5d237ab23af2e3c927ea5f33bd862e446946",
    "author": "a9ec9dfe8255789ff2ca667fddc4a86ad8848874f909846631a609cf98eec79b",
    "v1": "bdb6df1d29cf5a20884f94db8ccbddbea2185b41bbff97a7b72d39f76d6fc3c3",
}


def test_rows_that_predate_the_dimension_hash_exactly_as_before():
    """The property that makes an optional dimension additive: a row that never
    used it must produce the digest it produced before the dimension existed.
    Break this and every chain written by an older build reads as tampered."""
    assert _row_hash(*GOLDEN_ARGS) == GOLDEN["base"]
    assert _row_hash(*GOLDEN_ARGS, title_before="a",
                     title_after="b") == GOLDEN["title"]
    assert _row_hash(*GOLDEN_ARGS, author_user_id="u1",
                     author_token_id="t1") == GOLDEN["author"]
    assert _row_hash(*GOLDEN_ARGS, version=1) == GOLDEN["v1"]


def test_passing_no_date_is_the_same_as_the_dimension_not_existing(store):
    assert _row_hash(*GOLDEN_ARGS, valid_at=None) == GOLDEN["base"]
    assert _row_hash(*GOLDEN_ARGS, valid_at=dt.date(2021, 3, 4)) != GOLDEN["base"]


def test_the_date_folds_injectively(store):
    a = _row_hash(*GOLDEN_ARGS, valid_at=dt.date(2021, 3, 4))
    b = _row_hash(*GOLDEN_ARGS, valid_at=dt.date(2021, 4, 3))
    assert a != b


def test_the_dimension_registry_is_append_only(store):
    """Order is part of the recipe: compute and verify walk this same tuple, so
    reordering or inserting in the middle changes digests already written and
    turns an untouched chain into a "tampered" one. The test states the prefix so
    a future edit has to append."""
    assert [label for label, _ in OPTIONAL_DIMENSIONS] == [
        "memgres.title.v1", "memgres.author.v1", "memgres.valid_at.v1"]


def test_a_chain_mixing_dated_and_undated_rows_verifies(store):
    m = store.write(body="one", path="a.b", valid_at="2021-03-04")
    store.write(id=m.id, body="two")                       # no date
    store.write(id=m.id, body="three", valid_at="2026-08-25")
    store.write(id=m.id, valid_at="2026-08-26")            # revalidate
    assert store.verify_history(None, m.id) is True
