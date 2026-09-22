"""The evaluation harness and the fusion knobs it exists to tune.

Two halves: pure checks of the fusion arithmetic and the literal detector (no
database), and an integration pass that derives cases from a real corpus the way
`memgres-eval` does, to prove the generated ground truth actually points at the
memory it claims.
"""

import os
import sys
from pathlib import Path

import psycopg
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from memgres import eval as ev                                    # noqa: E402
from memgres.config import load                                   # noqa: E402
from memgres.embeddings import Embedder                           # noqa: E402
from memgres.schema import migrate                                # noqa: E402
from memgres.search import _rrf, looks_literal                    # noqa: E402
from memgres.store import Store                                   # noqa: E402
from memgres.vector.base import Hit                               # noqa: E402

DSN = os.environ.get("MEMGRES_TEST_DSN",
                     "postgresql://memgres:memgres@localhost:55432/memgres_dev")


# ─── the literal detector ────────────────────────────────────────────────────
@pytest.mark.parametrize("query", [
    "192.168.1.121",
    "MEMGRES_TOKEN_SINK",
    "okx.agentstools.dev",
    "/var/www/memgres",
    "create_own_namespace",
    "что лежит в /etc/nginx/sites-available",          # a literal inside prose
])
def test_a_query_after_an_exact_string_is_recognised(query):
    assert looks_literal(query) is True


@pytest.mark.parametrize("query", [
    "почему платежи внезапно перестали проходить бесплатно",
    "что мы решили про оплату подписки",
    "the failure rate dropped to 5.7 percent",          # a decimal is not a literal
    "what did we decide about pricing",
])
def test_ordinary_prose_is_not_mistaken_for_one(query):
    """The detector decides which ranking to trust, so a false positive would
    quietly hand prose queries to the lexical branch — the branch that answers
    them with whatever shares one common word."""
    assert looks_literal(query) is False


# ─── weighted fusion ─────────────────────────────────────────────────────────
def _hits(*ids):
    return [Hit(id=i, body=None, tags=[], path=i, score=0.0, title=i)
            for i in ids]


def test_a_down_weighted_list_no_longer_carries_its_top_hit_alone():
    """The point of the weights: a lexical #1 that matched one common word must
    not outrank a vector #1, while a memory BOTH rankings put forward still
    wins — that agreement is the signal hybrid exists to use."""
    sem, lex = _hits("good", "other"), _hits("junk", "good")

    even = [h.id for h in _rrf([sem, lex], 10, rrf_k=60)]
    assert even[:2] == ["good", "junk"]          # junk ties on rank, sits 2nd

    damped = [h.id for h in _rrf([sem, lex], 10, rrf_k=60, weights=[1.0, 0.3])]
    assert damped[0] == "good"
    assert damped.index("junk") > damped.index("other")   # now below a real hit


def test_rrf_k_decides_whether_agreement_beats_one_list_first_place():
    """What the damping constant actually buys. `agreed` is 2nd in both
    rankings, `x` and `y` are 1st in one each. With k=60 the top few positions
    are nearly equal in value, so two seconds outweigh a single first; with k=0
    a first place is worth double a second and the agreement advantage is gone.
    That is the knob: how much being confirmed twice is worth."""
    sem, lex = _hits("x", "agreed"), _hits("y", "agreed")

    flat = _rrf([sem, lex], 3, rrf_k=60)
    assert flat[0].id == "agreed"
    assert flat[0].score > flat[1].score

    peaked = _rrf([sem, lex], 3, rrf_k=0)
    assert peaked[0].score == pytest.approx(
        [h for h in peaked if h.id == "agreed"][0].score)   # no longer ahead


def test_the_weights_are_settings_not_constants():
    """A deployment tunes these against its own corpus, so they must come from
    the environment — and a configuration that ranks nothing is refused."""
    os.environ["MEMGRES_RRF_K"] = "17"
    os.environ["MEMGRES_RRF_W_LEXICAL"] = "0.4"
    try:
        cfg = load()
        assert (cfg.rrf_k, cfg.rrf_w_lexical, cfg.rrf_w_semantic) == (17, 0.4, 1.0)
        os.environ["MEMGRES_RRF_W_LEXICAL"] = "0"
        os.environ["MEMGRES_RRF_W_SEMANTIC"] = "0"
        with pytest.raises(ValueError, match="cannot both be 0"):
            load()
    finally:
        for k in ("MEMGRES_RRF_K", "MEMGRES_RRF_W_LEXICAL", "MEMGRES_RRF_W_SEMANTIC"):
            os.environ.pop(k, None)


# ─── metrics ─────────────────────────────────────────────────────────────────
def test_the_score_reports_where_the_answer_landed_not_just_whether_it_was_found():
    got = ev._score([1, 2, None, 5])
    assert got["n"] == 4 and got["missed"] == 1
    assert got["hit@1"] == 0.25 and got["hit@3"] == 0.5 and got["hit@10"] == 0.75
    assert got["mrr"] == round((1 + 0.5 + 0.2) / 4, 4)


# ─── generated ground truth, against a real corpus ───────────────────────────
class _Keyword(Embedder):
    dim = 3

    def _vec(self, t):
        t = t.lower()
        v = [float(t.count("apple")), float(t.count("banana")), float(t.count("cherry"))]
        n = sum(x * x for x in v) ** 0.5 or 1.0
        return [x / n for x in v]

    def embed_documents(self, texts):
        return [self._vec(t) for t in texts]

    def embed_query(self, t):
        return self._vec(t)


