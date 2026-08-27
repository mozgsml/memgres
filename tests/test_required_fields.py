"""What a write MUST carry, and what the answer tells you about what it carried.

Two failures of the same shape sit behind this file.

The first: `source` is described to every client as mandatory — an address by
which someone else finds the original — but nothing enforced it, so a corpus
fills with assertions nobody can check. The requirement is now declarable per
deployment (`MEMGRES_REQUIRED_FIELDS=source`), because it is a policy about a
corpus, not a property of the software: a scratch database has no use for it.

The second is subtler and is why the first went unnoticed for so long. The reply
to a write did not echo the provenance it had just recorded, so four edits in a
row went out with an empty `source` and every reply looked perfectly healthy.
A required field that the answer does not confirm is a field whose absence
nobody notices. It is echoed on WRITE only: provenance belongs to the revision,
not to the memory, so a read has no business claiming one — that question is
`history`/`blame`.

Alongside them, `edits`: how often a memory has been rewritten, which is a
different question from how often it is read, and the half of "what is hot"
the usage counters were missing.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

psycopg = pytest.importorskip("psycopg")

from memgres.config import load  # noqa: E402
from memgres.schema import migrate  # noqa: E402
from memgres.store import MissingField, Store  # noqa: E402

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


def _store(conn, monkeypatch, required: str = "") -> Store:
    """A store whose deployment declares — or doesn't — extra required fields.
    Built on the SAME connection as its predecessor on purpose: that is how a
    corpus written before the policy is modelled, old rows real, new rule live."""
    monkeypatch.setenv("MEMGRES_REQUIRED_FIELDS", required)
    return Store(load(), conn=conn)


# ─── the default: nothing extra is required ──────────────────────────────────
def test_nothing_is_required_unless_a_deployment_says_so(monkeypatch):
    for k in list(os.environ):
        if k.startswith("MEMGRES_"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("MEMGRES_DATABASE_URL", DSN)
    assert load().required_fields == ()


def test_without_the_policy_a_sourceless_write_is_fine(conn, monkeypatch):
    s = _store(conn, monkeypatch, "")
    m = s.write(body="один", path="a.b", title="A")
    assert m.source is None


# ─── the refusal ─────────────────────────────────────────────────────────────
def test_creating_without_source_is_refused(conn, monkeypatch):
    s = _store(conn, monkeypatch, "source")
    with pytest.raises(MissingField) as e:
        s.write(body="лимит имени OKX — 25", path="ops.okx", title="OKX")
    msg = str(e.value)
    assert "source" in msg
    # The refusal must say what the field is FOR, or the requirement degenerates
    # into filling the box: the second attempt would read "from the email".
    assert "ADDRESS" in msg
    assert "the user said" in msg


def test_whitespace_is_not_a_source(conn, monkeypatch):
    s = _store(conn, monkeypatch, "source")
    with pytest.raises(MissingField):
        s.write(body="один", path="a.b", title="A", source="   ")


def test_an_edit_that_changes_the_body_needs_one_too(conn, monkeypatch):
    s = _store(conn, monkeypatch, "")
    s.write(body="один", path="a.b", title="A")
    strict = _store(conn, monkeypatch, "source")
    with pytest.raises(MissingField):
        strict.write(at="a.b", body="два")
    with pytest.raises(MissingField):
        strict.write(at="a.b", replace=("два", "три"))


def test_a_declared_field_can_be_something_other_than_source(conn, monkeypatch):
    s = _store(conn, monkeypatch, "reason")
    with pytest.raises(MissingField) as e:
        s.write(body="один", path="a.b", title="A", source="host:/path 2026-08-27")
    assert "reason" in str(e.value)
    s.write(body="один", path="a.b", title="A",
            source="host:/path 2026-08-27", reason="первая запись")


# ─── what is exempt ──────────────────────────────────────────────────────────
def test_moving_and_retagging_are_exempt(conn, monkeypatch):
    """They store no content. Requiring provenance for re-filing would make a
    memory harder to organise than to write — friction unrelated to the point,
    and the surest way to get the field filled with junk."""
    s = _store(conn, monkeypatch, "")
    s.write(body="один", path="a.b", title="A")
    strict = _store(conn, monkeypatch, "source")
    strict.write(at="a.b", path="a.c")                    # move
    strict.write(at="a.c", tags=["x"])                    # retag
    assert strict.get(None, at="a.c").tags == ["x"]


# ─── the echo ────────────────────────────────────────────────────────────────
def test_the_answer_confirms_the_provenance_it_recorded(conn, monkeypatch):
    s = _store(conn, monkeypatch, "source")
    m = s.write(body="один", path="a.b", title="A",
                source="192.168.1.121:/var/www/memgres 2026-08-27",
                valid_at="2026-08-01")
    assert m.source == "192.168.1.121:/var/www/memgres 2026-08-27"
    assert str(m.valid_at) == "2026-08-01"
    d = m.to_dict(stringify_dates=True)
    assert d["source"] == "192.168.1.121:/var/www/memgres 2026-08-27"
    assert d["valid_at"] == "2026-08-01"


def test_a_read_claims_no_provenance(conn, monkeypatch):
    """A memory has no single source — its revisions do. Reporting one on a read
    would attribute the whole record to whichever edit happened to be last."""
    s = _store(conn, monkeypatch, "")
    s.write(body="один", path="a.b", title="A", source="первый источник")
    s.write(at="a.b", body="два", source="второй источник")
    got = s.get(None, at="a.b")
    assert got.source is None and got.valid_at is None
    # Oldest first, as history reads.
    assert [h["source"] for h in s.history(None, at="a.b")] == [
        "первый источник", "второй источник"]


# ─── the edit counter ────────────────────────────────────────────────────────
def test_edits_count_revisions_after_the_creation(conn, monkeypatch):
    s = _store(conn, monkeypatch, "")
    s.write(body="один", path="a.b", title="A")
    assert s.get(None, at="a.b").usage["edits"] == 0
    s.write(at="a.b", body="два")
    s.write(at="a.b", body="три")
    assert s.get(None, at="a.b").usage["edits"] == 2


def test_browsing_reports_the_same_count(conn, monkeypatch):
    """`list` is where "what is hot" gets asked over a whole subtree, so the
    number has to be there too — and has to agree with the one `get` reports."""
    s = _store(conn, monkeypatch, "")
    s.write(body="один", path="a.b", title="A")
    s.write(at="a.b", body="два")
    row = [r for r in s.list(None) if r["path"] == "a.b"][0]
    assert row["edits"] == 1 == s.get(None, at="a.b").usage["edits"]


def test_a_move_is_a_revision_too(conn, monkeypatch):
    """Deliberate: `seq` counts what the history holds, and a move IS an entry
    there. A number that skipped them would disagree with `history`."""
    s = _store(conn, monkeypatch, "")
    s.write(body="один", path="a.b", title="A")
    s.write(at="a.b", path="a.c")
    assert s.get(None, at="a.c").usage["edits"] == 1
