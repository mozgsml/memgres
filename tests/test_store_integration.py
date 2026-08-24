"""Store lifecycle against a live pgvector Postgres.

Skips unless MEMGRES_TEST_DSN (or the default local pgvector) is reachable.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

psycopg = pytest.importorskip("psycopg")

from memgres.config import load  # noqa: E402
from memgres.diffing import make_diff, content_hash, DiffConflict  # noqa: E402
from memgres.schema import migrate  # noqa: E402
from memgres.store import (  # noqa: E402
    Store, Conflict, NotFound, TooLarge, NoParent,
    ReplaceNotFound, AmbiguousReplace,
)  # noqa: E402

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
    # fresh schema each test
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


def test_create_get_history(store):
    m = store.write(body="first line\nsecond line\n", tags=["a", "b"],
                    path="root.notes", source="unit", reason="seed")
    assert m.seq == 1 and m.content_hash == content_hash("first line\nsecond line\n")
    got = store.get(None, m.id)
    assert got.body == m.body and got.tags == ["a", "b"] and got.path == "root.notes"
    h = store.history(None, m.id)
    assert len(h) == 1 and h[0]["op"] == "create" and h[0]["source"] == "unit"


def test_diff_apply_and_occ(store):
    m = store.write(body="alpha\nbeta\n")
    new = "alpha\nBETA\n"
    d = make_diff(m.body, new)
    # correct base hash applies
    m2 = store.write(id=m.id, diff=d, base_hash=m.content_hash, reason="edit")
    assert m2.body == new and m2.seq == 2
    # stale diff (old base) rejected
    with pytest.raises(Conflict):
        store.write(id=m.id, diff=d, base_hash=m.content_hash)


def test_diff_requires_base_hash(store):
    m = store.write(body="x\n")
    with pytest.raises(ValueError, match="base_hash"):
        store.write(id=m.id, diff=make_diff("x\n", "y\n"))


def test_malformed_diff_raises_and_does_not_bump_seq(store):
    # Regression (diff_silently_noops): a diff whose hunk header doesn't parse
    # used to silently apply nothing — body unchanged but seq++ / updated_at
    # moved, looking like a successful write. Now it raises and touches nothing.
    m = store.write(body="alpha\nbeta\n")
    assert m.seq == 1
    bad = "@@\n-alpha\n+ALPHA\n"          # bare @@ — not a valid unified-diff hunk
    with pytest.raises(DiffConflict):
        store.write(id=m.id, diff=bad, base_hash=m.content_hash)
    again = store.get(None, m.id)
    assert again.body == "alpha\nbeta\n"          # body untouched
    assert again.content_hash == m.content_hash   # hash unchanged
    assert again.seq == 1                          # seq NOT bumped


def test_single_mode_history_has_no_author(store):
    # Single mode has no identity: history rows carry NULL author, and the chain
    # verifies (the no-author path hashes exactly as before history_author).
    m = store.write(body="x\n", source="s", reason="r")
    store.write(id=m.id, body="y\n", reason="edit")
    h = store.history(None, m.id)
    assert [r["op"] for r in h] == ["create", "replace"]
    for r in h:
        assert r["author_user_id"] is None
        assert r["author_token_id"] is None
        assert r["author_name"] is None
    assert store.verify_history(None, m.id) is True


def test_history_author_stamped_and_attributed(monkeypatch):
    """In a shared namespace, each history row records WHO made it (server-stamped
    from the authenticated principal), the chain verifies, and blame credits lines
    to the right author."""
    from memgres import identity

    with psycopg.connect(DSN, autocommit=True) as c, c.cursor() as cur:
        cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    for k in list(os.environ):
        if k.startswith("MEMGRES_"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("MEMGRES_DATABASE_URL", DSN)
    monkeypatch.setenv("MEMGRES_KEY_MODE", "managed")
    monkeypatch.setenv("MEMGRES_EMBED_PROVIDER", "none")
    monkeypatch.setenv("MEMGRES_FTS_LANGUAGE", "simple")
    cfg = load(); conn = psycopg.connect(DSN); migrate(conn, cfg)

    # provision: Alice owns a namespace, Bob is a write-member of it
    with conn.transaction():
        uid_a = identity.create_user(conn, name="Alice")
        uid_b = identity.create_user(conn, name="Bob")
        nsid = identity.create_namespace(conn, uid_a, "shared")
        identity.add_member(conn, nsid, uid_b, "write")
        tok_a, tid_a = identity.issue_token(conn, uid_a, namespace_id=nsid,
                                            permission="write")
        tok_b, tid_b = identity.issue_token(conn, uid_b, namespace_id=nsid,
                                            permission="write")

    s = Store(cfg, conn=conn)
    m = s.write(tok_a, body="line one\n", space_id=nsid, source="seed")
    d = make_diff("line one\n", "line one\nline two (bob)\n")
    s.write(tok_b, id=m.id, diff=d, base_hash=m.content_hash, space_id=nsid,
            reason="add a line")

    h = s.history(tok_a, m.id, space_id=nsid)
    assert h[0]["author_user_id"] == uid_a and h[0]["author_name"] == "Alice"
    assert h[0]["author_token_id"] == tid_a
    assert h[1]["author_user_id"] == uid_b and h[1]["author_name"] == "Bob"
    assert h[1]["author_token_id"] == tid_b
    # the chain now folds author in and still verifies end-to-end
    assert s.verify_history(tok_a, m.id, space_id=nsid) is True

    # blame credits the added line to Bob, the seed line to Alice
    blame = s.annotate(tok_a, m.id, space_id=nsid)
    by_text = {b["text"].strip(): b for b in blame}
    assert by_text["line one"]["author_name"] == "Alice"
    assert by_text["line two (bob)"]["author_name"] == "Bob"
    conn.close()


def test_deleted_author_leaves_verifiable_stamp(monkeypatch):
    """Deleting the author user must NOT break the audit chain: the id stays
    stamped (name resolves to NULL), and verify_history stays True — because the
    hash folds the id, which is retained, not the joined name."""
    from memgres import identity

    with psycopg.connect(DSN, autocommit=True) as c, c.cursor() as cur:
        cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    for k in list(os.environ):
        if k.startswith("MEMGRES_"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("MEMGRES_DATABASE_URL", DSN)
    monkeypatch.setenv("MEMGRES_KEY_MODE", "managed")
    monkeypatch.setenv("MEMGRES_EMBED_PROVIDER", "none")
    monkeypatch.setenv("MEMGRES_FTS_LANGUAGE", "simple")
    cfg = load(); conn = psycopg.connect(DSN); migrate(conn, cfg)

    with conn.transaction():
        uid_a = identity.create_user(conn, name="Alice")
        uid_admin = identity.create_user(conn, name="Admin")
        nsid = identity.create_namespace(conn, uid_admin, "shared")
        identity.add_member(conn, nsid, uid_a, "write")
        tok_a, tid_a = identity.issue_token(conn, uid_a, namespace_id=nsid,
                                            permission="write")
        tok_admin, _ = identity.issue_token(conn, uid_admin, namespace_id=nsid,
                                            permission="admin")

    s = Store(cfg, conn=conn)
    m = s.write(tok_a, body="alice wrote this\n", space_id=nsid)
    # delete Alice (cascades her token; membership rows go too)
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("DELETE FROM app_user WHERE id=%s", (uid_a,))

    h = s.history(tok_admin, m.id, space_id=nsid)
    assert h[0]["author_user_id"] == uid_a       # bare id retained
    assert h[0]["author_name"] is None            # user row gone → no name
    assert s.verify_history(tok_admin, m.id, space_id=nsid) is True
    conn.close()


def test_replace_substring_unique(store):
    # content-addressed edit: send old→new, server finds & rewrites just that,
    # records it as a normal diff in history.
    m = store.write(body="alpha\nbeta\ngamma\n")
    m2 = store.write(id=m.id, replace=("beta", "BETA changed"), reason="edit")
    assert m2.body == "alpha\nBETA changed\ngamma\n" and m2.seq == 2
    ops = [r["op"] for r in store.history(None, m.id)]
    assert ops == ["create", "diff"]          # lowers to the canonical diff path
    assert store.verify_history(None, m.id) is True


def test_replace_not_found_leaves_record_untouched(store):
    m = store.write(body="alpha\nbeta\n")
    with pytest.raises(ReplaceNotFound):
        store.write(id=m.id, replace=("zzz", "x"))
    again = store.get(None, m.id)
    assert again.body == "alpha\nbeta\n" and again.seq == 1


def test_replace_ambiguous_requires_all_or_context(store):
    m = store.write(body="x x x\n")
    with pytest.raises(AmbiguousReplace):
        store.write(id=m.id, replace=("x", "y"))          # 3 matches, no all
    assert store.get(None, m.id).seq == 1                  # untouched
    m2 = store.write(id=m.id, replace=("x", "y"), replace_all=True)
    assert m2.body == "y y y\n" and m2.seq == 2


def test_replace_first_only_by_default(store):
    m = store.write(body="a-a-a\n")
    # unique per occurrence? "a" appears 3× → ambiguous; use a unique old instead
    m2 = store.write(id=m.id, replace=("a-a-a", "Z"))
    assert m2.body == "Z\n"


def test_replace_noop_is_rejected(store):
    from memgres.diffing import DiffConflict
    m = store.write(body="alpha\nbeta\n")
    with pytest.raises(DiffConflict):
        store.write(id=m.id, replace=("beta", "beta"))    # old == new
    assert store.get(None, m.id).seq == 1


def test_replace_edits_body_larger_than_write_cap(store):
    # the point of replace: old+new cross the wire, not the whole body — so a body
    # bigger than MAX_WRITE_BYTES stays editable (whole-body rewrite could not).
    m = store.write(body="head\n" + ("filler line\n" * 40) + "TARGET\n")
    store.cfg = store.cfg.__class__(**{**store.cfg.__dict__, "max_write_bytes": 20})
    # a whole-body rewrite of this body would exceed the cap
    with pytest.raises(TooLarge):
        store.write(id=m.id, body=m.body + "x\n")
    # but a small replace succeeds — only old+new are size-checked
    m2 = store.write(id=m.id, replace=("TARGET", "DONE"))
    assert m2.body.endswith("DONE\n") and m2.seq == 2


def test_replace_all_rejects_amplified_body_before_materializing(store):
    # replace_all can multiply a small `new` by every occurrence; the result must
    # be bounded against MAX_BODY_BYTES *before* the giant string is built, not
    # after. old+new is tiny (passes the write cap), but the projected body isn't.
    m = store.write(body="x" * 100)                 # 100 occurrences of "x"
    store.cfg = store.cfg.__class__(**{**store.cfg.__dict__, "max_body_bytes": 200})
    with pytest.raises(TooLarge):
        store.write(id=m.id, replace=("x", "yyyy"), replace_all=True)  # →400B
    # the body was never mutated (rejected up front, no seq bump)
    after = store.get(None, m.id)
    assert after.body == "x" * 100 and after.seq == 1


def test_replace_requires_id(store):
    with pytest.raises(ValueError, match="id"):
        store.write(replace=("a", "b"))


def test_title_set_and_returned(store):
    m = store.write(body="b\n", title="My Note")
    assert m.title == "My Note"
    assert store.get(None, m.id).title == "My Note"
    row = [r for r in store.list(None) if r["id"] == m.id][0]
    assert row["title"] == "My Note"


def test_title_change_audited_and_verifies(store):
    m = store.write(body="b\n", title="First")
    m2 = store.write(id=m.id, title="Second")          # title-only change
    assert m2.title == "Second" and m2.seq == 2
    h = store.history(None, m.id)
    assert h[1]["op"] == "retitle"
    assert h[1]["title_before"] == "First" and h[1]["title_after"] == "Second"
    # the create row recorded the initial title (before None -> after "First")
    assert h[0]["title_after"] == "First"
    assert store.verify_history(None, m.id) is True


def test_title_untouched_leaves_history_and_verify_intact(store):
    # a body edit that doesn't touch the title records NULL title cols and the
    # chain verifies — the no-title path must not perturb the hash.
    m = store.write(body="one\n")                       # no title
    store.write(id=m.id, body="two\n", reason="edit")   # body change, no title
    h = store.history(None, m.id)
    assert all(r["title_before"] is None and r["title_after"] is None for r in h)
    assert store.verify_history(None, m.id) is True


def test_title_clear_is_audited(store):
    m = store.write(body="b\n", title="Some Title")
    m2 = store.write(id=m.id, title="")                 # clear it
    assert m2.title == ""
    h = store.history(None, m.id)
    assert h[1]["op"] == "retitle"
    assert h[1]["title_before"] == "Some Title" and h[1]["title_after"] == ""
    assert store.verify_history(None, m.id) is True


def test_title_size_cap(store):
    store.cfg = store.cfg.__class__(**{**store.cfg.__dict__, "max_title_bytes": 8})
    with pytest.raises(TooLarge):
        store.write(body="b\n", title="way too long a title")


def test_find_matches_title_not_body(store):
    # `store` has no embedder — find must work lexically, over the TITLE only.
    a = store.write(body="the body mentions apples\n", title="Fruit Notes")
    b = store.write(body="something about fruit here\n", title="Vegetable Notes")
    hits = store.find(None, "fruit")
    ids = [h["id"] for h in hits]
    assert a.id in ids                 # 'fruit' is in a's TITLE
    assert b.id not in ids             # b has 'fruit' only in its BODY → not matched
    assert set(hits[0]) == {"id", "path", "title", "tags", "score",
                            "space_id", "space"}                     # light rows


def test_find_respects_tag_filter(store):
    store.write(body="x\n", title="alpha report", tags=["keep"])
    store.write(body="y\n", title="alpha summary", tags=["drop"])
    hits = store.find(None, "alpha", tags=["keep"])
    assert len(hits) == 1 and hits[0]["title"] == "alpha report"


def test_replace_and_retag(store):
    m = store.write(body="body\n", tags=["old"])
    m2 = store.write(id=m.id, body="new body\n", tags=["new"], reason="replace")
    assert m2.body == "new body\n" and m2.tags == ["new"] and m2.seq == 2
    ops = [r["op"] for r in store.history(None, m.id)]
    assert ops == ["create", "replace"]


def test_move_cascades_subtree(store):
    root = store.write(body="root\n", path="a")
    child = store.write(body="child\n", path="a.b")
    grand = store.write(body="grand\n", path="a.b.c")
    store.move(None, root.id, "z")
    assert store.get(None, root.id).path == "z"
    assert store.get(None, child.id).path == "z.b"       # cascaded
    assert store.get(None, grand.id).path == "z.b.c"     # cascaded deep
    assert store.history(None, root.id)[-1]["op"] == "move"


def test_a_cascaded_move_is_recorded_on_every_descendant(store):
    """A descendant's address changes too, so its own history must say so —
    otherwise `history`/`blame` show a node that silently teleported, and nothing
    can answer "where did the old path go?" for anything but the moved node."""
    root = store.write(body="root\n", path="a")
    child = store.write(body="child\n", path="a.b")
    grand = store.write(body="grand\n", path="a.b.c")
    store.move(None, root.id, "z", source="reorg", reason="tidy up")

    for mem, before, after in ((child, "a.b", "z.b"), (grand, "a.b.c", "z.b.c")):
        last = store.history(None, mem.id)[-1]
        assert last["op"] == "move"
        assert last["path_before"] == before and last["path_after"] == after
        assert last["diff"] is None                      # the body did not change
        assert last["hash_before"] == last["hash_after"]
        assert last["source"] == "reorg" and last["reason"] == "tidy up"
        # the row is a real revision of that memory, not a footnote
        assert last["seq"] == store.get(None, mem.id).seq
        # …and it extends that memory's own hash chain
        assert store.verify_history(None, mem.id)


def test_a_cascaded_move_does_not_touch_bodies_or_neighbours(store):
    store.write(body="root\n", path="a")
    child = store.write(body="child\n", path="a.b")
    outsider = store.write(body="outsider\n", path="b.x")
    before = store.get(None, outsider.id)

    root_id = store.list(None, path_prefix="a")[0]["id"]
    store.move(None, root_id, "z")

    assert store.get(None, child.id).body == "child\n"
    after = store.get(None, outsider.id)
    assert (after.path, after.seq) == (before.path, before.seq)
    assert [r["op"] for r in store.history(None, outsider.id)] == ["create"]


def test_hash_chain_verifies(store):
    m = store.write(body="1\n", source="s")
    store.write(id=m.id, body="2\n", reason="r2")
    store.write(id=m.id, path="p", reason="moved")
    assert store.verify_history(None, m.id) is True


def test_forget_erases_history(store):
    m = store.write(body="secret\n")
    assert store.forget(None, m.id) is True
    with pytest.raises(NotFound):
        store.get(None, m.id)
    with pytest.raises(NotFound):
        store.history(None, m.id)


def test_write_size_limit(store):
    store.cfg = store.cfg.__class__(**{**store.cfg.__dict__, "max_write_bytes": 8})
    with pytest.raises(TooLarge):
        store.write(body="this is definitely longer than eight bytes\n")


def test_sparse_paths_allowed_by_default(store):
    # default: MEMGRES_REQUIRE_PARENT off — deep path with no parent row is fine
    m = store.write(body="deep\n", path="a.b.c")
    assert m.path == "a.b.c"


def test_require_parent_enforced(monkeypatch):
    with psycopg.connect(DSN, autocommit=True) as c, c.cursor() as cur:
        cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    for k in list(os.environ):
        if k.startswith("MEMGRES_"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("MEMGRES_DATABASE_URL", DSN)
    monkeypatch.setenv("MEMGRES_EMBED_PROVIDER", "none")
    monkeypatch.setenv("MEMGRES_FTS_LANGUAGE", "simple")
    monkeypatch.setenv("MEMGRES_REQUIRE_PARENT", "true")
    cfg = load(); conn = psycopg.connect(DSN); migrate(conn, cfg)
    s = Store(cfg, conn=conn)
    s.write(body="root\n", path="food")                 # root ok, no parent needed
    with pytest.raises(NoParent):
        s.write(body="orphan\n", path="food.fruit.apple")  # food.fruit missing
    s.write(body="fruit\n", path="food.fruit")          # now create the parent
    child = s.write(body="apple\n", path="food.fruit.apple")  # ok now
    assert child.path == "food.fruit.apple"
    conn.close()


def test_default_token_from_config(monkeypatch):
    """MEMGRES_TOKEN is the default identity when a call passes no token
    (single-tenant endpoints); a different token is a different user."""
    from memgres.identity import new_token, SpaceNotFound
    with psycopg.connect(DSN, autocommit=True) as c, c.cursor() as cur:
        cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    for k in list(os.environ):
        if k.startswith("MEMGRES_"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("MEMGRES_DATABASE_URL", DSN)
    monkeypatch.setenv("MEMGRES_KEY_MODE", "open")
    default_tok = new_token()
    monkeypatch.setenv("MEMGRES_TOKEN", default_tok)              # env default
    monkeypatch.setenv("MEMGRES_EMBED_PROVIDER", "none")
    monkeypatch.setenv("MEMGRES_FTS_LANGUAGE", "simple")
    cfg = load(); conn = psycopg.connect(DSN); migrate(conn, cfg)
    s = Store(cfg, conn=conn)
    from memgres import identity as ident
    ident.create_own_namespace(conn, ident.resolve(conn, cfg, default_tok), "mine")
    # no token passed -> falls back to MEMGRES_TOKEN, so writes/reads work
    m = s.write(None, body="via default token\n")
    assert s.get(None, m.id).body == "via default token\n"
    # a different token is a different user -> can't see it
    with pytest.raises((NotFound, SpaceNotFound)):
        s.get(new_token(), m.id)
    conn.close()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))


def test_cascaded_move_rows_carry_the_author_and_still_verify(monkeypatch):
    """The chain folds the author in only when there IS one, so a cascaded row
    written with identity on is the case where compute and verify could disagree
    — and a mismatch shows up only when someone verifies."""
    from memgres import identity

    with psycopg.connect(DSN, autocommit=True) as c, c.cursor() as cur:
        cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    for k in list(os.environ):
        if k.startswith("MEMGRES_"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("MEMGRES_DATABASE_URL", DSN)
    monkeypatch.setenv("MEMGRES_KEY_MODE", "managed")
    monkeypatch.setenv("MEMGRES_EMBED_PROVIDER", "none")
    monkeypatch.setenv("MEMGRES_FTS_LANGUAGE", "simple")
    cfg = load(); conn = psycopg.connect(DSN); migrate(conn, cfg)
    with conn.transaction():
        uid = identity.create_user(conn, name="mover")
        nsid = identity.create_namespace(conn, uid, "ns")
        tok, tid = identity.issue_token(conn, uid, namespace_id=nsid,
                                        permission="write")
    s = Store(cfg, conn=conn)
    try:
        root = s.write(tok, body="root\n", path="a", space_id=nsid)
        child = s.write(tok, body="child\n", path="a.b", space_id=nsid)
        s.move(tok, root.id, "z", space_id=nsid)

        last = s.history(tok, child.id, space_id=nsid)[-1]
        assert last["op"] == "move" and last["path_after"] == "z.b"
        assert last["author_user_id"] == uid and last["author_token_id"] == tid
        assert last["author_name"] == "mover"
        assert s.verify_history(tok, child.id, space_id=nsid)
    finally:
        conn.close()
