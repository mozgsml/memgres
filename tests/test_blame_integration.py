"""Blame (line attribution) and version reconstruction against a live DB."""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

psycopg = pytest.importorskip("psycopg")

from memgres.config import load  # noqa: E402
from memgres.diffing import make_diff  # noqa: E402
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
    conn = psycopg.connect(DSN)
    migrate(conn, load())
    s = Store(load(), conn=conn)
    yield s
    conn.close()


def test_blame_attributes_each_line(store):
    # alice writes two lines
    m = store.write(body="alpha\nbeta\n", source="alice", reason="seed")
    # bob changes the second line
    v2 = "alpha\nBETA-edited\n"
    m = store.write(id=m.id, diff=make_diff(m.body, v2), base_hash=m.content_hash,
                    source="bob", reason="edit beta")
    # carol appends a third line
    v3 = "alpha\nBETA-edited\ngamma\n"
    m = store.write(id=m.id, diff=make_diff(m.body, v3), base_hash=m.content_hash,
                    source="carol", reason="add gamma")

    blame = store.annotate(None, m.id)
    assert [b["text"] for b in blame] == ["alpha\n", "BETA-edited\n", "gamma\n"]
    assert blame[0]["source"] == "alice"     # untouched original line
    assert blame[1]["source"] == "bob"       # bob rewrote line 2
    assert blame[2]["source"] == "carol"     # carol added line 3
    assert blame[0]["seq"] == 1 and blame[2]["seq"] == 3


def test_blame_line_selector(store):
    m = store.write(body="l1\nl2\nl3\nl4\nl5\n", source="alice")
    # single line
    one = store.annotate(None, m.id, lines=[3])
    assert len(one) == 1 and one[0]["line"] == 3 and one[0]["text"] == "l3\n"
    # a set of lines
    some = store.annotate(None, m.id, lines=[1, 4, 5])
    assert [b["line"] for b in some] == [1, 4, 5]
    # out-of-range ignored, not an error
    assert store.annotate(None, m.id, lines=[99]) == []
    # None = whole document
    assert len(store.annotate(None, m.id)) == 5


def test_blame_grouped_into_runs(store):
    # alice writes 4 lines, bob rewrites the middle two -> expect 3 blocks
    m = store.write(body="a1\na2\na3\na4\n", source="alice", reason="seed")
    v2 = "a1\nB2\nB3\na4\n"
    m = store.write(id=m.id, diff=make_diff(m.body, v2), base_hash=m.content_hash,
                    source="bob", reason="mid")
    g = store.annotate_grouped(None, m.id)
    assert len(g) == 3
    assert (g[0]["start"], g[0]["end"], g[0]["source"]) == (1, 1, "alice")
    assert (g[1]["start"], g[1]["end"], g[1]["source"], g[1]["lines"]) == (2, 3, "bob", 2)
    assert (g[2]["start"], g[2]["end"], g[2]["source"]) == (4, 4, "alice")
    assert g[1]["text"] == "B2\nB3\n"
    # ownership map: no text
    g2 = store.annotate_grouped(None, m.id, include_text=False)
    assert "text" not in g2[0] and g2[1]["lines"] == 2


def test_reconstruct_matches_each_version(store):
    m = store.write(body="one\n")
    bodies = ["one\n"]
    for i in range(2, 8):
        new = "".join(f"line{j}\n" for j in range(i))
        m = store.write(id=m.id, diff=make_diff(m.body, new), base_hash=m.content_hash)
        bodies.append(new)
    # every historical version reconstructs exactly
    for seq, expected in enumerate(bodies, start=1):
        assert store.reconstruct(None, m.id, seq) == expected
    # default = current
    assert store.reconstruct(None, m.id) == bodies[-1]


def test_reconstruct_ignores_metadata_ops(store):
    m = store.write(body="body text\n", source="a")
    store.write(id=m.id, tags=["x"])          # retag: no line change
    store.write(id=m.id, path="p.q")          # move: no line change
    assert store.reconstruct(None, m.id) == "body text\n"
    blame = store.annotate(None, m.id)
    assert len(blame) == 1 and blame[0]["source"] == "a"


def test_blame_survives_whole_body_replace(store):
    m = store.write(body="keep\nreplace me\n", source="orig")
    m = store.write(id=m.id, body="keep\nnew content\n", base_hash=m.content_hash,
                    source="editor")           # whole-body replace stores a diff too
    blame = store.annotate(None, m.id)
    assert blame[0]["source"] == "orig"        # 'keep' unchanged
    assert blame[1]["source"] == "editor"      # replaced line credited to editor


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