@pytest.fixture
def store(monkeypatch):
    try:
        psycopg.connect(DSN, connect_timeout=2).close()
    except Exception:
        pytest.skip("no test Postgres")
    with psycopg.connect(DSN, autocommit=True) as c, c.cursor() as cur:
        cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    for k in list(os.environ):
        if k.startswith("MEMGRES_"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("MEMGRES_DATABASE_URL", DSN)
    monkeypatch.setenv("MEMGRES_FTS_LANGUAGE", "simple")
    monkeypatch.setenv("MEMGRES_REQUIRE_TITLE", "false")
    # the shape of an embedded deployment; the stub embedder above is injected
    monkeypatch.setenv("MEMGRES_EMBED_PROVIDER", "openai")
    monkeypatch.setenv("MEMGRES_EMBED_MODEL", "stub")
    monkeypatch.setenv("MEMGRES_EMBED_DIM", "3")
    monkeypatch.setenv("MEMGRES_EMBED_API_KEY", "x")
    conn = psycopg.connect(DSN)
    migrate(conn, load())
    s = Store(load(), embedder=_Keyword(), conn=conn)
    yield s
    conn.close()


def test_generated_cases_point_at_the_memory_they_came_from(store):
    a = store.write(body="apple orchards near the river " * 12,
                    title="How the apple orchards were planted",
                    path="notes.apples")
    store.write(body="the service listens on 10.11.12.13 and reads BANANA_SINK_PATH " * 8,
                title="Where the banana service keeps its socket", path="notes.bananas")
    with store._conn.cursor() as cur:
        cur.execute("SELECT DISTINCT namespace FROM memory")
        ns = [str(r[0]) for r in cur.fetchall()]

    import random
    titles = ev._title_cases(store._conn, ns, 10, random.Random(0))
    assert {t[1] for t in titles} == {"How the apple orchards were planted",
                                      "Where the banana service keeps its socket"}
    assert dict((q, mid) for _k, q, mid in titles)[
        "How the apple orchards were planted"] == str(a.id)

    literals = ev._literal_cases(store._conn, ns, 10, random.Random(0))
    found = {q.lower() for _k, q, _m in literals}
    assert "10.11.12.13" in found and "banana_sink_path" in found
    # every generated case names exactly one memory — that is what makes it an answer
    for _kind, q, mid in literals:
        with store._conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM memory WHERE body ILIKE %s", (f"%{q}%",))
            assert cur.fetchone()[0] == 1, q
        assert mid


def test_the_harness_scores_every_setting_over_one_fetch(store):
    store.write(body="apple " * 60, title="All about apples", path="a")
    store.write(body="banana " * 60, title="All about bananas", path="b")
    with store._conn.cursor() as cur:
        cur.execute("SELECT DISTINCT namespace FROM memory")
        ns = [str(r[0]) for r in cur.fetchall()]
    import random
    cases = ev._title_cases(store._conn, ns, 5, random.Random(0))
    report = ev.evaluate(store._conn, store.cfg, store.embedder, store._vectors,
                         ns, cases, ev.default_settings(store.cfg, sweep=True), k=5)
    assert report["cases"] == len(cases)
    # the sweep costs no extra queries, so every setting is scored on the same lists
    assert len(report["settings"]) == 3 + 12
    for name, r in report["settings"].items():
        assert 0.0 <= r["all"]["mrr"] <= 1.0, name


# ─── the search log ──────────────────────────────────────────────────────────
def test_the_log_is_off_until_asked_for(store, monkeypatch):
    """A query is content, so recording it is a decision a deployment makes,
    not a default it discovers."""
    store.write(body="apple " * 40, title="Apples", path="a")
    store.recall(None, "apple")
    with store._conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM search_log")
        assert cur.fetchone()[0] == 0


def test_a_logged_search_and_the_memory_opened_after_it_make_a_case(store, monkeypatch):
    monkeypatch.setenv("MEMGRES_SEARCH_LOG", "true")
    s = Store(load(), embedder=_Keyword(), conn=store._conn, source="mcp")
    a = s.write(body="apple " * 40, title="All about apples", path="a")
    s.write(body="banana " * 40, title="All about bananas", path="b")

    s.recall(None, "apple")
    s.get(None, id=a.id)

    with s._conn.cursor() as cur:
        cur.execute("SELECT kind, source, query, mode, array_length(results, 1), memory_id "
                    "FROM search_log ORDER BY id")
        rows = cur.fetchall()
    kinds = [r[0] for r in rows]
    assert kinds == ["recall", "get"]
    assert rows[0][1] == "mcp" and rows[0][2] == "apple"
    assert rows[0][3] in ("hybrid", "lexical")     # never the unresolved 'auto'
    assert rows[0][4] >= 1
    assert str(rows[1][5]) == str(a.id)

    with s._conn.cursor() as cur:
        cur.execute("SELECT DISTINCT namespace FROM memory")
        ns = [str(r[0]) for r in cur.fetchall()]
    cases = ev._log_cases(s._conn, ns, 10)
    assert cases == [("logged", "apple", str(a.id))]
    # and the door can be singled out, since agents and people ask differently
    assert ev._log_cases(s._conn, ns, 10, source="web") == []


def test_a_broken_log_never_breaks_a_search(store, monkeypatch):
    """Best-effort, like the usage counters: statistics must not be the reason a
    read fails."""
    monkeypatch.setenv("MEMGRES_SEARCH_LOG", "true")
    s = Store(load(), embedder=_Keyword(), conn=store._conn)
    s.write(body="apple " * 40, title="Apples", path="a")
    with s._conn.cursor() as cur:
        cur.execute("DROP TABLE search_log")
    assert s.recall(None, "apple")          # the answer still comes back
