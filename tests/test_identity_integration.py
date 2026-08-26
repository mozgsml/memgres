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
    AuthError, SpaceAmbiguous, SpaceNotFound, Principal, new_token,
    valid_format,
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
    # Captions are not what most of this suite is about; the requirement
    # has its own file (test_require_title.py) covering both settings.
    monkeypatch.setenv("MEMGRES_REQUIRE_TITLE", "false")
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


# ─── space resolution: by name ───────────────────────────────────────────────
def test_a_name_that_matches_nothing_is_an_error_not_a_new_space(conn):
    """Addressing an unknown name used to CREATE it, so a typo produced a new
    empty space and the write landed there looking like it had worked. Creating
    is now something you ask for."""
    uid = ident.create_user(conn, can_create_namespace=True)
    secret, _ = ident.issue_token(conn, uid, permission="write")
    p = ident.resolve(conn, cfg(conn), secret)

    for for_write in (False, True):
        with pytest.raises(SpaceNotFound):
            ident.resolve_space(conn, p, space="proj", for_write=for_write)

    nsid = ident.create_own_namespace(conn, p, "proj")
    again, perm = ident.resolve_space(conn, p, space="proj")
    assert again == nsid
    assert perm == "write"                       # min(owner=admin, ceiling=write)


def test_creating_a_namespace_needs_the_right(conn):
    uid = ident.create_user(conn)                # no can_create_namespace
    secret, _ = ident.issue_token(conn, uid, permission="write")
    p = ident.resolve(conn, cfg(conn), secret)
    with pytest.raises(AuthError):
        ident.create_own_namespace(conn, p, "proj")


def test_no_reachable_namespace_is_an_error_on_both_reads_and_writes(conn):
    """There is no default namespace to fall back on any more — where data goes
    is never inferred."""
    uid = ident.create_user(conn, can_create_namespace=True)
    secret, _ = ident.issue_token(conn, uid, permission="write")
    p = ident.resolve(conn, cfg(conn), secret)
    for for_write in (False, True):
        with pytest.raises(SpaceNotFound):
            ident.resolve_space(conn, p, for_write=for_write)

    # with exactly one, there is nothing to choose and it resolves silently
    only = ident.create_own_namespace(conn, p, "only")
    assert ident.resolve_space(conn, p)[0] == only


# ─── the collision the alias exists for ──────────────────────────────────────
def test_a_shared_namespace_is_reachable_by_its_name(conn):
    alice = ident.create_user(conn, name="alice")
    bob = ident.create_user(conn, name="bob")
    b_notes = ident.create_namespace(conn, bob, "notes")
    ident.add_member(conn, b_notes, alice, "write")

    secret, _ = ident.issue_token(conn, alice, permission="admin")
    p = ident.resolve(conn, cfg(conn), secret)
    # alice owns nothing called notes, so the shared one answers to the name
    by_name, perm = ident.resolve_space(conn, p, space="notes")
    assert by_name == b_notes and perm == "write"


def test_two_namespaces_of_one_name_are_refused_until_aliased(conn):
    """Nobody chose this collision — alice named hers, bob named his, and then
    bob shared. So it is refused with both named, and alice settles it."""
    alice = ident.create_user(conn, name="alice")
    bob = ident.create_user(conn, name="bob")
    a_notes = ident.create_namespace(conn, alice, "notes")
    b_notes = ident.create_namespace(conn, bob, "notes")
    ident.add_member(conn, b_notes, alice, "write")

    secret, _ = ident.issue_token(conn, alice, permission="admin")
    p = ident.resolve(conn, cfg(conn), secret)
    with pytest.raises(SpaceAmbiguous) as e:
        ident.resolve_space(conn, p, space="notes")
    assert a_notes in str(e.value) and b_notes in str(e.value)

    # each is still addressable by id, and an alias makes a name for it
    assert ident.resolve_space(conn, p, space_id=b_notes)[0] == b_notes
    ident.create_alias(conn, alice, "bobs-notes", b_notes)
    assert ident.resolve_space(conn, p, space="bobs-notes")[0] == b_notes


