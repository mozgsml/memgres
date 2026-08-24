"""Adversarial multi-tenant isolation tests.

Every test here is an *attack*: a caller holding a valid token for their own
user tries to reach another tenant's data or exceed their permission. All must
fail closed. If any of these ever passes, isolation is broken.
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
from memgres.identity import AuthError, SpaceNotFound, new_token  # noqa: E402
from memgres.store import Store, NotFound  # noqa: E402

DSN = os.environ.get("MEMGRES_TEST_DSN",
                     "postgresql://memgres:memgres@localhost:55432/memgres")


def _reachable() -> bool:
    try:
        psycopg.connect(DSN, connect_timeout=2).close()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _reachable(), reason="no test Postgres")

# any denial is acceptable — the point is the op does NOT succeed
DENIED = (AuthError, SpaceNotFound, NotFound, PermissionError)


@pytest.fixture
def env(monkeypatch):
    with psycopg.connect(DSN, autocommit=True) as c, c.cursor() as cur:
        cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    for k in list(os.environ):
        if k.startswith("MEMGRES_"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("MEMGRES_DATABASE_URL", DSN)
    monkeypatch.setenv("MEMGRES_EMBED_PROVIDER", "none")
    monkeypatch.setenv("MEMGRES_FTS_LANGUAGE", "simple")
    base = load()
    setup = psycopg.connect(DSN, autocommit=True)
    migrate(setup, base)
    stores = []

    def make_store(key_mode="managed", admin_token=""):
        cfg = dataclasses.replace(base, key_mode=key_mode, admin_token=admin_token)
        s = Store(cfg, conn=psycopg.connect(DSN))
        s._own_conn = True
        stores.append(s)
        return s

    yield setup, make_store
    for s in stores:
        s.close()
    setup.close()


def _user_with_token(setup, name, permission="write", namespace=None):
    uid = ident.create_user(setup, name=name)
    nsid = ident.create_namespace(setup, uid, namespace) if namespace else None
    secret, _ = ident.issue_token(setup, uid, namespace_id=nsid,
                                  permission=permission)
    return uid, secret, nsid


# ─── the core attack: reach another tenant's memory ──────────────────────────
def test_cannot_read_victim_memory_by_uuid(env):
    setup, make_store = env
    s = make_store("managed")
    _, victim_tok, _ = _user_with_token(setup, "victim", namespace="secrets")
    _, attacker_tok, _ = _user_with_token(setup, "attacker", namespace="secrets")

    vmem = s.write(victim_tok, body="launch codes\n", space="secrets")

    # attacker knows the exact UUID and tries every addressing trick
    with pytest.raises(DENIED):                       # default space
        s.get(attacker_tok, vmem.id)
    with pytest.raises(DENIED):                       # their OWN 'secrets' by name
        s.get(attacker_tok, vmem.id, space="secrets")
    # victim's namespace id directly
    _, _, victim_ns = _user_with_token(setup, "probe", namespace="x")  # noqa
    with pytest.raises(DENIED):
        # attacker guesses victim's namespace id — must be unreachable
        s.get(attacker_tok, vmem.id, space_id=_ns_of(setup, "victim", "secrets"))


def _ns_of(setup, user_name, ns_name):
    with setup.cursor() as cur:
        cur.execute("SELECT n.id FROM namespace n JOIN app_user u "
                    "ON u.id=n.owner_user_id WHERE u.name=%s AND n.name=%s",
                    (user_name, ns_name))
        return str(cur.fetchone()[0])


def test_cannot_recall_across_tenants(env):
    setup, make_store = env
    s = make_store("managed")
    _, victim_tok, _ = _user_with_token(setup, "victim", namespace="n")
    _, attacker_tok, _ = _user_with_token(setup, "attacker", namespace="n")
    s.write(victim_tok, body="victim only secret data\n", space="n")
    s.write(attacker_tok, body="attacker own note\n", space="n")
    # attacker's recall never sees the victim's row
    hits = s.recall(attacker_tok, "secret data", space="n")
    assert all("victim" not in h.snippet for h in hits)
    assert hits == [] or all("attacker" in h.snippet for h in hits)


def test_cannot_forget_or_move_victim_memory(env):
    setup, make_store = env
    s = make_store("managed")
    _, victim_tok, _ = _user_with_token(setup, "victim", namespace="n")
    _, attacker_tok, _ = _user_with_token(setup, "attacker", namespace="n")
    vmem = s.write(victim_tok, body="keep me\n", path="root.a", space="n")

    # forget matches zero rows in the attacker's namespace → False (idempotent
    # delete; returns False for both "not yours" and "doesn't exist" → no leak),
    # and crucially does NOT delete the victim's row.
    assert s.forget(attacker_tok, vmem.id, space="n") is False
    with pytest.raises(DENIED):
        s.move(attacker_tok, vmem.id, "root.b", space="n")
    with pytest.raises(DENIED):
        s.history(attacker_tok, vmem.id, space="n")
    # victim's memory is untouched
    assert s.get(victim_tok, vmem.id, space="n").body == "keep me\n"
    assert s.get(victim_tok, vmem.id, space="n").path == "root.a"


def test_name_collision_does_not_cross_tenants(env):
    """Both own a namespace called 'notes'; a name must resolve to the caller's."""
    setup, make_store = env
    s = make_store("managed")
    _, alice_tok, _ = _user_with_token(setup, "alice", namespace="notes")
    _, bob_tok, _ = _user_with_token(setup, "bob", namespace="notes")
    amem = s.write(alice_tok, body="alice notes\n", space="notes")
    bmem = s.write(bob_tok, body="bob notes\n", space="notes")
    # bob naming 'notes' hits HIS notes, never alice's
    assert s.get(bob_tok, bmem.id, space="notes").body == "bob notes\n"
    with pytest.raises(DENIED):
        s.get(bob_tok, amem.id, space="notes")
    # recall in bob's 'notes' never returns alice's row
    assert all("alice" not in h.snippet for h in s.recall(bob_tok, "notes", space="notes"))


