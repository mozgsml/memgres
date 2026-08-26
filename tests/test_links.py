"""The link graph: `[[wiki links]]` become edges you can walk, including backwards.

The convention was already load-bearing before any tool supported it — 238
`[[…]]` links across the 97 memories of the reference corpus. What living only in
body text could not do was answer "what points HERE", which is the question that
matters when a fact changes; 42 of those 97 memories had no inbound link at all
and were reachable only by search.

The tests below split into three claims:

* the parser recognises links and, just as importantly, LEAVES ALONE things that
  merely sit in double brackets — documentation of the syntax, URLs, prose;
* an edge pins the target's id at write time, so it survives the target moving
  and cannot be hijacked by something later claiming the vacated path — AND the
  move repairs the bodies that name the old address, because edges are re-derived
  from body text on every write and the pin alone would last only until the
  referring memory was next edited;
* a link across a tenant boundary does not exist — paths are not a global
  address space, and a backlink names a memory.
"""

import dataclasses
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

psycopg = pytest.importorskip("psycopg")

from memgres import identity as ident  # noqa: E402
from memgres.config import load  # noqa: E402
from memgres.links import parse_links  # noqa: E402
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


def _clean_env(monkeypatch):
    with psycopg.connect(DSN, autocommit=True) as c, c.cursor() as cur:
        # A test that fails mid-way leaves its connection open, and DROP SCHEMA
        # then blocks behind it — the next run HANGS instead of reporting the
        # failure. Evict other backends first so a broken test fails loudly.
        cur.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = current_database() AND pid <> pg_backend_pid()")
        cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    for k in list(os.environ):
        if k.startswith("MEMGRES_"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("MEMGRES_DATABASE_URL", DSN)
    monkeypatch.setenv("MEMGRES_EMBED_PROVIDER", "none")
    monkeypatch.setenv("MEMGRES_FTS_LANGUAGE", "simple")
    monkeypatch.setenv("MEMGRES_REQUIRE_TITLE", "false")


@pytest.fixture
def store(monkeypatch):
    _clean_env(monkeypatch)
    conn = psycopg.connect(DSN)
    cfg = load()
    migrate(conn, cfg)
    s = Store(cfg, conn=conn)
    yield s
    conn.close()


@pytest.fixture
def tenants(monkeypatch):
    """Two users, each owning a namespace, in managed mode."""
    _clean_env(monkeypatch)
    base = load()
    setup = psycopg.connect(DSN, autocommit=True)
    migrate(setup, base)
    cfg = dataclasses.replace(base, key_mode="managed")
    s = Store(cfg, conn=psycopg.connect(DSN))
    s._own_conn = True

    def user(name, space):
        uid = ident.create_user(setup, name=name)
        nsid = ident.create_namespace(setup, uid, space)
        secret, _ = ident.issue_token(setup, uid, namespace_id=nsid,
                                      permission="write")
        return secret, nsid

    yield s, user
    s.close()
    setup.close()


# ═══ the parser ══════════════════════════════════════════════════════════════
def test_the_four_shapes():
    body = ("see [[ops.deploy]] and [[ops.deploy#gate]] and "
            "[[ops.deploy|the runbook]] and [[ops.deploy#gate|the QA gate]]")
    links = parse_links(body)
    assert [(l.raw_target, l.anchor, l.label) for l in links] == [
        ("ops.deploy", None, None),
        ("ops.deploy", "gate", None),
        ("ops.deploy", None, "the runbook"),
        ("ops.deploy", "gate", "the QA gate"),
    ]


def test_the_anchor_comes_before_the_label():
    """`#` then `|`, as in every wiki dialect — so a `#` inside a LABEL is text.
    The split is positional rather than clever, which is what keeps it
    predictable."""
    [link] = parse_links("[[ops.deploy|issue #42]]")
    assert (link.raw_target, link.anchor, link.label) == \
        ("ops.deploy", None, "issue #42")


def test_order_is_recorded():
    links = parse_links("[[b.one]] then [[a.two]] then [[c.three]]")
    assert [l.ord for l in links] == [0, 1, 2]
    assert [l.raw_target for l in links] == ["b.one", "a.two", "c.three"]


def test_documentation_of_the_syntax_is_not_a_link():
    """The corpus's own notes explain the convention by writing it in backticks.
    A parser that flags its own documentation teaches everyone to ignore it."""
    assert parse_links("write it as `[[path]]` in the body") == []
    assert parse_links("```\nlink: [[ops.deploy]]\n```") == []
    assert parse_links("~~~\n[[ops.deploy]]\n~~~") == []


def test_blanking_code_keeps_the_rest_findable():
    links = parse_links("`[[ignored.me]]` but [[real.one]] counts")
    assert [l.raw_target for l in links] == ["real.one"]


def test_urls_and_prose_are_left_alone():
    assert parse_links("[[https://example.com/x]]") == []
    assert parse_links("[[mailto:someone@example.com]]") == []
    assert parse_links("[[some thing with spaces]]") == []
    assert parse_links("[[has-hyphens]]") == []      # not an ltree label


def test_known_schemes_point_at_other_stores():
    links = parse_links("[[idea:payai-x402-facilitator]] and [[file:notes-2021]]")
    assert [(l.scheme, l.raw_target) for l in links] == [
        ("idea", "idea:payai-x402-facilitator"),
        ("file", "file:notes-2021"),
    ]


# ═══ edges ═══════════════════════════════════════════════════════════════════
def test_a_link_becomes_a_resolved_edge(store):
    target = store.write(body="the runbook", path="ops.deploy")
    src = store.write(body="see [[ops.deploy]]", path="notes.a")

    out = store.links(None, src.id)["out"]
    assert len(out) == 1
    assert out[0]["resolved"] is True
    assert out[0]["id"] == target.id and out[0]["path"] == "ops.deploy"


def test_backlinks_answer_what_the_body_cannot(store):
    target = store.write(body="the runbook", path="ops.deploy", title="Runbook")
    a = store.write(body="see [[ops.deploy]]", path="notes.a", title="A")
    b = store.write(body="also [[ops.deploy|there]]", path="notes.b", title="B")

    inbound = store.links(None, target.id)["in"]
    assert [r["id"] for r in inbound] == sorted([a.id, b.id],
                                                key=lambda i: ("notes.a" if i == a.id
                                                               else "notes.b"))
    assert {r["path"] for r in inbound} == {"notes.a", "notes.b"}
    assert [r["label"] for r in inbound if r["id"] == b.id] == ["there"]


def test_a_link_to_something_unwritten_dangles_and_says_so(store):
    src = store.write(body="see [[ops.not_yet]]", path="notes.a")
    [edge] = store.links(None, src.id)["out"]
    assert edge["resolved"] is False and edge["target"] == "ops.not_yet"
    assert edge["path"] is None


def test_the_dangling_edge_binds_when_the_target_appears(store):
    """A link to something not yet written is a deliberate marker. It has to
    stand — and then actually bind, or the marker was a lie."""
    src = store.write(body="see [[ops.not_yet]]", path="notes.a")
    target = store.write(body="now it exists", path="ops.not_yet")

    [edge] = store.links(None, src.id)["out"]
    assert edge["resolved"] is True and edge["id"] == target.id
    assert [r["id"] for r in store.links(None, target.id)["in"]] == [src.id]


def test_it_also_binds_when_a_memory_moves_onto_the_awaited_path(store):
    src = store.write(body="see [[ops.awaited]]", path="notes.a")
    target = store.write(body="elsewhere for now", path="tmp.draft")
    assert store.links(None, src.id)["out"][0]["resolved"] is False

    store.move(None, target.id, "ops.awaited")
    assert store.links(None, src.id)["out"][0]["id"] == target.id


def test_an_edge_follows_its_target_when_it_moves(store):
    """And the BODY is repaired to match. Leaving the old address written there
    would be a delayed break, not a cosmetic one: edges are re-derived from the
    body on every write, so the next edit to `src` — a typo fix — would rebuild
    this edge from text that no longer resolves."""
    target = store.write(body="the runbook", path="ops.deploy")
    src = store.write(body="see [[ops.deploy]]", path="notes.a")
    store.move(None, target.id, "ops.deploy_v2")

    [edge] = store.links(None, src.id)["out"]
    assert edge["resolved"] is True and edge["id"] == target.id
    assert edge["path"] == "ops.deploy_v2"       # where it lives NOW
    assert edge["target"] == "ops.deploy_v2"     # and what the body says
    assert store.get(None, src.id).body == "see [[ops.deploy_v2]]"


def test_a_new_memory_cannot_hijack_the_vacated_path(store):
    """THE test this table exists for. The target moves away and something else
    deliberately claims its old path; a link resolved by TEXT would silently
    retarget to the impostor. The edge pins an id, so it cannot."""
    target = store.write(body="the runbook", path="ops.deploy")
    src = store.write(body="see [[ops.deploy]]", path="notes.a")
    store.move(None, target.id, "ops.deploy_v2")

    impostor = store.write(body="something else entirely", path="ops.deploy",
                           if_moved="create")

    [edge] = store.links(None, src.id)["out"]
    assert edge["id"] == target.id
    assert edge["id"] != impostor.id
    assert [r["id"] for r in store.links(None, impostor.id)["in"]] == []


def test_erasing_a_target_leaves_the_edge_visibly_dangling(store):
    """`forget` is real deletion and leaves no redirect. The edge must not vanish
    (the link is still written in the body) and must not point at whatever later
    takes the path — so it goes null, and the loss is visible."""
    target = store.write(body="the runbook", path="ops.deploy")
    src = store.write(body="see [[ops.deploy]]", path="notes.a")
    store.forget(None, target.id)

    [edge] = store.links(None, src.id)["out"]
    assert edge["resolved"] is False
    assert edge["target"] == "ops.deploy"        # still says what it wanted


def test_editing_the_body_re_derives_the_edges(store):
    store.write(body="the runbook", path="ops.deploy")
    store.write(body="other", path="ops.other")
    src = store.write(body="see [[ops.deploy]]", path="notes.a")

    store.write(id=src.id, body="now see [[ops.other]] instead")
    assert [e["target"] for e in store.links(None, src.id)["out"]] == ["ops.other"]


def test_deleting_the_source_takes_its_edges_with_it(store):
    target = store.write(body="the runbook", path="ops.deploy")
    src = store.write(body="see [[ops.deploy]]", path="notes.a")
    store.forget(None, src.id)
    assert store.links(None, target.id)["in"] == []


def test_direction_narrows_the_answer(store):
    target = store.write(body="the runbook", path="ops.deploy")
    store.write(body="see [[ops.deploy]]", path="notes.a")
    assert set(store.links(None, target.id, direction="in")) == {"in"}
    assert set(store.links(None, target.id, direction="out")) == {"out"}
    with pytest.raises(ValueError, match="direction"):
        store.links(None, target.id, direction="sideways")


def test_a_scheme_link_is_recorded_but_never_resolved(store):
    src = store.write(body="from [[idea:payai-x402]]", path="notes.a")
    [edge] = store.links(None, src.id)["out"]
    assert edge["scheme"] == "idea" and edge["resolved"] is False


# ═══ tenancy ═════════════════════════════════════════════════════════════════
def test_a_link_never_binds_across_a_tenant_boundary(tenants):
    """Paths are not a global address space. Two tenants may both own
    `ops.deploy`, and a link written by one must resolve to its own."""
    s, user = tenants
    a_tok, _ = user("alice", "alpha")
    b_tok, _ = user("bob", "beta")

    b_target = s.write(b_tok, body="bob's runbook", path="ops.deploy",
                       space="beta")
    a_src = s.write(a_tok, body="see [[ops.deploy]]", path="notes.a",
                    space="alpha")

    [edge] = s.links(a_tok, a_src.id)["out"]
    assert edge["resolved"] is False              # alice has no ops.deploy
    assert edge["id"] != b_target.id

    a_target = s.write(a_tok, body="alice's runbook", path="ops.deploy",
                       space="alpha")
    [edge] = s.links(a_tok, a_src.id)["out"]
    assert edge["id"] == a_target.id              # binds to HER own


def test_backlinks_do_not_name_memories_from_unreachable_namespaces(tenants):
    """A backlink carries a path and a title. Listing one from a namespace the
    caller cannot read would leak all three — that a memory exists, where it
    lives, and what it is called."""
    s, user = tenants
    a_tok, _ = user("alice", "alpha")
    b_tok, _ = user("bob", "beta")

    a_target = s.write(a_tok, body="alice's runbook", path="ops.deploy",
                       space="alpha")
    s.write(a_tok, body="see [[ops.deploy]]", path="notes.a", space="alpha")

    # Bob writes the same link text in his own namespace; it binds to nothing of
    # Alice's, and even if it did he must not appear in her backlinks.
    s.write(b_tok, body="see [[ops.deploy]]", path="notes.b", space="beta")

    inbound = s.links(a_tok, a_target.id)["in"]
    assert {r["path"] for r in inbound} == {"notes.a"}


def test_a_memory_can_be_addressed_by_its_path(store):
    store.write(body="the runbook", path="ops.deploy")
    store.write(body="see [[ops.deploy]]", path="notes.a")
    inbound = store.links(None, at="ops.deploy")["in"]
    assert [r["path"] for r in inbound] == ["notes.a"]


def test_an_expired_source_stops_appearing_in_backlinks(store):
    target = store.write(body="the runbook", path="ops.deploy")
    src = store.write(body="see [[ops.deploy]]", path="notes.a")
    with store._conn.cursor() as cur:
        cur.execute("UPDATE memory SET expires_at = now() - interval '1 day' "
                    "WHERE id=%s", (src.id,))
    store._conn.commit()
    assert store.links(None, target.id)["in"] == []


# ═══ the backfill ════════════════════════════════════════════════════════════
def test_a_corpus_written_before_the_graph_gets_one(store):
    """Edges are derived on write, so an existing corpus would upgrade into a
    perfectly empty graph — and `memory_links` would answer "nothing points here"
    for every memory, which reads like a fact rather than like "not indexed yet".
    """
    from memgres.relink import rebuild
    target = store.write(body="the runbook", path="ops.deploy")
    src = store.write(body="see [[ops.deploy]]", path="notes.a")
    # Simulate the pre-graph state: bodies stored, no edges.
    with store._conn.cursor() as cur:
        cur.execute("DELETE FROM memory_link")
        cur.execute("UPDATE memgres_meta SET links_built = false")
    store._conn.commit()
    assert store.links(None, target.id)["in"] == []

    assert rebuild(store._conn, store.cfg) == 2
    store._conn.commit()
    assert [r["id"] for r in store.links(None, target.id)["in"]] == [src.id]
    assert store.links(None, src.id)["out"][0]["id"] == target.id


def test_the_backfill_runs_once(store):
    from memgres.relink import rebuild
    store.write(body="see [[ops.deploy]]", path="notes.a")
    with store._conn.cursor() as cur:
        cur.execute("UPDATE memgres_meta SET links_built = false")
    store._conn.commit()

    assert rebuild(store._conn, store.cfg) == 1
    store._conn.commit()
    assert rebuild(store._conn, store.cfg) == 0        # flag set: no rescan
    assert rebuild(store._conn, store.cfg, force=True) == 1   # unless forced


def test_the_backfill_does_not_cross_namespaces(tenants):
    from memgres.relink import rebuild
    s, user = tenants
    a_tok, _ = user("alice", "alpha")
    b_tok, _ = user("bob", "beta")
    b_target = s.write(b_tok, body="bob's runbook", path="ops.deploy", space="beta")
    a_src = s.write(a_tok, body="see [[ops.deploy]]", path="notes.a", space="alpha")

    with s._conn.cursor() as cur:
        cur.execute("DELETE FROM memory_link")
        cur.execute("UPDATE memgres_meta SET links_built = false")
    s._conn.commit()
    rebuild(s._conn, s.cfg, force=True)
    s._conn.commit()

    [edge] = s.links(a_tok, a_src.id)["out"]
    assert edge["resolved"] is False and edge["id"] != b_target.id


# ─── a move repairs the bodies that still name the old address ───────────────
#
# Pinning the edge to an id is not enough on its own, and the gap is easy to miss:
# `_sync_links` rebuilds a memory's edges FROM ITS BODY on every write, so the pin
# lasts exactly until the REFERRING memory is next touched. After that the stale
# text wins — silently, in one of two directions.

def test_the_repair_survives_the_next_edit_of_the_referrer(store):
    """Half one: the vacated path is empty, so a rebuild from stale text turns a
    resolved edge into a dangling one. The edit that does it can be a typo fix."""
    target = store.write(body="the runbook", path="ops.deploy")
    src = store.write(body="see [[ops.deploy]]", path="notes.a")
    store.move(None, target.id, "ops.deploy_v2")

    store.write(id=src.id, body=store.get(None, src.id).body + "\nps: typo fixed")

    [edge] = store.links(None, src.id)["out"]
    assert edge["resolved"] is True and edge["id"] == target.id


def test_a_later_occupant_cannot_capture_the_link_on_the_next_edit(store):
    """Half two, and the worse one: something else has claimed the vacated path,
    so the rebuild binds the edge to a DIFFERENT memory. Nothing is raised — the
    link simply points somewhere else from then on."""
    target = store.write(body="the runbook", path="ops.deploy")
    src = store.write(body="see [[ops.deploy]]", path="notes.a")
    store.move(None, target.id, "ops.deploy_v2")
    impostor = store.write(body="something else", path="ops.deploy",
                           if_moved="create")

    store.write(id=src.id, body=store.get(None, src.id).body + "\nps: edited")

    [edge] = store.links(None, src.id)["out"]
    assert edge["id"] == target.id and edge["id"] != impostor.id


def test_the_repair_keeps_the_label_and_the_anchor(store):
    """A rename moves the address — not what the author called it, nor which
    section they meant."""
    target = store.write(body="the runbook", path="ops.deploy")
    src = store.write(body="see [[ops.deploy#шаги|инструкция]]", path="notes.a")
    store.move(None, target.id, "ops.deploy_v2")
    assert store.get(None, src.id).body == "see [[ops.deploy_v2#шаги|инструкция]]"


def test_the_repair_does_not_touch_a_link_written_in_backticks(store):
    """Documentation that explains the syntax writes `[[path]]` in a code span.
    The parser never scanned it, so it has no edge and no span — which is exactly
    why the repair addresses links by ord instead of substituting text."""
    target = store.write(body="the runbook", path="ops.deploy")
    src = store.write(
        body="live [[ops.deploy]], quoted `[[ops.deploy]]`, fenced:\n"
             "```\n[[ops.deploy]]\n```",
        path="notes.a")
    store.move(None, target.id, "ops.deploy_v2")

    after = store.get(None, src.id).body
    assert "live [[ops.deploy_v2]]" in after
    assert "quoted `[[ops.deploy]]`" in after
    assert "```\n[[ops.deploy]]\n```" in after


def test_only_the_links_that_pointed_at_the_mover_are_rewritten(store):
    store.write(body="unrelated", path="ops.other")
    target = store.write(body="the runbook", path="ops.deploy")
    src = store.write(body="[[ops.deploy]] and [[ops.other]] and [[ops.deploy]]",
                      path="notes.a")
    store.move(None, target.id, "ops.deploy_v2")
    assert store.get(None, src.id).body == (
        "[[ops.deploy_v2]] and [[ops.other]] and [[ops.deploy_v2]]")


def test_a_move_writes_nothing_to_memories_that_do_not_link_to_it(store):
    """No phantom history: a memory nobody had to repair must come out of a move
    byte-identical, with nothing appended to its chain."""
    target = store.write(body="the runbook", path="ops.deploy")
    bystander = store.write(body="unrelated, and [[ops.nothing]] is unwritten",
                            path="notes.b")
    before = store.get(None, bystander.id)

    store.move(None, target.id, "ops.deploy_v2")

    after = store.get(None, bystander.id)
    assert (after.seq, after.content_hash) == (before.seq, before.content_hash)
    assert [r["op"] for r in store.history(None, bystander.id)] == ["create"]


def test_a_subtree_move_repairs_links_to_the_descendants_too(store):
    """A descendant is re-addressed by the cascade exactly like the node itself,
    so a link to one goes just as stale."""
    parent = store.write(body="parent", path="ops.deploy")
    child = store.write(body="child", path="ops.deploy.step1")
    src = store.write(body="see [[ops.deploy.step1]]", path="notes.a")

    store.move(None, parent.id, "ops.release")

    assert store.get(None, src.id).body == "see [[ops.release.step1]]"
    [edge] = store.links(None, src.id)["out"]
    assert edge["resolved"] is True and edge["id"] == child.id


def test_a_memory_linking_into_its_own_subtree_comes_back_repaired(store):
    """The mover can be its own referrer, and it is the one memory the caller is
    holding — returning it with the pre-repair body would hand back a body that
    is not what the store now has."""
    parent = store.write(body="root, see [[ops.deploy.step1]]", path="ops.deploy")
    store.write(body="child", path="ops.deploy.step1")

    moved = store.move(None, parent.id, "ops.release")

    assert moved.body == "root, see [[ops.release.step1]]"
    stored = store.get(None, parent.id)
    assert (moved.body, moved.content_hash, moved.seq) == (
        stored.body, stored.content_hash, stored.seq)


def test_the_repair_is_recorded_as_relink_and_the_chain_still_verifies(store):
    """The repair is a real change to a real memory: it appends a row, carries a
    diff, and leaves the tamper-evident chain verifiable."""
    target = store.write(body="the runbook", path="ops.deploy")
    src = store.write(body="see [[ops.deploy]]", path="notes.a")

    store.move(None, target.id, "ops.deploy_v2", reason="renamed for clarity")

    rows = store.history(None, src.id)
    assert [r["op"] for r in rows] == ["create", "relink"]
    assert "ops.deploy → ops.deploy_v2" in rows[-1]["reason"]
    assert rows[-1]["diff"]
    assert store.verify_history(None, src.id) is True


def test_the_repair_is_credited_to_whoever_moved_the_memory(monkeypatch):
    """Authorship goes to the person whose move caused it, under a `relink` op —
    not to a service identity. Someone did cause this, and crediting a ghost
    hides who, which is the opposite of what blame is for."""
    _clean_env(monkeypatch)
    base = load()
    setup = psycopg.connect(DSN, autocommit=True)
    migrate(setup, base)
    cfg = dataclasses.replace(base, key_mode="managed")
    s = Store(cfg, conn=psycopg.connect(DSN))

    uid_a = ident.create_user(setup, name="Alice")
    uid_b = ident.create_user(setup, name="Bob")
    nsid = ident.create_namespace(setup, uid_a, "shared")
    ident.add_member(setup, nsid, uid_b, "write")
    tok_a, _ = ident.issue_token(setup, uid_a, namespace_id=nsid, permission="write")
    tok_b, tid_b = ident.issue_token(setup, uid_b, namespace_id=nsid,
                                     permission="write")

    target = s.write(tok_a, body="the runbook", path="ops.deploy", space_id=nsid)
    src = s.write(tok_a, body="see [[ops.deploy]]", path="notes.a", space_id=nsid)

    s.move(tok_b, target.id, "ops.deploy_v2", space_id=nsid)   # Bob moves it

    last = s.history(tok_a, src.id, space_id=nsid)[-1]
    assert last["op"] == "relink"
    assert last["author_user_id"] == uid_b and last["author_token_id"] == tid_b
    assert last["author_name"] == "Bob"
    assert s.verify_history(tok_a, src.id, space_id=nsid) is True
    s._conn.close()
    setup.close()


def test_the_repair_stays_inside_the_namespace(monkeypatch, tenants):
    """Two tenants may both own `ops.deploy`. One moving theirs must not rewrite
    a single character of the other's body."""
    s, user = tenants
    a_tok, _ = user("alice", "alpha")
    b_tok, _ = user("bob", "beta")
    a_target = s.write(a_tok, body="alice's runbook", path="ops.deploy", space="alpha")
    s.write(b_tok, body="bob's runbook", path="ops.deploy", space="beta")
    b_src = s.write(b_tok, body="see [[ops.deploy]]", path="notes.b", space="beta")

    s.move(a_tok, a_target.id, "ops.deploy_v2", space="alpha")

    kept = s.get(b_tok, b_src.id, space="beta")
    assert kept.body == "see [[ops.deploy]]"          # untouched
    assert [r["op"] for r in s.history(b_tok, b_src.id, space="beta")] == ["create"]


# ─── the backfill has to SURVIVE the process that ran it ─────────────────────
def test_the_backfill_persists_after_the_server_starts_it(store):
    """`maybe_backfill` opens its OWN connection and closes it when done. If the
    work is not committed, closing rolls all of it back — and the server then
    serves a link graph that is perfectly empty while the log says it rebuilt one.
    That is the exact silence this backfill exists to prevent, and it shipped: our
    live corpus had 0 edges and `links_built` false after every start.

    Asserted from a SECOND connection on purpose. A connection always sees its own
    uncommitted work, so checking on the same one — or letting a fixture commit
    afterwards, which is what every other test here does — hides the bug
    completely."""
    from memgres.relink import maybe_backfill
    store.write(body="the runbook", path="ops.deploy")
    store.write(body="see [[ops.deploy]]", path="notes.a")
    store._conn.commit()

    with store._conn.cursor() as cur:                  # pretend it never ran
        cur.execute("DELETE FROM memory_link")
        cur.execute("UPDATE memgres_meta SET links_built = false")
    store._conn.commit()

    assert maybe_backfill(store.cfg, lambda: psycopg.connect(DSN)) == 2

    with psycopg.connect(DSN) as check, check.cursor() as cur:
        cur.execute("SELECT count(*) FROM memory_link")
        assert cur.fetchone()[0] == 1                  # the edge is really there
        cur.execute("SELECT links_built FROM memgres_meta")
        assert cur.fetchone()[0] is True               # and it will not run again


# ─── what a rewrite must never do to the text around it ──────────────────────
def test_a_rewrite_to_the_same_target_changes_nothing_at_all(store):
    """The oracle: point every link at the target it already has, and the body
    must come back byte-identical. Anything else is text destroyed by a repair —
    the worst outcome available in a memory store, and it commits as a
    well-formed diff that `verify_history` happily accepts."""
    from memgres.links import parse_links, rewrite_targets
    shapes = [
        "See [[ops.a|the `runbook` doc]] for details.",
        "[[ops.a`]] [[ops.b`]]",
        "[[ops.a|x` and more `y]] then [[ops.b]]",
        "plain [[ops.a]] and `[[ops.a]]` quoted",
        "```\n[[ops.a]]\n```\nthen [[ops.b#step|см. `код`]]",
        "[[ops.a#шаг|подпись]] · [[ops.b]] · [[idea:some-slug]]",
        "[[ops.a]][[ops.b]] adjacent, and [[[[ops.c]]]] nested",
        "trailing [[ops.a]]",
        "[[ops.a]] leading",
    ]
    for body in shapes:
        same = {l.ord: l.raw_target for l in parse_links(body)}
        assert rewrite_targets(body, same) == body, f"mangled: {body!r}"


def test_a_label_containing_code_survives_the_move(store):
    """End to end, because the parser and the rewriter agreeing in isolation is
    not the claim — the claim is that a move does not eat the author's text."""
    target = store.write(body="the runbook", path="ops.a")
    src = store.write(body="See [[ops.a|the `runbook` doc]] for details.",
                      path="notes.a")
    store.move(None, target.id, "ops.moved")
    assert store.get(None, src.id).body == \
        "See [[ops.moved|the `runbook` doc]] for details."


def test_a_link_broken_across_a_code_span_is_not_a_link(store):
    """Backticks that swallow a `]]` and the `[[` after it look, in the blanked
    copy, like one enormous link spanning both. Treating it as one would delete
    everything between them."""
    target = store.write(body="the runbook", path="ops.a")
    store.write(body="the other", path="ops.b")
    src = store.write(body="[[ops.a`]] [[ops.b`]]", path="notes.a")
    store.move(None, target.id, "ops.moved")
    assert store.get(None, src.id).body == "[[ops.a`]] [[ops.b`]]"


# ─── the corpus that lived through the old behaviour ─────────────────────────
def test_a_body_left_stale_by_the_OLD_code_is_repaired_by_the_next_move(store):
    """Every store upgraded to this repair carries bodies naming a path their
    target left long ago — moves made before the repair existed, where the edge
    stayed pinned by id and only the text went stale. Refusing to touch a link
    whose text disagrees with the edge would leave exactly those unrepaired, and
    they are the ones a later occupant captures.

    No fixture can reach this state by writing normally, which is the point."""
    target = store.write(body="the runbook", path="ops.deploy")
    src = store.write(body="follow [[ops.deploy]] before shipping", path="notes.a")
    store.move(None, target.id, "ops.release")

    # Put the body back the way the old code left it: stale text, edge intact.
    with store._conn.cursor() as cur:
        cur.execute("UPDATE memory SET body='follow [[ops.deploy]] before shipping' "
                    "WHERE id=%s", (src.id,))
    store._conn.commit()

    store.move(None, target.id, "ops.shipping")

    assert store.get(None, src.id).body == "follow [[ops.shipping]] before shipping"
    squatter = store.write(body="unrelated", path="ops.deploy", if_moved="create")
    store.write(id=src.id, body=store.get(None, src.id).body + "\ntypo fixed")
    [edge] = store.links(None, src.id)["out"]
    assert edge["id"] == target.id and edge["id"] != squatter.id


# ─── a repair must not make a memory uneditable ──────────────────────────────
def test_a_repair_that_would_burst_the_body_ceiling_is_skipped(monkeypatch):
    """Growth is (number of links × how much longer the new path is), and both
    are ordinary. Pushing a body past the ceiling would leave its owner unable to
    edit their own memory — every write of it refused for size. The edge is
    pinned by id, so skipping costs the text, not the link."""
    _clean_env(monkeypatch)
    monkeypatch.setenv("MEMGRES_MAX_BODY_BYTES", "400")
    monkeypatch.setenv("MEMGRES_MAX_WRITE_BYTES", "400")
    conn = psycopg.connect(DSN)
    cfg = load()
    migrate(conn, cfg)
    s = Store(cfg, conn=conn)

    target = s.write(body="t", path="ops.a")
    body = " ".join("[[ops.a]]" for _ in range(30))          # 299 B
    src = s.write(body=body, path="notes.a")

    s.move(None, target.id, "ops.a_much_longer_name")        # would be ~750 B

    kept = s.get(None, src.id)
    assert kept.body == body                                  # text untouched
    assert [e["id"] for e in s.links(None, src.id)["out"]] == [target.id] * 30
    s.write(id=src.id, body=body + " ok")                     # still editable
    conn.close()


def test_an_expired_referrer_is_left_alone(monkeypatch):
    """It is invisible to every read and the sweeper is about to delete it.
    Rewriting it spends a history row and a re-index on a body nobody can see."""
    _clean_env(monkeypatch)
    monkeypatch.setenv("MEMGRES_RETENTION_DAYS", "7")
    conn = psycopg.connect(DSN)
    cfg = load()
    migrate(conn, cfg)
    s = Store(cfg, conn=conn)

    target = s.write(body="the runbook", path="ops.a")
    src = s.write(body="see [[ops.a]]", path="notes.a")
    with conn.cursor() as cur:
        cur.execute("UPDATE memory SET expires_at = now() - interval '1 day' "
                    "WHERE id=%s", (src.id,))
    conn.commit()

    s.move(None, target.id, "ops.moved")

    with conn.cursor() as cur:
        cur.execute("SELECT body, seq FROM memory WHERE id=%s", (src.id,))
        body, seq = cur.fetchone()
    assert body == "see [[ops.a]]" and seq == 1
    conn.close()
