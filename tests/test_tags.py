"""Tags: one spelling, a visible vocabulary, and a choice of ALL or ANY.

Tags are compared byte-for-byte by a GIN index, which made every difference in
case or Unicode form a second, silently unrelated label — 265 distinct tags
across 97 memories in this repo's own corpus, with filtering useless not because
the index was broken but because nothing agreed on the spelling.

Three claims here: normalisation happens on BOTH sides (a filter written one way
finds a row stored another), rows written before normalisation are brought along
by the migration, and a writer can SEE the vocabulary instead of inventing one.
"""

import os
import sys
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

psycopg = pytest.importorskip("psycopg")

from memgres.config import load  # noqa: E402
from memgres.schema import migrate  # noqa: E402
from memgres.store import Store  # noqa: E402
from memgres.tags import normalize_tag, normalize_tags  # noqa: E402

DSN = os.environ.get("MEMGRES_TEST_DSN",
                     "postgresql://memgres:memgres@localhost:55432/memgres")

# The same letter, two Unicode spellings: composed U+0439, and "и" + a combining
# breve. Editors emit both; they render identically and compare unequal.
Y_COMPOSED = "й"
Y_DECOMPOSED = "й"


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


# ─── the normaliser itself ───────────────────────────────────────────────────
def test_case_and_unicode_form_collapse():
    assert normalize_tag("X402") == "x402"
    assert normalize_tag("  spaced  ") == "spaced"
    assert (normalize_tag("сло" + Y_DECOMPOSED) ==
            normalize_tag("Сло" + Y_COMPOSED))


def test_normalising_dedupes_but_keeps_the_caller_s_order():
    assert normalize_tags(["Beta", "alpha", "BETA", "  ", "Alpha"]) == \
        ["beta", "alpha"]


def test_none_and_empty_stay_distinct():
    """`tags=None` means "leave them alone" on an edit and `[]` means "clear
    them" — collapsing the two would make a metadata-free edit wipe the tags."""
    assert normalize_tags(None) is None
    assert normalize_tags([]) == []


# ─── both sides, or it is not normalisation ──────────────────────────────────
def test_a_tag_is_stored_normalised(store):
    m = store.write(body="one", tags=["X402", "Ops"])
    assert m.tags == ["x402", "ops"]


def test_a_filter_written_in_any_spelling_finds_it(store):
    m = store.write(body="one", tags=["x402"])
    # Query side normalised too — otherwise fixing storage alone would break
    # every filter that used to work.
    assert [h.id for h in store.recall(None, "one", tags=["X402"])] == [m.id]
    assert [r["id"] for r in store.list(None, tags=["  X402  "])] == [m.id]


def test_the_two_unicode_spellings_are_one_tag(store):
    m = store.write(body="one", tags=["сло" + Y_DECOMPOSED])
    hits = store.recall(None, "one", tags=["сло" + Y_COMPOSED])
    assert [h.id for h in hits] == [m.id]


def test_normalising_does_not_leave_a_duplicate_in_one_row(store):
    m = store.write(body="one", tags=["Ops", "OPS", "ops"])
    assert m.tags == ["ops"]


# ─── ALL vs ANY ──────────────────────────────────────────────────────────────
def test_all_is_the_default_and_any_is_opt_in(store):
    both = store.write(body="one", tags=["a", "b"])
    just_a = store.write(body="two", tags=["a"])

    assert [h.id for h in store.recall(None, "one two", tags=["a", "b"])] == [both.id]
    got = {h.id for h in store.recall(None, "one two", tags=["a", "b"],
                                      match_tags="any")}
    assert got == {both.id, just_a.id}


def test_browse_takes_the_same_choice(store):
    both = store.write(body="one", tags=["a", "b"])
    just_a = store.write(body="two", tags=["a"])
    assert [r["id"] for r in store.list(None, tags=["a", "b"])] == [both.id]
    assert {r["id"] for r in store.list(None, tags=["a", "b"], match_tags="any")} \
        == {both.id, just_a.id}


def test_an_unknown_match_mode_is_refused(store):
    with pytest.raises(ValueError, match="tag match"):
        store.recall(None, "one", tags=["a"], match_tags="either")


# ─── the vocabulary is visible ───────────────────────────────────────────────
def test_tags_lists_what_is_in_use_most_used_first(store):
    store.write(body="one", tags=["ops", "x402"])
    store.write(body="two", tags=["ops"])
    store.write(body="three", tags=["misc"])
    rows = store.tags(None)
    assert rows[0] == {"tag": "ops", "count": 2}
    assert {r["tag"] for r in rows} == {"ops", "x402", "misc"}


def test_the_vocabulary_narrows_by_prefix_and_caps(store):
    store.write(body="one", tags=["ops.deploy", "ops.listing", "misc"])
    assert {r["tag"] for r in store.tags(None, prefix="ops")} == \
        {"ops.deploy", "ops.listing"}
    assert len(store.tags(None, k=1)) == 1


def test_the_vocabulary_is_normalised_before_matching_a_prefix(store):
    store.write(body="one", tags=["Ops"])
    assert [r["tag"] for r in store.tags(None, prefix="OPS")] == ["ops"]