# ─── permission ceilings ─────────────────────────────────────────────────────
def test_read_token_cannot_mutate(env):
    setup, make_store = env
    s = make_store("managed")
    uid = ident.create_user(setup, name="u")
    ns = ident.create_namespace(setup, uid, "n")
    ro, _ = ident.issue_token(setup, uid, permission="read")
    rw, _ = ident.issue_token(setup, uid, permission="write")
    mem = s.write(rw, body="seed\n", space="n")
    # read is fine
    assert s.get(ro, mem.id, space="n").body == "seed\n"
    # every mutation denied for the read token
    with pytest.raises(DENIED):
        s.write(ro, body="x\n", space="n")
    with pytest.raises(DENIED):
        s.write(ro, id=mem.id, body="x\n", space="n")
    with pytest.raises(DENIED):
        s.move(ro, mem.id, "root.z", space="n")
    with pytest.raises(DENIED):
        s.forget(ro, mem.id, space="n")


def test_scoped_token_confined_to_its_namespace(env):
    setup, make_store = env
    s = make_store("managed")
    uid = ident.create_user(setup, name="u")
    a = ident.create_namespace(setup, uid, "a")
    b = ident.create_namespace(setup, uid, "b")
    scoped, _ = ident.issue_token(setup, uid, namespace_id=a, permission="write")
    # works in its own scope
    s.write(scoped, body="in a\n", space_id=a)
    # cannot touch the user's OTHER namespace, by id or name
    with pytest.raises(DENIED):
        s.write(scoped, body="in b\n", space_id=b)
    with pytest.raises(DENIED):
        s.recall(scoped, "x", space_id=b)
    # cannot create a brand new namespace
    with pytest.raises(DENIED):
        s.write(scoped, body="in c\n", space="c")


# ─── token lifecycle ─────────────────────────────────────────────────────────
def test_revoked_token_cannot_act(env):
    setup, make_store = env
    s = make_store("managed")
    uid = ident.create_user(setup, name="u")
    ns = ident.create_namespace(setup, uid, "n")
    secret, tid = ident.issue_token(setup, uid, permission="write")
    s.write(secret, body="before\n", space="n")
    ident.revoke_token(setup, tid)
    with pytest.raises(DENIED):
        s.get(secret, "whatever", space="n")
    with pytest.raises(DENIED):
        s.write(secret, body="after\n", space="n")