def test_an_alias_outlives_a_name_someone_else_brings(conn):
    """Your alias is your decision; a name arriving later is a stranger's. The
    alias keeps working — otherwise sharing would silently break your calls."""
    alice = ident.create_user(conn, name="alice")
    bob = ident.create_user(conn, name="bob")
    carol = ident.create_user(conn, name="carol")
    b_ns = ident.create_namespace(conn, bob, "b-stuff")
    ident.add_member(conn, b_ns, alice, "read")
    ident.create_alias(conn, alice, "shared", b_ns)

    secret, _ = ident.issue_token(conn, alice, permission="admin")
    p = ident.resolve(conn, cfg(conn), secret)
    assert ident.resolve_space(conn, p, space="shared")[0] == b_ns

    # carol now shares a namespace actually NAMED 'shared'
    c_ns = ident.create_namespace(conn, carol, "shared")
    ident.add_member(conn, c_ns, alice, "read")
    assert ident.resolve_space(conn, p, space="shared")[0] == b_ns    # unchanged
    assert ident.resolve_space(conn, p, space_id=c_ns)[0] == c_ns


def test_an_alias_may_not_shadow_a_name_that_already_works(conn):
    alice = ident.create_user(conn, name="alice")
    bob = ident.create_user(conn, name="bob")
    mine = ident.create_namespace(conn, alice, "notes")
    theirs = ident.create_namespace(conn, bob, "other")
    ident.add_member(conn, theirs, alice, "read")

    with pytest.raises(SpaceAmbiguous):
        ident.create_alias(conn, alice, "notes", theirs)     # would shadow `mine`
    # and an alias grants nothing: the target must already be reachable
    unreachable = ident.create_namespace(conn, bob, "private")
    with pytest.raises(SpaceNotFound):
        ident.create_alias(conn, alice, "peek", unreachable)
    assert ident.resolve_space(
        conn, ident.resolve(conn, cfg(conn),
                            ident.issue_token(conn, alice)[0]),
        space="notes")[0] == mine


def test_no_door_may_create_a_namespace_your_alias_shadows(conn):
    """The rule was written on the self-service door and missing from the two
    admin-side ones, so an admin provisioning you a namespace could leave every
    call naming it resolving to a DIFFERENT, shared space — silently. It now
    lives at the single point all three funnel through."""
    alice = ident.create_user(conn, name="alice", can_create_namespace=True)
    bob = ident.create_user(conn, name="bob")
    b_ns = ident.create_namespace(conn, bob, "b-stuff")
    ident.add_member(conn, b_ns, alice, "write")
    ident.create_alias(conn, alice, "private", b_ns)

    # the admin door — this is the one that was missing the check
    with pytest.raises(SpaceAmbiguous):
        ident.create_namespace(conn, alice, "private")
    # and no namespace was made, so nothing to clean up
    assert [n["name"] for n in ident.list_namespaces(conn, owner_user_id=alice)] == []

    secret, _ = ident.issue_token(conn, alice, permission="write")
    p = ident.resolve(conn, cfg(conn), secret)
    assert ident.resolve_space(conn, p, space="private")[0] == b_ns


def test_a_read_only_token_may_not_create_a_namespace(conn):
    """A token weakened to read-only is what the docs tell you to give an agent;
    it must not be able to change deployment state."""
    uid = ident.create_user(conn, name="u", can_create_namespace=True)
    ro, _ = ident.issue_token(conn, uid, permission="read")
    p = ident.resolve(conn, cfg(conn), ro)
    with pytest.raises(AuthError, match="write-capable"):
        ident.create_own_namespace(conn, p, "nope")
    assert ident.list_namespaces(conn) == []


def test_one_account_cannot_own_unbounded_namespaces(conn):
    """A bound, not a business rule: self-service must not be turnable into an
    INSERT loop by anyone holding a well-formed token."""
    uid = ident.create_user(conn, name="u", can_create_namespace=True)
    for i in range(ident.MAX_NAMESPACES_PER_USER):
        ident.create_namespace(conn, uid, f"ns{i}")
    with pytest.raises(SpaceAmbiguous, match="cap"):
        ident.create_namespace(conn, uid, "one-too-many")