def test_an_expired_memory_contributes_no_tags(store):
    m = store.write(body="one", tags=["ghost"])
    with store._conn.cursor() as cur:
        cur.execute("UPDATE memory SET expires_at = now() - interval '1 day' "
                    "WHERE id=%s", (m.id,))
    store._conn.commit()
    assert store.tags(None) == []


# ─── rows written before normalisation are brought along ─────────────────────
def test_the_migration_normalises_what_was_already_stored(store):
    """Fixing writes alone would leave the old spellings stranded: a filter that
    normalises would stop matching the very rows it used to find."""
    m = store.write(body="one", tags=["x402"])
    # Reach behind the store to plant pre-normalisation spellings, including two
    # that collapse to the same tag.
    with store._conn.cursor() as cur:
        cur.execute("UPDATE memory SET tags = %s WHERE id=%s",
                    (["X402", "x402", "  Ops  ", "сло" + Y_DECOMPOSED], m.id))
    store._conn.commit()

    from memgres.schema import migrate as _migrate
    _migrate(store._conn, store.cfg)          # migrations re-apply on every start

    with store._conn.cursor() as cur:
        cur.execute("SELECT tags FROM memory WHERE id=%s", (m.id,))
        tags = cur.fetchone()[0]
    assert sorted(tags) == sorted(["x402", "ops",
                                   unicodedata.normalize("NFC", "сло" + Y_DECOMPOSED)])
    assert [h.id for h in store.recall(None, "one", tags=["X402"])] == [m.id]


# ─── requests that mean nothing must not mean two different things ───────────
def test_a_tag_request_that_normalises_to_nothing_filters_nothing(store):
    """Left to the operators this is the same input meaning opposite things:
    `tags @> '{}'` is true of every row and `tags && '{}'` is true of none, so a
    filter of blanks would return everything or nothing depending on a mode flag
    the caller may not even have set. Neither is an answer."""
    a = store.write(body="alpha", tags=["keep"])
    b = store.write(body="beta", tags=["other"])
    for mode in ("all", "any"):
        got = {h.id for h in store.recall(None, "alpha beta", tags=["   ", ""],
                                          match_tags=mode)}
        assert got == {a.id, b.id}, mode
    assert {r["id"] for r in store.list(None, tags=[])} == {a.id, b.id}


# ─── a narrowed answer must actually be narrowed ─────────────────────────────
def test_like_wildcards_in_a_prefix_are_literal(store):
    """`_` and `%` are LIKE metacharacters and a tag is ordinary text. Unescaped,
    prefix="ops_" also matches "opsx" — a wrong answer dressed as a narrowed one."""
    store.write(body="x", tags=["opsx"])
    store.write(body="y", tags=["ops.deploy"])
    store.write(body="z", tags=["ops_run"])
    assert [r["tag"] for r in store.tags(None, prefix="ops_")] == ["ops_run"]
    assert store.tags(None, prefix="%") == []


# ─── a re-run must be a no-op, and order is part of the value ────────────────
def test_the_migration_keeps_the_writer_s_order(store):
    """`normalize_tags` preserves first-seen order, so the migration must too.
    Sorting instead would make stored tags diverge from `memory_history.
    tags_after` — with `verify_history` still reporting True, so nothing would
    flag it."""
    m = store.write(body="one", tags=["Zeta", "Alpha"])
    assert m.tags == ["zeta", "alpha"]

    from memgres.schema import migrate
    migrate(store._conn, store.cfg)
    store._conn.commit()
    with store._conn.cursor() as cur:
        cur.execute("SELECT tags FROM memory WHERE id=%s", (m.id,))
        assert cur.fetchone()[0] == ["zeta", "alpha"]


def test_re_running_the_migration_writes_nothing(store):
    """Migrations re-apply on EVERY start, and the docstring promises a re-run is
    a no-op. A migration that rewrote rows each boot would also read a client's
    unchanged tag list as a change: phantom `retag`, seq bump, expiry renewal —
    and then flip it back on the next boot, forever."""
    from memgres.schema import migrate
    m = store.write(body="one", tags=["Zeta", "Alpha", "ZETA"])
    with store._conn.cursor() as cur:
        cur.execute("SELECT xmin::text FROM memory WHERE id=%s", (m.id,))
        before = cur.fetchone()[0]

    migrate(store._conn, store.cfg)
    migrate(store._conn, store.cfg)
    store._conn.commit()
    with store._conn.cursor() as cur:
        cur.execute("SELECT xmin::text, tags FROM memory WHERE id=%s", (m.id,))
        after, tags = cur.fetchone()
    # xmin changes only if the row was actually rewritten.
    assert after == before, "the migration rewrote a row that was already canonical"
    assert tags == ["zeta", "alpha"]


def test_a_denormalised_row_is_fixed_in_place_keeping_order(store):
    from memgres.schema import migrate
    m = store.write(body="one", tags=["zeta"])
    with store._conn.cursor() as cur:
        cur.execute("UPDATE memory SET tags = %s WHERE id=%s",
                    (["Zeta", "  ALPHA ", "zeta"], m.id))
    store._conn.commit()

    migrate(store._conn, store.cfg)
    store._conn.commit()
    with store._conn.cursor() as cur:
        cur.execute("SELECT tags FROM memory WHERE id=%s", (m.id,))
        assert cur.fetchone()[0] == ["zeta", "alpha"]      # first-seen order