def test_random_and_malformed_tokens_rejected(env):
    setup, make_store = env
    s = make_store("managed")
    for bad in [new_token(), "garbage", "", "mgk_short"]:
        with pytest.raises(DENIED):
            s.write(bad, body="x\n")
        with pytest.raises(DENIED):
            s.recall(bad, "x")


def test_token_scoped_to_unreachable_namespace_cannot_read(env):
    """Regression for the composed cross-tenant read: a token wrongly scoped to a
    namespace its user can't reach must NOT resolve that namespace on the default
    path (it once defaulted to 'read' and leaked bodies via recall)."""
    setup, make_store = env
    s = make_store("managed")
    _, victim_tok, _ = _user_with_token(setup, "victim", namespace="vault")
    vmem = s.write(victim_tok, body="top secret payload\n", space="vault")
    vns = _ns_of(setup, "victim", "vault")

    # attacker's user; forge a token scoped to the victim's namespace id
    a_uid = ident.create_user(setup, name="attacker")
    bad, _ = ident.issue_token(setup, a_uid, namespace_id=vns, permission="read")

    with pytest.raises(DENIED):                 # default path — the old leak
        s.recall(bad, "secret payload")
    with pytest.raises(DENIED):                 # by-id path
        s.recall(bad, "secret payload", space_id=vns)
    with pytest.raises(DENIED):                 # get by known uuid + default
        s.get(bad, vmem.id)
    with pytest.raises(DENIED):
        s.get(bad, vmem.id, space_id=vns)


def test_open_mode_random_token_reads_nothing(env):
    """A well-formed but never-written token (open mode) creates nothing and
    cannot read another tenant's data."""
    setup, make_store = env
    s = make_store("open")
    _, victim_tok, _ = _user_with_token(setup, "victim", namespace="n")
    vmem = s.write(victim_tok, body="victim\n", space="n")   # victim pre-exists

    ghost = new_token()                     # valid format, no user yet
    with pytest.raises(DENIED):             # can't read victim's memory
        s.get(ghost, vmem.id, space_id=_ns_of(setup, "victim", "n"))
    with pytest.raises(DENIED):             # its own read makes/sees nothing
        s.recall(ghost, "victim")
    # and it created no user row by merely reading
    with setup.cursor() as cur:
        cur.execute("SELECT count(*) FROM app_user")
        assert cur.fetchone()[0] == 1       # only the victim


# ─── multi-namespace search: widening must never widen past what you reach ───
# `space="all"` is the one address that does not name its namespaces, so it is
# the one an attacker would reach for. These pin that it resolves from the
# caller's OWN reachability and nothing else.
def test_all_never_includes_an_unreachable_namespace(env):
    setup, make_store = env
    s = make_store("managed")
    v_uid = ident.create_user(setup, name="victim")
    vns = ident.create_namespace(setup, v_uid, "secrets")
    victim_tok, _ = ident.issue_token(setup, v_uid)
    s.write(victim_tok, body="launch codes\n", space="secrets")

    a_uid = ident.create_user(setup, name="attacker")
    ident.create_namespace(setup, a_uid, "mine")
    attacker_tok, _ = ident.issue_token(setup, a_uid)
    s.write(attacker_tok, body="codes for my own lunch\n", space="mine")

    hits = s.recall(attacker_tok, "launch codes", space="all")
    assert all(h.namespace != vns for h in hits)
    assert all("launch codes" not in (h.snippet or "") for h in hits)
    # and the same for the browse + title paths, which share the filter
    assert all(r["space_id"] != vns for r in s.list(attacker_tok, space="all"))
    assert all(r["space_id"] != vns
               for r in s.find(attacker_tok, "codes", space="all"))


def test_all_does_not_widen_a_scoped_token(env):
    """A token scoped to one namespace must stay there even when its user owns
    others — `all` means "all you reach", and a scoped token reaches one."""
    setup, make_store = env
    s = make_store("managed")
    uid = ident.create_user(setup, name="u")
    work = ident.create_namespace(setup, uid, "work")
    ident.create_namespace(setup, uid, "private")
    full, _ = ident.issue_token(setup, uid)
    s.write(full, body="work note\n", space="work")
    s.write(full, body="private diary entry\n", space="private")

    scoped, _ = ident.issue_token(setup, uid, namespace_id=work)
    hits = s.recall(scoped, "note diary entry", space="all")
    assert hits, "the scoped token should still see its own namespace"
    assert all(h.namespace == work for h in hits)
    assert all("diary" not in (h.snippet or "") for h in hits)


