"""Searching across several namespaces: how an address resolves.

`test_security_integration.py` covers the attacks (a caller must never widen
past what they reach). This file covers the CONTRACT the honest caller sees:
when naming a namespace is optional, when it is required, what `all` means, and
what each hit says about where it came from.

The rule under test, in one sentence: exactly one reachable namespace resolves
on its own, and beyond that you say which — because searching one of your four
namespaces and reporting "nothing found" is a partial answer wearing the clothes
of a complete one.
"""

import dataclasses
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

psycopg = pytest.importorskip("psycopg")

from memgres.config import load  # noqa: E402
from memgres.schema import migrate  # noqa: E402
from memgres import identity as ident  # noqa: E402
from memgres.identity import SpaceAmbiguous, SpaceNotFound  # noqa: E402
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
def env(monkeypatch):
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
    base = load()
    setup = psycopg.connect(DSN, autocommit=True)
    migrate(setup, base)
    store = Store(dataclasses.replace(base, key_mode="managed"),
                  conn=psycopg.connect(DSN))
    store._own_conn = True
    yield setup, store
    store.close()
    setup.close()


def _owner(setup, name, *spaces):
    """A user owning `spaces`, plus an unscoped token for it."""
    uid = ident.create_user(setup, name=name)
    ids = [ident.create_namespace(setup, uid, s) for s in spaces]
    tok, _ = ident.issue_token(setup, uid)
    return uid, tok, ids


# ─── one namespace: nothing to choose, so nothing to say ─────────────────────
def test_a_single_namespace_needs_no_address(env):
    setup, s = env
    _, tok, _ = _owner(setup, "solo", "only")
    s.write(tok, body="lonely note\n", space="only")

    assert len(s.recall(tok, "lonely")) == 1
    assert len(s.list(tok)) == 1
    assert len(s.recall(tok, "note", space="only", bodies=False)) == 1


# ─── several namespaces: silence is refused, and the refusal is useful ───────
def test_several_namespaces_require_an_address(env):
    setup, s = env
    _, tok, _ = _owner(setup, "multi", "work", "home")
    s.write(tok, body="a note at work\n", space="work")

    with pytest.raises(SpaceAmbiguous) as e:
        s.recall(tok, "note")
    msg = str(e.value)
    assert "work" in msg and "home" in msg     # names the candidates…
    assert "all" in msg                        # …and the way to take them all

    for call in (lambda: s.list(tok),
                 lambda: s.recall(tok, "note", bodies=False)):
        with pytest.raises(SpaceAmbiguous):
            call()


# ─── naming them ─────────────────────────────────────────────────────────────
def test_all_covers_every_reachable_namespace(env):
    setup, s = env
    _, tok, _ = _owner(setup, "multi", "work", "home")
    s.write(tok, body="apple at work\n", space="work")
    s.write(tok, body="apple at home\n", space="home")

    hits = s.recall(tok, "apple", space="all")
    assert {h.space for h in hits} == {"work", "home"}
    assert len(s.list(tok, space="all")) == 2


def test_a_list_of_names_covers_exactly_those(env):
    setup, s = env
    _, tok, _ = _owner(setup, "multi", "a", "b", "c")
    for name in ("a", "b", "c"):
        s.write(tok, body=f"apple in {name}\n", space=name)

    hits = s.recall(tok, "apple", space=["a", "c"])
    assert {h.space for h in hits} == {"a", "c"}


def test_a_shared_namespace_joins_by_id(env):
    """A shared namespace can be addressed by id, and the two forms combine in
    one search. (It answers to its NAME too, unless that name is ambiguous for
    you — which is what aliases are for; covered in test_identity_integration.)"""
    setup, s = env
    _, mine_tok, _ = _owner(setup, "me", "mine")
    other_uid, other_tok, other_ids = _owner(setup, "them", "theirs")
    s.write(other_tok, body="apple they shared\n", space="theirs")
    s.write(mine_tok, body="apple of my own\n", space="mine")

    with setup.cursor() as cur:
        cur.execute("SELECT id FROM app_user WHERE name='me'")
        my_uid = str(cur.fetchone()[0])
    ident.add_member(setup, other_ids[0], my_uid, "read")

    hits = s.recall(mine_tok, "apple", space="mine", space_id=other_ids[0])
    assert {h.snippet.strip() for h in hits} == {"apple they shared",
                                                 "apple of my own"}
    # and `all` picks the shared one up without being told about it
    assert len(s.recall(mine_tok, "apple", space="all")) == 2


def test_the_same_namespace_twice_is_searched_once(env):
    setup, s = env
    _, tok, ids = _owner(setup, "multi", "work", "home")
    m = s.write(tok, body="apple at work\n", space="work")

    hits = s.recall(tok, "apple", space="work", space_id=ids[0])
    assert [h.id for h in hits] == [m.id]


