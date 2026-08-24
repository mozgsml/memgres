"""Three small things that all guard the same failure: a write that succeeds
while the data is wrong, or a read that looks whole when it isn't.

* the substring edit accepts the spellings other tools use, and refuses two of
  them that disagree;
* a body ending in what looks like the client's own tool delimiter is stored
  unchanged and reported;
* a line-ranged read says loudly that it is partial.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

psycopg = pytest.importorskip("psycopg")

from memgres.config import load  # noqa: E402
from memgres.delimiters import stray_delimiters  # noqa: E402
from memgres.lines import parse_line_spec  # noqa: E402
from memgres.schema import migrate  # noqa: E402
from memgres.store import Store, build_replace, fold_replace_aliases  # noqa: E402

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


# ─── the substring edit answers to three names ───────────────────────────────
def test_the_spellings_other_editors_use_are_accepted(store):
    m = store.write(body="alpha beta gamma\n")
    for old_key, new_key in (("replace_old", "replace_new"),
                             ("old_string", "new_string"),
                             ("old_str", "new_str")):
        folded = fold_replace_aliases({old_key: "beta", new_key: "BETA"})
        assert folded == {"replace_old": "beta", "replace_new": "BETA"}

    folded = fold_replace_aliases({"old_string": "beta", "new_string": "BETA"})
    edited = store.write(id=m.id, replace=build_replace(folded["replace_old"],
                                                        folded["replace_new"]))
    assert edited.body == "alpha BETA gamma\n"


def test_two_spellings_that_disagree_are_refused(store):
    """Picking one silently would apply an edit nobody asked for — and this
    family of parameters already has a history of quiet damage."""
    with pytest.raises(ValueError) as e:
        fold_replace_aliases({"replace_new": "one", "new_string": "two"})
    assert "conflicting" in str(e.value)
    # the same text under two names is just a redundant client, not a conflict
    assert fold_replace_aliases({"replace_new": "same", "new_str": "same"}) == {
        "replace_new": "same"}


def test_the_missing_half_is_named_along_with_its_other_spellings(store):
    with pytest.raises(ValueError) as e:
        build_replace("beta", None)
    msg = str(e.value)
    assert "replace_new" in msg and "new_string" in msg


# ─── a leaked tool delimiter is reported, never cleaned ──────────────────────
def test_a_stray_delimiter_is_reported_and_the_body_kept(store):
    m = store.write(body="a note that got cut off</body>")
    assert m.body == "a note that got cut off</body>"      # stored verbatim
    assert m.warnings and "</body>" in m.warnings[0]


def test_markup_in_prose_is_not_flagged(store):
    """The false-positive test. Measured on 88 live records: "unbalanced closing
    tag with one of our parameter names" alone flagged two records that were
    *discussing this very failure*. Only the position test cleared them."""
    quiet = [
        "<div>a paragraph about html</div>\n",
        "a note about the </body> tag and how clients leak it\nmore text after\n",
        "<body>full document</body>\n",
        "config: <parameter name='x'>1</parameter>\n",
        "",
    ]
    for body in quiet:
        assert stray_delimiters(body) == [], body

    # The literals below are ASSEMBLED rather than written out: this very file
    # was truncated once while being saved, because the closing tag in its own
    # source leaked through the authoring tool. The bug is real enough to bite
    # the test for the bug.
    close = lambda name: "</" + name + ">"                          # noqa: E731
    loud = ["cut off" + close("body"),
            "trailing" + close("replace_new"),
            "x" + close("antml:parameter")]
    for body in loud:
        assert stray_delimiters(body), body


def test_an_edit_that_introduces_one_is_reported_too(store):
    """Checked on the STORED body, not the request: a substring edit can put the
    stray tag there just as well as a whole-body write can."""
    m = store.write(body="clean text here\n")
    assert m.warnings == []
    edited = store.write(id=m.id, replace=("here\n", "here" + "</" + "body>"))
    assert edited.warnings and "body" in edited.warnings[0]


# ─── a partial read says so ──────────────────────────────────────────────────
def test_a_line_range_returns_part_and_admits_it(store):
    body = "".join(f"line {i}\n" for i in range(1, 13))
    m = store.write(body=body, path="long.doc")

    part = store.get(None, m.id, lines="3-5")
    assert part.body == "line 3\nline 4\nline 5\n"
    assert part.partial is True
    assert part.lines == [3, 5] and part.total_lines == 12
    # the hash is withheld: it would describe text the caller cannot see, and
    # the dangerous move is sending a slice back as a whole body
    assert part.content_hash is None
    assert part.to_dict()["partial"] is True

    # a whole read is unchanged and unmarked
    whole = store.get(None, m.id)
    assert whole.body == body and whole.partial is False
    assert whole.content_hash is not None


def test_a_line_range_past_the_end_returns_what_exists(store):
    m = store.write(body="one\ntwo\n")
    part = store.get(None, m.id, lines="1,40-80")
    assert part.body == "one\n" and part.lines == [1, 1]
    assert store.get(None, m.id, lines="40-80").body == ""


def test_an_absurd_line_range_costs_nothing(store):
    """`lines="1-50000000"` on a ten-line memory used to build fifty million
    integers before anything looked at the body: 4.3 GB and 3.4 seconds, from one
    request, to return ten numbers. The range is clipped to the body BEFORE it is
    expanded."""
    import resource
    import time

    m = store.write(body="".join(f"line {i}\n" for i in range(1, 11)))
    before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    t = time.time()
    part = store.get(None, m.id, lines="1-50000000")
    assert part.total_lines == 10 and part.lines == [1, 10]
    assert time.time() - t < 1.0
    grew_mb = (resource.getrusage(resource.RUSAGE_SELF).ru_maxrss - before) / 1024
    assert grew_mb < 100, f"grew {grew_mb:.0f}MB"


def test_a_selector_too_large_to_clip_is_refused(store):
    """Without a body to clip against — blame passes none — an impossible
    selector is refused rather than trimmed to the first hundred thousand, which
    would look like an answer."""
    with pytest.raises(ValueError):
        parse_line_spec("1-50000000")
    with pytest.raises(ValueError):
        parse_line_spec(",".join(str(i) for i in range(1, 100_003)))


def test_the_line_parser_is_shared_and_forgiving():
    assert parse_line_spec(None) is None
    assert parse_line_spec("2") == [2]
    assert parse_line_spec("1,3-5") == [1, 3, 4, 5]
    assert parse_line_spec("5-2") == [2, 3, 4, 5]      # reversed reads the same
    assert parse_line_spec("3-9", total=5) == [3, 4, 5]
    assert parse_line_spec("0") is None                 # 1-based, so 0 is nothing


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