def test_listing_a_foreign_namespace_is_refused_not_dropped(env):
    """Naming an unreachable namespace must FAIL, not be silently skipped:
    a quietly-narrowed search returns a partial answer that looks complete."""
    setup, make_store = env
    s = make_store("managed")
    v_uid = ident.create_user(setup, name="victim")
    vns = ident.create_namespace(setup, v_uid, "secrets")

    a_uid = ident.create_user(setup, name="attacker")
    ident.create_namespace(setup, a_uid, "mine")
    attacker_tok, _ = ident.issue_token(setup, a_uid)

    with pytest.raises(DENIED):                      # foreign id alone
        s.recall(attacker_tok, "anything", space_id=vns)
    with pytest.raises(DENIED):                      # mixed with a legit one
        s.recall(attacker_tok, "anything", space="mine", space_id=vns)
    with pytest.raises(DENIED):                      # a name they do not own
        s.recall(attacker_tok, "anything", space=["mine", "secrets"])


def test_read_only_ceiling_holds_across_all_spaces(env):
    """`all` must not launder a read-only token into a writer anywhere."""
    setup, make_store = env
    s = make_store("managed")
    uid = ident.create_user(setup, name="u")
    ident.create_namespace(setup, uid, "a")
    ident.create_namespace(setup, uid, "b")
    writer, _ = ident.issue_token(setup, uid)
    s.write(writer, body="alpha note\n", space="a")
    s.write(writer, body="beta note\n", space="b")

    reader, _ = ident.issue_token(setup, uid, permission="read")
    assert len(s.recall(reader, "note", space="all")) == 2
    with pytest.raises(DENIED):
        s.write(reader, body="should not land\n", space="a")


def test_request_access_does_not_reveal_which_uuids_are_namespaces(env):
    """The oracle this closes: an unreachable namespace produced a request, a
    uuid that named nothing produced a foreign-key error. The difference told an
    outsider which uuids are real — membership-blind, and independent of the
    caller's own access. Both answers are now the same."""
    import uuid

    from memgres import admin

    setup, make_store = env
    make_store("managed")
    victim = ident.create_user(setup, name="victim")
    hidden = ident.create_namespace(setup, victim, "private")

    outsider = ident.create_user(setup, name="outsider")
    ident.create_namespace(setup, outsider, "own")
    tok, _ = ident.issue_token(setup, outsider)
    cfg = dataclasses.replace(load(), key_mode="managed")
    p = ident.resolve(setup, cfg, tok)

    real = admin.request_access(setup, p, namespace_id=hidden)
    fake = admin.request_access(setup, p, namespace_id=str(uuid.uuid4()))
    assert real == fake == {"status": "submitted"}

    # the request against the namespace that exists was in fact recorded…
    assert len(ident.list_requests(setup, hidden)) == 1
    # …and a malformed id is a plain argument error, which reveals nothing about
    # what exists — and must not abort the transaction with a driver fault
    with pytest.raises(ValueError):
        admin.request_access(setup, p, namespace_id="not-a-uuid")
    assert ident.reaches(setup, outsider, hidden) is None


def test_request_access_tells_you_about_your_own_access(env):
    """Reporting reachability leaks nothing: it is the caller's own membership,
    which `list_spaces` shows them anyway."""
    from memgres import admin

    setup, make_store = env
    make_store("managed")
    uid = ident.create_user(setup, name="u")
    ns = ident.create_namespace(setup, uid, "own")
    tok, _ = ident.issue_token(setup, uid)
    cfg = dataclasses.replace(load(), key_mode="managed")
    p = ident.resolve(setup, cfg, tok)

    assert admin.request_access(setup, p, namespace_id=ns) == {
        "status": "already_reachable", "permission": "admin"}
    assert ident.list_requests(setup, ns) == []
