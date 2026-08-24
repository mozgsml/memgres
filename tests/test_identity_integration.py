"""Identity / tenancy / tokens against a live Postgres.

Skips unless MEMGRES_TEST_DSN (or the default local pgvector) is reachable.
Covers token format + auth, space resolution (id-canonical, name = own-only),
lazy creation, permission ceilings, token scope, and request-access.
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
from memgres.identity import (  # noqa: E402
    AuthError, SpaceNotFound, Principal, new_token, valid_format,
    TOKEN_RE,
)
from memgres.store import NotFound as NotFoundErr  # noqa: E402

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
    monkeypatch.setenv("MEMGRES_EMBED_PROVIDER", "none")
    monkeypatch.setenv("MEMGRES_FTS_LANGUAGE", "simple")
    base = load()
    c = psycopg.connect(DSN, autocommit=True)
    migrate(c, base)
    c._base_cfg = base            # stash for cfg() helper
    yield c
    c.close()


def cfg(conn, *, key_mode="managed", admin_token=""):
    return dataclasses.replace(conn._base_cfg, key_mode=key_mode,
                               admin_token=admin_token)


# ─── token format ────────────────────────────────────────────────────────────
def test_token_format():
    t = new_token()
    assert TOKEN_RE.match(t) and valid_format(t)
    assert t.startswith("mgk_") and len(t) == 4 + 43
    assert not valid_format("mgk_short")
    assert not valid_format("nope_" + "a" * 43)
    assert not valid_format("")


# ─── authentication ──────────────────────────────────────────────────────────
def test_admin_token_resolves(conn):
    c = cfg(conn, admin_token="s3cret-admin")
    p = ident.resolve(conn, c, "s3cret-admin")
    assert p.is_admin and p.permission == "admin" and p.user_id is None


def test_unknown_token_managed_rejected(conn):
    with pytest.raises(AuthError):
        ident.resolve(conn, cfg(conn, key_mode="managed"), new_token())


def test_unknown_token_open_is_provisional(conn):
    p = ident.resolve(conn, cfg(conn, key_mode="open"), new_token())
    assert p.provisional and p.user_id is None and p.token_hash


def test_malformed_and_missing_token(conn):
    c = cfg(conn, key_mode="open")
    with pytest.raises(AuthError):
        ident.resolve(conn, c, "garbage")
    with pytest.raises(AuthError):
        ident.resolve(conn, c, "")


def test_issued_token_resolves_with_ceiling_and_scope(conn):
    uid = ident.create_user(conn, name="alice")
    ns = ident.create_namespace(conn, uid, "notes")
    secret, tid = ident.issue_token(conn, uid, namespace_id=ns,
                                    permission="read", label="ro")
    p = ident.resolve(conn, cfg(conn), secret)
    assert p.user_id == uid and p.permission == "read"
    assert p.scope_namespace_id == ns and p.token_id == tid


def test_revoked_and_expired_rejected(conn):
    uid = ident.create_user(conn)
    secret, tid = ident.issue_token(conn, uid)
    ident.revoke_token(conn, tid)
    with pytest.raises(AuthError, match="revoked"):
        ident.resolve(conn, cfg(conn), secret)

    import datetime as dt
    past = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)
    s2, _ = ident.issue_token(conn, uid, expires_at=past)
    with pytest.raises(AuthError, match="expired"):
        ident.resolve(conn, cfg(conn), s2)


def test_bring_your_own_token_open_mode(conn):
    uid = ident.create_user(conn)
    byo = new_token()
    ident.register_token(conn, uid, byo, permission="write")
    p = ident.resolve(conn, cfg(conn, key_mode="open"), byo)
    assert p.user_id == uid and not p.provisional
    with pytest.raises(ValueError):
        ident.register_token(conn, uid, "too-weak")


# ─── space resolution: by name (own only) ────────────────────────────────────
def test_name_resolves_own_and_lazy_creates_on_write(conn):
    uid = ident.create_user(conn, can_create_namespace=True)
    secret, _ = ident.issue_token(conn, uid, permission="write")
    p = ident.resolve(conn, cfg(conn), secret)

    # read of a non-existent name never creates
    with pytest.raises(SpaceNotFound):
        ident.resolve_space(conn, p, space="proj", for_write=False)

    nsid, perm = ident.resolve_space(conn, p, space="proj", for_write=True)
    assert perm == "write"                       # min(owner=admin, ceiling=write)
    again, _ = ident.resolve_space(conn, p, space="proj")
    assert again == nsid                          # same space, not a duplicate


def test_default_namespace_resolution_and_set(conn):
    uid = ident.create_user(conn, can_create_namespace=True)
    secret, _ = ident.issue_token(conn, uid, permission="write")
    p = ident.resolve(conn, cfg(conn), secret)
    # no space arg, no default yet → read fails, write lazily creates default
    with pytest.raises(SpaceNotFound):
        ident.resolve_space(conn, p)
    dflt, _ = ident.resolve_space(conn, p, for_write=True)
    # now it's the user's default
    again, _ = ident.resolve_space(conn, p)
    assert again == dflt
    # switch default to another space
    other = ident.create_namespace(conn, uid, "other")
    ident.set_default_space(conn, uid, other)
    p2 = ident.resolve(conn, cfg(conn), secret)
    assert ident.resolve_space(conn, p2)[0] == other


# ─── the collision the model is built to avoid ───────────────────────────────
def test_name_never_collides_with_shared_space(conn):
    alice = ident.create_user(conn, name="alice")
    bob = ident.create_user(conn, name="bob")
    a_notes = ident.create_namespace(conn, alice, "notes")
    b_notes = ident.create_namespace(conn, bob, "notes")   # same name, other owner
    ident.add_member(conn, b_notes, alice, "write")         # alice shares bob's notes

    secret, _ = ident.issue_token(conn, alice, permission="admin")
    p = ident.resolve(conn, cfg(conn), secret)

    # name resolves to alice's OWN notes, never bob's
    by_name, _ = ident.resolve_space(conn, p, space="notes")
    assert by_name == a_notes
    # bob's notes is reachable only by id, with the shared (write) permission
    by_id, perm = ident.resolve_space(conn, p, space_id=b_notes)
    assert by_id == b_notes and perm == "write"


def test_space_id_unreachable_rejected(conn):
    alice = ident.create_user(conn)
    bob = ident.create_user(conn)
    b_ns = ident.create_namespace(conn, bob, "private")
    secret, _ = ident.issue_token(conn, alice)
    p = ident.resolve(conn, cfg(conn), secret)
    with pytest.raises(SpaceNotFound):
        ident.resolve_space(conn, p, space_id=b_ns)


# ─── permission ceiling & token scope ────────────────────────────────────────
def test_read_ceiling_caps_owner_admin(conn):
    uid = ident.create_user(conn)
    ns = ident.create_namespace(conn, uid, "n")
    secret, _ = ident.issue_token(conn, uid, permission="read")
    p = ident.resolve(conn, cfg(conn), secret)
    _, perm = ident.resolve_space(conn, p, space_id=ns)
    assert perm == "read"                         # owner is admin, capped to read


def test_scoped_token_confined(conn):
    uid = ident.create_user(conn)
    a = ident.create_namespace(conn, uid, "a")
    b = ident.create_namespace(conn, uid, "b")
    secret, _ = ident.issue_token(conn, uid, namespace_id=a, permission="write")
    p = ident.resolve(conn, cfg(conn), secret)
    assert ident.resolve_space(conn, p, space_id=a)[0] == a
    with pytest.raises(AuthError):
        ident.resolve_space(conn, p, space_id=b)
    with pytest.raises(AuthError):                 # can't create a new one either
        ident.resolve_space(conn, p, space="c", for_write=True)


# ─── provisional user materializes on first write ────────────────────────────
def test_provisional_user_created_on_write(conn):
    secret = new_token()
    p = ident.resolve(conn, cfg(conn, key_mode="open"), secret)
    assert p.user_id is None
    with pytest.raises(SpaceNotFound):             # read creates nothing
        ident.resolve_space(conn, p)
    nsid, _ = ident.resolve_space(conn, p, for_write=True)
    assert p.user_id is not None and nsid
    # re-resolving the same secret now finds a real user
    p2 = ident.resolve(conn, cfg(conn, key_mode="open"), secret)
    assert p2.user_id == p.user_id and not p2.provisional


# ─── request-access flow ─────────────────────────────────────────────────────
def test_request_access_approve_grants_membership(conn):
    owner = ident.create_user(conn, name="owner")
    joiner = ident.create_user(conn, name="joiner")
    shared = ident.create_namespace(conn, owner, "shared")

    rid = ident.request_access(conn, joiner, shared, "read")
    pending = ident.list_requests(conn, shared)
    assert len(pending) == 1 and pending[0]["id"] == rid

    # before approval, joiner can't reach it
    js, _ = ident.issue_token(conn, joiner)
    jp = ident.resolve(conn, cfg(conn), js)
    with pytest.raises(SpaceNotFound):
        ident.resolve_space(conn, jp, space_id=shared)

    ident.approve_request(conn, rid)
    assert ident.list_requests(conn, shared) == []
    nsid, perm = ident.resolve_space(conn, jp, space_id=shared)
    assert nsid == shared and perm == "read"


# ─── Store end-to-end under identity (open/managed) ──────────────────────────
def _store(conn, *, key_mode="open", admin_token=""):
    from memgres.store import Store
    c = psycopg.connect(DSN)                      # own non-autocommit conn
    s = Store(cfg(conn, key_mode=key_mode, admin_token=admin_token), conn=c)
    s._own_conn = True                            # so s.close() closes c (no leaked tx)
    return s


def test_store_open_mode_isolates_tenants(conn):
    s = _store(conn, key_mode="open")
    alice, bob = new_token(), new_token()
    # each token lazily gets its own user + default namespace on first write
    ma = s.write(alice, body="alice private\n", tags=["x"])
    mb = s.write(bob, body="bob private\n", tags=["x"])
    # each reads only their own
    assert s.get(alice, ma.id).body == "alice private\n"
    assert s.get(bob, mb.id).body == "bob private\n"
    with pytest.raises(NotFoundErr):
        s.get(alice, mb.id)                       # cross-tenant read blocked
    hits = s.recall(alice, "private")
    assert [h.id for h in hits] == [ma.id]        # recall scoped to alice
    s.close()


def test_store_read_only_token_cannot_write(conn):
    uid = ident.create_user(conn)
    ns = ident.create_namespace(conn, uid, "n")
    ro, _ = ident.issue_token(conn, uid, permission="read")
    rw, _ = ident.issue_token(conn, uid, permission="write")
    s = _store(conn, key_mode="managed")
    m = s.write(rw, body="seed\n", space="n")
    assert s.get(ro, m.id, space="n").body == "seed\n"       # read ok
    with pytest.raises(AuthError):
        s.write(ro, body="nope\n", space="n")               # write denied
    s.close()


def test_store_managed_rejects_unknown_token(conn):
    s = _store(conn, key_mode="managed")
    with pytest.raises(AuthError):
        s.write(new_token(), body="x\n")
    s.close()


def test_store_named_spaces_same_token(conn):
    uid = ident.create_user(conn, can_create_namespace=True)
    tok, _ = ident.issue_token(conn, uid, permission="write")   # unscoped
    s = _store(conn, key_mode="managed")
    a = s.write(tok, body="in a\n", space="a")
    b = s.write(tok, body="in b\n", space="b")
    # one token, two named namespaces, isolated recall
    assert [h.id for h in s.recall(tok, "in", space="a")] == [a.id]
    assert [h.id for h in s.recall(tok, "in", space="b")] == [b.id]
    s.close()


def test_list_spaces_flags(conn):
    owner = ident.create_user(conn)
    other = ident.create_user(conn)
    mine = ident.create_namespace(conn, owner, "mine")
    ident.set_default_space(conn, owner, mine)
    theirs = ident.create_namespace(conn, other, "theirs")
    ident.add_member(conn, theirs, owner, "read")

    spaces = {s["name"]: s for s in ident.list_spaces(conn, owner)}
    assert spaces["mine"]["mine"] and spaces["mine"]["is_default"]
    assert spaces["mine"]["permission"] == "admin"
    assert not spaces["theirs"]["mine"] and spaces["theirs"]["permission"] == "read"


# ─── directory reads: users / namespaces / members ───────────────────────────
def test_list_users_filters_and_pages(conn):
    a = ident.create_user(conn, name="alice")
    ident.create_user(conn, name="bob", role="user_manager")
    root = ident.create_user(conn, name="root", role="superadmin")

    everyone = ident.list_users(conn)
    assert [u["name"] for u in everyone] == ["alice", "bob", "root"]  # created_at
    assert {u["id"] for u in everyone} == {a, everyone[1]["id"], root}
    assert all(u["default_namespace_id"] is None for u in everyone)

    assert [u["name"] for u in ident.list_users(conn, role="superadmin")] == ["root"]
    assert [u["name"] for u in ident.list_users(conn, limit=2)] == ["alice", "bob"]
    assert [u["name"] for u in ident.list_users(conn, limit=2, offset=2)] == ["root"]

    with pytest.raises(ValueError):
        ident.list_users(conn, role="wizard")


def test_list_namespaces_is_deployment_wide(conn):
    """`list_spaces` is caller-relative; this answers what exists at all."""
    a = ident.create_user(conn, name="a")
    b = ident.create_user(conn, name="b")
    ns_a = ident.create_namespace(conn, a, "sales", instruction="deals only")
    ns_b = ident.create_namespace(conn, b, "hr")

    everything = ident.list_namespaces(conn)
    assert {n["id"] for n in everything} == {ns_a, ns_b}
    assert [n for n in everything if n["id"] == ns_a][0]["instruction"] == "deals only"
    # a's own view sees only a's namespace, which is exactly the difference
    assert [n["id"] for n in ident.list_namespaces(conn, owner_user_id=a)] == [ns_a]
    assert [s["id"] for s in ident.list_spaces(conn, a)] == [ns_a]


def test_list_members_includes_the_owner(conn):
    """The owner is not a row in namespace_member, but omitting it would answer
    "who can see this?" wrongly — the question a public/private split raises."""
    owner = ident.create_user(conn, name="owner")
    guest = ident.create_user(conn, name="guest")
    ns = ident.create_namespace(conn, owner, "kb")
    ident.add_member(conn, ns, guest, "read")

    members = ident.list_members(conn, ns)
    assert [(m["user_id"], m["permission"], m["owner"]) for m in members] == [
        (owner, "admin", True), (guest, "read", False)]

    with pytest.raises(ident.SpaceNotFound):
        ident.list_members(conn, "00000000-0000-0000-0000-000000000000")


def test_token_owner_resolves_and_misses_cleanly(conn):
    """Authorizing an action addressed by token needs to know whose it is."""
    uid = ident.create_user(conn, name="u")
    _, tid = ident.issue_token(conn, uid)
    assert ident.token_owner(conn, tid) == uid
    assert ident.token_owner(conn, "00000000-0000-0000-0000-000000000000") is None


# ─── the right to create a namespace, and choosing between several ───────────
def test_without_the_right_a_write_is_refused_not_silently_created(conn):
    """The old behaviour: any unscoped write token could conjure a namespace by
    naming one. On a shared deployment that turns a typo into a second store
    nobody is looking at."""
    uid = ident.create_user(conn)                       # no right by default
    secret, _ = ident.issue_token(conn, uid, permission="write")
    p = ident.resolve(conn, cfg(conn), secret)

    with pytest.raises(ident.AuthError, match="may not create"):
        ident.resolve_space(conn, p, space="proj", for_write=True)
    with pytest.raises(ident.AuthError, match="may not create"):
        ident.resolve_space(conn, p, for_write=True)    # nor a lazy default
    assert ident.list_namespaces(conn) == []            # nothing was created

    # granting it makes the same call work
    ident.set_can_create_namespace(conn, uid, True)
    nsid, _ = ident.resolve_space(conn, p, space="proj", for_write=True)
    assert [n["id"] for n in ident.list_namespaces(conn)] == [nsid]


def test_a_single_reachable_namespace_needs_no_naming(conn):
    """Strictness scales with what there is to get wrong: with one namespace
    there is no choice to make, so an unqualified write still lands."""
    owner = ident.create_user(conn)
    ns = ident.create_namespace(conn, owner, "only")
    secret, _ = ident.issue_token(conn, owner, permission="write")
    p = ident.resolve(conn, cfg(conn), secret)

    assert ident.resolve_space(conn, p)[0] == ns        # read, no default set
    assert ident.resolve_space(conn, p, for_write=True)[0] == ns


def test_several_reachable_namespaces_force_a_choice(conn):
    """With more than one, guessing is a misfile — and once public and private
    sit side by side the wrong guess is the expensive one."""
    owner = ident.create_user(conn)
    ident.create_namespace(conn, owner, "public")
    ident.create_namespace(conn, owner, "private")
    secret, _ = ident.issue_token(conn, owner, permission="write")
    p = ident.resolve(conn, cfg(conn), secret)

    with pytest.raises(ident.SpaceAmbiguous) as e:
        ident.resolve_space(conn, p, for_write=True)
    assert "private" in str(e.value) and "public" in str(e.value)  # names the options

    # naming one resolves it; so does having a default
    assert ident.resolve_space(conn, p, space="public")[0] is not None
    ident.set_default_space(conn, owner, ident.list_spaces(conn, owner)[0]["id"])
    assert ident.resolve_space(conn, ident.resolve(conn, cfg(conn), secret))[0]


def test_a_shared_namespace_counts_as_reachable(conn):
    """Reachability, not ownership: a member with exactly one shared namespace
    should not have to name it either."""
    owner = ident.create_user(conn)
    guest = ident.create_user(conn)
    ns = ident.create_namespace(conn, owner, "team")
    ident.add_member(conn, ns, guest, "write")
    secret, _ = ident.issue_token(conn, guest, permission="write")
    p = ident.resolve(conn, cfg(conn), secret)

    nsid, perm = ident.resolve_space(conn, p, for_write=True)
    assert nsid == ns and perm == "write"               # membership, not admin