def test_an_empty_profile_edit_still_has_to_find_the_user(conn):
    with pytest.raises(SpaceNotFound):
        ident.edit_user(conn, "00000000-0000-0000-0000-000000000000")


def test_a_namespace_may_not_be_born_shadowed_by_your_alias(conn):
    alice = ident.create_user(conn, name="alice", can_create_namespace=True)
    bob = ident.create_user(conn, name="bob")
    b_ns = ident.create_namespace(conn, bob, "b-stuff")
    ident.add_member(conn, b_ns, alice, "read")
    ident.create_alias(conn, alice, "notes", b_ns)

    secret, _ = ident.issue_token(conn, alice, permission="admin")
    p = ident.resolve(conn, cfg(conn), secret)
    with pytest.raises(SpaceAmbiguous):
        ident.create_own_namespace(conn, p, "notes")


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
    with pytest.raises(SpaceNotFound):             # an unknown name is not a space
        ident.resolve_space(conn, p, space="c", for_write=True)
    with pytest.raises(AuthError):                 # and it can't create one either
        ident.create_own_namespace(conn, p, "c")


# ─── provisional user materializes on first write ────────────────────────────
def test_provisional_user_created_on_write(conn):
    secret = new_token()
    p = ident.resolve(conn, cfg(conn, key_mode="open"), secret)
    assert p.user_id is None
    with pytest.raises(SpaceNotFound):             # read creates nothing
        ident.resolve_space(conn, p)
    # open mode is self-service: the first namespace it asks for materializes it
    nsid = ident.create_own_namespace(conn, p, "mine")
    assert p.user_id is not None and nsid
    assert ident.resolve_space(conn, p)[0] == nsid
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
    # open mode is self-service: each token makes its own space, then writes
    for tok in (alice, bob):
        ident.create_own_namespace(
            conn, ident.resolve(conn, cfg(conn, key_mode="open"), tok), "mine")
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
    ident.create_namespace(conn, uid, "a")
    ident.create_namespace(conn, uid, "b")
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
    theirs = ident.create_namespace(conn, other, "theirs")
    ident.add_member(conn, theirs, owner, "read")

    spaces = {s["name"]: s for s in ident.list_spaces(conn, owner)}
    assert spaces["mine"]["mine"] and spaces["mine"]["permission"] == "admin"
    assert not spaces["theirs"]["mine"] and spaces["theirs"]["permission"] == "read"
    # no alias set yet — you address these by their names
    assert spaces["mine"]["alias"] is None and spaces["theirs"]["alias"] is None

    ident.create_alias(conn, owner, "shared", theirs)
    spaces = {s["name"]: s for s in ident.list_spaces(conn, owner)}
    assert spaces["theirs"]["alias"] == "shared"   # what to type for it


# ─── directory reads: users / namespaces / members ───────────────────────────
def test_list_users_filters_and_pages(conn):
    a = ident.create_user(conn, name="alice")
    ident.create_user(conn, name="bob", role="user_manager")
    root = ident.create_user(conn, name="root", role="superadmin")

    everyone = ident.list_users(conn)
    assert [u["name"] for u in everyone] == ["alice", "bob", "root"]  # created_at
    assert {u["id"] for u in everyone} == {a, everyone[1]["id"], root}
    assert all(u["can_create_namespace"] is False for u in everyone)

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

    # naming an unknown space is an error whoever you are — nothing is created
    with pytest.raises(SpaceNotFound):
        ident.resolve_space(conn, p, space="proj", for_write=True)
    # and asking outright is refused without the right
    with pytest.raises(ident.AuthError, match="may not create"):
        ident.create_own_namespace(conn, p, "proj")
    assert ident.list_namespaces(conn) == []            # nothing was created

    # granting it makes the same call work
    ident.set_can_create_namespace(conn, uid, True)
    nsid = ident.create_own_namespace(conn, p, "proj")
    assert [n["id"] for n in ident.list_namespaces(conn)] == [nsid]
    assert ident.resolve_space(conn, p, space="proj")[0] == nsid


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

    # naming one resolves it — and there is no default to fall back on instead
    assert ident.resolve_space(conn, p, space="public")[0] is not None


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
