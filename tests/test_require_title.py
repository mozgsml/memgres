"""A memory that stores content must carry a caption.

`title` is what names a memory in a result list and what title-weighted ranking
has to weigh, and it used to be optional — in this repo's own corpus that left
70% of memories with no caption at all. So a write that stores CONTENT now has
to supply one.

The scope is deliberate, and the two halves are tested separately:

  * content writes (create, and an edit that changes the body) require it — which
    is also how an existing corpus migrates: each memory gains a caption the next
    time someone actually edits it, with no bulk pass and nothing to schedule;
  * re-addressing (`move`) and relabelling (`retag`) do not, because they store
    no content. Requiring it there would make an untitled memory unmovable.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

psycopg = pytest.importorskip("psycopg")

from memgres.config import load  # noqa: E402
from memgres.schema import migrate  # noqa: E402
from memgres.store import MissingTitle, Store  # noqa: E402

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
def conn(monkeypatch):
    with psycopg.connect(DSN, autocommit=True) as c, c.cursor() as cur:
        cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    for k in list(os.environ):
        if k.startswith("MEMGRES_"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("MEMGRES_DATABASE_URL", DSN)
    monkeypatch.setenv("MEMGRES_FTS_LANGUAGE", "simple")
    monkeypatch.setenv("MEMGRES_EMBED_PROVIDER", "none")
    c = psycopg.connect(DSN)
    migrate(c, load())
    yield c
    c.close()


def _store(conn, monkeypatch, required: bool) -> Store:
    """A store whose deployment does — or doesn't — require captions. Building a
    second one on the SAME connection is how a corpus written before the
    requirement is modelled: the old rows are real, the new policy is live."""
    monkeypatch.setenv("MEMGRES_REQUIRE_TITLE", "true" if required else "false")
    return Store(load(), conn=conn)


# ─── the default ─────────────────────────────────────────────────────────────
def test_captions_are_required_by_default(monkeypatch):
    for k in list(os.environ):
        if k.startswith("MEMGRES_"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("MEMGRES_DATABASE_URL", DSN)
    assert load().require_title is True


# ─── create ──────────────────────────────────────────────────────────────────
def test_creating_without_a_caption_is_refused(conn, monkeypatch):
    s = _store(conn, monkeypatch, True)
    with pytest.raises(MissingTitle) as e:
        s.write(body="the OKX name limit is 25, not 30\nrest of it", path="ops.okx")
    msg = str(e.value)
    # The refusal has to carry enough to write the caption from: the caller
    # should not have to read the memory back to answer it.
    assert "ops.okx" in msg
    assert "the OKX name limit is 25, not 30" in msg
    assert "title" in msg


def test_a_caption_of_only_whitespace_is_not_a_caption(conn, monkeypatch):
    s = _store(conn, monkeypatch, True)
    with pytest.raises(MissingTitle):
        s.write(body="one", path="a.b", title="   ")


def test_creating_with_a_caption_is_fine(conn, monkeypatch):
    s = _store(conn, monkeypatch, True)
    m = s.write(body="one", path="a.b", title="A caption")
    assert m.title == "A caption"


def test_refusal_happens_before_the_write(conn, monkeypatch):
    """Refusing after storing would leave exactly the untitled memory the rule
    exists to prevent."""
    s = _store(conn, monkeypatch, True)
    with pytest.raises(MissingTitle):
        s.write(body="one", path="a.b")
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM memory")
        assert cur.fetchone()[0] == 0


# ─── editing an untitled memory: the migration path ──────────────────────────
def test_editing_the_body_of_an_untitled_memory_asks_for_a_caption(conn, monkeypatch):
    old = _store(conn, monkeypatch, False)
    m = old.write(body="written before the rule", path="a.b")
    assert m.title == ""

    now = _store(conn, monkeypatch, True)
    with pytest.raises(MissingTitle):
        now.write(id=m.id, body="edited after it")


def test_the_caption_rides_along_in_the_same_edit(conn, monkeypatch):
    """One extra argument, not an extra round trip: the same `write` that changes
    the body can supply the caption, which is what makes the migration cheap."""
    old = _store(conn, monkeypatch, False)
    m = old.write(body="written before the rule", path="a.b")

    now = _store(conn, monkeypatch, True)
    fixed = now.write(id=m.id, body="edited after it", title="Now captioned")
    assert fixed.title == "Now captioned"
    assert fixed.body == "edited after it"


def test_an_already_captioned_memory_edits_without_repeating_it(conn, monkeypatch):
    s = _store(conn, monkeypatch, True)
    m = s.write(body="one", path="a.b", title="Kept")
    edited = s.write(id=m.id, body="two")
    assert edited.title == "Kept"


# ─── writes that store no content are none of this rule's business ───────────
def test_moving_an_untitled_memory_is_allowed(conn, monkeypatch):
    old = _store(conn, monkeypatch, False)
    m = old.write(body="one", path="a.b")

    now = _store(conn, monkeypatch, True)
    moved = now.move(None, m.id, "a.c")
    assert moved.path == "a.c"


def test_retagging_an_untitled_memory_is_allowed(conn, monkeypatch):
    old = _store(conn, monkeypatch, False)
    m = old.write(body="one", path="a.b")

    now = _store(conn, monkeypatch, True)
    retagged = now.write(id=m.id, tags=["x"])
    assert retagged.tags == ["x"]


# ─── the deployment can turn it off ──────────────────────────────────────────
def test_a_deployment_may_keep_captions_optional(conn, monkeypatch):
    s = _store(conn, monkeypatch, False)
    m = s.write(body="one", path="a.b")
    assert m.title == ""


# ─── the transports map it to "your request was wrong", not "server broke" ───
def test_the_refusal_is_a_value_error(conn, monkeypatch):
    """`server.py` maps ValueError → 422. A caption that was never supplied is
    the caller's to fix, so it must not surface as a 500."""
    assert issubclass(MissingTitle, ValueError)


def test_a_create_with_no_body_blames_the_body(conn, monkeypatch):
    """Saying "no title" to someone who forgot the body sends them to fix the
    wrong thing."""
    s = _store(conn, monkeypatch, True)
    with pytest.raises(ValueError, match="needs a body"):
        s.write(path="a.b")