# ─── the `all` keyword's edges ───────────────────────────────────────────────
def test_all_with_explicit_ids_is_refused_as_contradictory(env):
    setup, s = env
    _, tok, ids = _owner(setup, "multi", "work", "home")
    with pytest.raises(SpaceAmbiguous):
        s.recall(tok, "apple", space="all", space_id=ids[0])


def test_a_namespace_actually_named_all_is_not_guessed_at(env):
    """The keyword collides with a real name: rather than pick an interpretation
    silently, say so and point at the unambiguous address."""
    setup, s = env
    _, tok, ids = _owner(setup, "unlucky", "all", "other")
    s.write(tok, body="apple\n", space="all")

    with pytest.raises(SpaceAmbiguous) as e:
        s.recall(tok, "apple", space="all")
    assert "space_id" in str(e.value)
    # the id still addresses it, so the caller is never stuck
    assert len(s.recall(tok, "apple", space_id=ids[0])) == 1


def test_all_inside_a_longer_list_is_a_literal_name(env):
    """The keyword only widens when it IS the whole address. Alongside other
    names it is read literally, so a list never covers more than it names.

    (A one-element list IS the keyword: over HTTP `?space=all` arrives as
    `["all"]`, and the wire cannot distinguish that from a bare string.)"""
    setup, s = env
    _, tok, _ = _owner(setup, "unlucky", "all", "other", "third")
    s.write(tok, body="apple in all\n", space="all")
    s.write(tok, body="apple in other\n", space="other")
    s.write(tok, body="apple in third\n", space="third")

    hits = s.recall(tok, "apple", space=["all", "other"])
    assert {h.space for h in hits} == {"all", "other"}   # not "third"


def test_all_with_no_namespaces_at_all_says_so(env):
    setup, s = env
    uid = ident.create_user(setup, name="bare")
    tok, _ = ident.issue_token(setup, uid)
    with pytest.raises(SpaceNotFound):
        s.recall(tok, "apple", space="all")


# ─── what a hit says about itself ────────────────────────────────────────────
def test_every_hit_carries_its_namespace(env):
    setup, s = env
    _, tok, ids = _owner(setup, "multi", "work", "home")
    s.write(tok, body="apple\n", title="Apple", space="work")

    [hit] = s.recall(tok, "apple", space="all")
    assert hit.space == "work" and hit.namespace == ids[0]
    assert hit.to_recall_dict()["space"] == "work"
    assert hit.to_recall_dict()["space_id"] == ids[0]

    [light] = s.recall(tok, "Apple", space="all", bodies=False)
    assert light.space == "work" and light.namespace == ids[0]

    [row] = s.list(tok, space="all")
    assert row["space"] == "work" and row["space_id"] == ids[0]


# ─── a superadmin reaches more than it belongs to, so `all` is two questions ──
def _superadmin(setup, name, *spaces):
    uid, tok, ids = _owner(setup, name, *spaces)
    ident.set_role(setup, uid, "superadmin")
    tok, _ = ident.issue_token(setup, uid, permission="admin")
    return uid, tok, ids


def test_all_is_refused_for_a_superadmin_that_would_under_answer(env):
    """The failure this closes: `all` returned the caller's MEMBERSHIPS, while a
    superadmin reads any namespace by id. Searching 2 of 3 namespaces and
    reporting nothing found is indistinguishable from an answer — the same class
    of silent partial result the whole addressing model exists to prevent."""
    setup, s = env
    _, root, _ = _superadmin(setup, "root", "ops")
    _, other, _ = _owner(setup, "tenant", "theirs")
    s.write(other, body="apple in someone else's space\n", space="theirs")
    s.write(root, body="apple in mine\n", space="ops")

    with pytest.raises(SpaceAmbiguous) as e:
        s.recall(root, "apple", space="all")
    msg = str(e.value)
    assert "superadmin" in msg and "'ops'" in msg      # names what it DOES cover
    assert "'*'" in msg                                # …and the wide word

    # `*` is the explicit wide read, and it sees both
    assert len(s.recall(root, "apple", space="*")) == 2
    # naming them still works, and stays narrow
    assert len(s.recall(root, "apple", space="ops")) == 1


def test_a_plain_user_never_sees_the_ambiguity_or_the_wide_word(env):
    """`all` is unchanged for everyone whose reach IS their memberships."""
    setup, s = env
    _, tok, _ = _owner(setup, "plain", "work", "home")
    _, other, _ = _owner(setup, "tenant", "theirs")
    s.write(other, body="apple elsewhere\n", space="theirs")
    s.write(tok, body="apple at work\n", space="work")

    assert len(s.recall(tok, "apple", space="all")) == 1     # no refusal
    with pytest.raises(ident.AuthError) as e:
        s.recall(tok, "apple", space="*")
    assert "superadmin" in str(e.value)


def test_a_superadmin_whose_memberships_cover_everything_is_not_bothered(env):
    """The refusal fires only where the two answers actually differ."""
    setup, s = env
    _, root, _ = _superadmin(setup, "root", "ops")
    s.write(root, body="apple\n", space="ops")
    assert len(s.recall(root, "apple", space="all")) == 1


def test_the_wide_word_does_not_widen_a_scoped_token(env):
    """A token pinned to one namespace was narrowed on purpose; the role behind
    it does not undo that."""
    setup, s = env
    uid, root, ids = _superadmin(setup, "root", "ops", "second")
    _, other, _ = _owner(setup, "tenant", "theirs")
    s.write(other, body="apple elsewhere\n", space="theirs")
    s.write(root, body="apple in ops\n", space_id=ids[0])
    scoped, _ = ident.issue_token(setup, uid, namespace_id=ids[0],
                                  permission="admin")

    hits = s.recall(scoped, "apple", space="*")
    assert [h.namespace for h in hits] == [ids[0]]


def test_naming_no_namespace_at_all_gets_the_same_refusal(env):
    """The trap reached by saying nothing. A superadmin with ONE membership was
    silently answered from that one namespace — the exact partial answer the
    keyword refusal exists to prevent, one function away from the fix."""
    setup, s = env
    _, root, _ = _superadmin(setup, "root", "ops")
    _, other, _ = _owner(setup, "tenant", "theirs")
    s.write(other, body="apple elsewhere\n", space="theirs")

    with pytest.raises(SpaceAmbiguous) as e:
        s.recall(root, "apple")                     # no space, no space_id
    assert "'*'" in str(e.value)
    assert len(s.recall(root, "apple", space="*")) == 1

    # a WRITE with no address still resolves to the single membership: it has to
    # land somewhere, and nothing is left out of an answer
    s.write(root, body="apple of my own\n")
    assert len(s.recall(root, "apple", space="ops")) == 1


def test_a_stranger_cannot_take_the_wide_keyword_away(env):
    """`*` was checked against EVERY name in the deployment, so any tenant could
    disable it for the superadmin by naming a namespace `*` — and with `all`
    already refused, the two errors pointed at each other and left the operator
    enumerating uuids. A stranger's choice of name must not reach into what this
    caller's words mean."""
    setup, s = env
    _, root, _ = _superadmin(setup, "root", "ops")
    _, tenant, ids = _owner(setup, "tenant", "*")
    s.write(tenant, body="apple in the star\n", space_id=ids[0])
    s.write(root, body="apple in ops\n", space="ops")

    assert len(s.recall(root, "apple", space="*")) == 2      # still works


def test_owning_a_namespace_named_like_the_wide_keyword_is_ambiguous_not_forbidden(env):
    """For the owner of a namespace named `*`, that name is the likely meaning —
    so the answer is the ambiguity refusal, not a lecture about superadmins."""
    setup, s = env
    _, tok, ids = _owner(setup, "plain", "*", "other")
    s.write(tok, body="apple\n", space_id=ids[0])

    with pytest.raises(SpaceAmbiguous) as e:
        s.recall(tok, "apple", space="*")
    assert "space_id" in str(e.value)
    assert len(s.recall(tok, "apple", space_id=ids[0])) == 1


def test_a_namespace_actually_named_like_a_keyword_is_not_swallowed(env):
    """Namespace names are free text, so a keyword can collide with a real name.
    Neither meaning is assumed — the call is refused and told to use the id."""
    setup, s = env
    _, tok, ids = _owner(setup, "clever", "all", "other")
    s.write(tok, body="apple\n", space_id=ids[0])

    with pytest.raises(SpaceAmbiguous) as e:
        s.recall(tok, "apple", space="all")
    assert "space_id" in str(e.value)
    assert len(s.recall(tok, "apple", space_id=ids[0])) == 1


# ─── single mode is untouched: no identity, one implicit space ───────────────
def test_single_mode_ignores_addressing(env):
    setup, _ = env
    base = load()
    s = Store(dataclasses.replace(base, key_mode="single"),
              conn=psycopg.connect(DSN))
    s._own_conn = True
    try:
        s.write(None, body="apple\n")
        assert len(s.recall(None, "apple")) == 1
        assert len(s.recall(None, "apple", space="all")) == 1
        [hit] = s.recall(None, "apple")
        assert hit.space is None          # there is no namespace to name
    finally:
        s.close()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
