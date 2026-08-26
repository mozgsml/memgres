"""Regression tests for what an adversarial review found.

Each test here corresponds to a reported hole, and each one FAILED before its
fix. They are grouped by the property the attacker was after rather than by the
module, because that is how the next reviewer will read them.

The two that matter most are not exotic: an approval that grants more than the
approver agreed to, and a pair of anti-lockout counters that disagreed about
what "an admin" means — the second one turns two ordinary control-plane calls
into a deployment nobody can enter.
"""

import os
import stat
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

psycopg = pytest.importorskip("psycopg")

from memgres import admin, identity  # noqa: E402
from memgres.config import load  # noqa: E402
from memgres.schema import migrate  # noqa: E402

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
def box(monkeypatch):
    with psycopg.connect(DSN, autocommit=True) as c, c.cursor() as cur:
        cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    for k in list(os.environ):
        if k.startswith("MEMGRES_"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("MEMGRES_DATABASE_URL", DSN)
    monkeypatch.setenv("MEMGRES_KEY_MODE", "managed")
    monkeypatch.setenv("MEMGRES_EMBED_PROVIDER", "none")
    monkeypatch.setenv("MEMGRES_FTS_LANGUAGE", "simple")
    cfg = load()
    conn = psycopg.connect(DSN)
    migrate(conn, cfg)
    conn.commit()
    yield conn, cfg
    conn.close()


def _root(conn):
    uid = identity.create_user(conn, "root", role="superadmin")
    return uid, identity.Principal(user_id=uid, permission="admin",
                                   scope_namespace_id=None, role="superadmin",
                                   is_admin=True)


def _person(conn, name, *, ceiling="admin", role="user"):
    uid = identity.create_user(conn, name, role=role)
    return uid, identity.Principal(user_id=uid, permission=ceiling,
                                   scope_namespace_id=None, role=role)


# ─── granting more than the approver agreed to ───────────────────────────────

def test_a_pending_request_cannot_be_raised_under_the_approver(box):
    """File `read`, let the owner see `read`, raise it to `admin` before they
    click. The grant read the row at approval time, so the admin authorized one
    thing and granted another."""
    conn, _ = box
    owner_id, owner = _person(conn, "owner")
    mallory_id, mallory = _person(conn, "mallory")
    ns = identity.create_namespace(conn, owner_id, "notes")

    admin.request_access(conn, mallory, namespace_id=ns, permission="read")
    seen = admin.list_requests(conn, owner, namespace_id=ns)
    assert seen[0]["requested_permission"] == "read"

    admin.request_access(conn, mallory, namespace_id=ns, permission="admin")
    still = admin.list_requests(conn, owner, namespace_id=ns)
    assert still[0]["requested_permission"] == "read"     # not amendable

    admin.decide_access(conn, owner, request_id=seen[0]["id"], approve=True)
    assert identity.reaches(conn, mallory_id, ns) == "read"


def test_approving_refuses_when_the_request_is_not_what_was_seen(box):
    """The second lock: even if a future writer makes pending requests amendable
    again, an approver who says what they saw cannot be made to grant more."""
    conn, _ = box
    owner_id, owner = _person(conn, "owner")
    mallory_id, mallory = _person(conn, "mallory")
    ns = identity.create_namespace(conn, owner_id, "notes")
    rid = admin.request_access(conn, mallory, namespace_id=ns,
                               permission="read") and \
        admin.list_requests(conn, owner, namespace_id=ns)[0]["id"]

    with conn.cursor() as cur:                 # simulate the row changing
        cur.execute("UPDATE access_request SET requested_permission='admin' "
                    "WHERE id=%s", (rid,))
    with pytest.raises(identity.AuthError, match="look at it again"):
        admin.decide_access(conn, owner, request_id=rid, approve=True,
                            expect_permission="read")
    assert identity.reaches(conn, mallory_id, ns) is None


def test_a_read_only_credential_cannot_ask_for_admin(box):
    """The input half of the same over-grant: the ask is clamped by the ceiling
    of the credential making it, so a read-only agent token cannot file an
    `admin` request its account would keep long after the token is gone."""
    conn, _ = box
    owner_id, owner = _person(conn, "owner")
    weak_id, weak = _person(conn, "weak", ceiling="read")
    ns = identity.create_namespace(conn, owner_id, "notes")
    admin.request_access(conn, weak, namespace_id=ns, permission="admin")
    assert admin.list_requests(conn, owner,
                               namespace_id=ns)[0]["requested_permission"] == "read"


def test_deciding_a_request_you_may_not_see_says_no_such_request(box):
    """Same answer as a request that does not exist — which uuids are real is
    not something a refusal should confirm."""
    conn, _ = box
    owner_id, owner = _person(conn, "owner")
    other_id, other = _person(conn, "other")
    third_id, third = _person(conn, "third")
    ns = identity.create_namespace(conn, owner_id, "notes")
    admin.request_access(conn, third, namespace_id=ns)
    rid = admin.list_requests(conn, owner, namespace_id=ns)[0]["id"]

    with pytest.raises(identity.SpaceNotFound) as seen:
        admin.decide_access(conn, other, request_id=rid, approve=True)
    with pytest.raises(identity.SpaceNotFound) as absent:
        admin.decide_access(conn, other, approve=True,
                            request_id="00000000-0000-0000-0000-000000000000")
    assert str(seen.value) == str(absent.value) == "'no such request'"


# ─── locking yourself out of your own deployment ─────────────────────────────

def test_the_two_lockout_guards_agree_about_what_an_admin_is(box):
    """Disable the second superadmin (allowed — two are active), then DEMOTE the
    first: `revoke_superadmin` counted disabled admins as present and agreed.
    End state was zero active superadmins and recovery by hand-editing the DB."""
    conn, _ = box
    s1, root = _root(conn)
    s2 = identity.create_user(conn, "root2", role="superadmin")

    admin.set_disabled(conn, root, user_id=s2, disabled=True)
    with pytest.raises(identity.AuthError, match="last active superadmin"):
        admin.set_disabled(conn, root, user_id=s1, disabled=True)
    with pytest.raises(identity.AuthError, match="last active superadmin"):
        identity.revoke_superadmin(conn, s1)
    assert identity.count_superadmins(conn) == 1        # actives only


def test_bootstrap_reseeds_when_every_admin_has_been_switched_off(box):
    """`count_service_admins` decides whether a fresh admin is seeded at start.
    Counting disabled ones as present meant the deployment stayed adminless."""
    conn, _ = box
    s1, root = _root(conn)
    identity.create_user(conn, "mgr", role="user_manager")
    assert identity.count_service_admins(conn) == 2
    with conn.cursor() as cur:
        cur.execute("UPDATE app_user SET disabled_at=now() WHERE role<>'user'")
    assert identity.count_service_admins(conn) == 0


def test_disabling_the_bootstrap_account_leaves_the_break_glass_working(box):
    """The env secret is stored AS A TOKEN of the seeded admin, so the row
    matches first — and disabling that account took the operator's way back in
    with it. Two ordinary calls could leave a deployment nobody can enter."""
    import dataclasses
    conn, cfg = box
    env_secret = identity.new_token()
    cfg = dataclasses.replace(cfg, admin_token=env_secret)
    uid = identity.create_user(conn, "bootstrap", role="superadmin")
    identity.register_token(conn, uid, env_secret, permission="admin")
    identity.create_user(conn, "root2", role="superadmin")   # so it may be off
    with conn.cursor() as cur:
        cur.execute("UPDATE app_user SET disabled_at=now() WHERE id=%s", (uid,))
    conn.commit()

    p = identity.resolve(conn, cfg, env_secret)
    assert p.is_admin and p.permission == "admin" and p.user_id is None

    # and an ordinary token of that account is still refused
    other, _ = identity.issue_token(conn, uid, permission="write")
    conn.commit()
    with pytest.raises(identity.AuthError, match="disabled"):
        identity.resolve(conn, cfg, other)


# ─── unsolicited namespaces ──────────────────────────────────────────────────

def test_a_transfer_cannot_push_someone_past_the_namespace_cap(box):
    """Mint your 50, push them onto a victim, repeat: they end up owning
    namespaces they never asked for and cannot delete, since there is no
    delete_namespace."""
    conn, _ = box
    owner_id, owner = _person(conn, "owner")
    victim_id, _ = _person(conn, "victim")
    for i in range(identity.MAX_NAMESPACES_PER_USER):
        identity.create_namespace(conn, victim_id, f"theirs{i}")
    ns = identity.create_namespace(conn, owner_id, "mine")
    with pytest.raises(identity.SpaceAmbiguous, match="cap"):
        admin.transfer_namespace(conn, owner, namespace_id=ns,
                                 new_owner_user_id=victim_id)
    assert identity.namespace_owner(conn, ns) == owner_id


def test_a_namespace_scoped_token_cannot_give_the_namespace_away(box):
    """A pin says "work in here", not "dispose of this"."""
    conn, _ = box
    owner_id, _ = _person(conn, "owner")
    heir_id, _ = _person(conn, "heir")
    ns = identity.create_namespace(conn, owner_id, "notes")
    pinned = identity.Principal(user_id=owner_id, permission="admin",
                                scope_namespace_id=ns, role="user")
    with pytest.raises(admin.Forbidden, match="unscoped"):
        admin.transfer_namespace(conn, pinned, namespace_id=ns,
                                 new_owner_user_id=heir_id)


def test_only_the_owner_may_rename(box):
    """An admin member was given authority over the contents; renaming breaks
    every by-name call the owner and every other member make."""
    conn, _ = box
    owner_id, owner = _person(conn, "owner")
    deputy_id, deputy = _person(conn, "deputy")
    ns = identity.create_namespace(conn, owner_id, "notes")
    admin.add_member(conn, owner, namespace_id=ns, user_id=deputy_id,
                     permission="admin")
    admin.edit_namespace(conn, deputy, namespace_id=ns, description="fine")
    with pytest.raises(admin.Forbidden, match="only the owner"):
        admin.edit_namespace(conn, deputy, namespace_id=ns, name="theirs")


# ─── enrollment and offboarding ──────────────────────────────────────────────

def test_a_key_cannot_bind_to_a_disabled_account(box):
    """It used to succeed, spend the key and report "bound" — and the token was
    then refused at the first call. No access gained, but the answer was a lie
    and the key was gone."""
    conn, _ = box
    _, root = _root(conn)
    uid, _ = _person(conn, "leaver")
    key = admin.create_enrollment(conn, root, user_id=uid)["key"]
    admin.set_disabled(conn, root, user_id=uid, disabled=True)
    with pytest.raises(identity.AuthError, match="disabled"):
        identity.redeem_enrollment(conn, key, identity.new_token())
    assert admin.list_enrollments(conn, root,
                                  user_id=uid)[0]["state"] == "pending"


def test_a_user_manager_cannot_enumerate_the_keys_of_its_seniors(box):
    """`list_tokens` refuses exactly this; the enrollment listing applied its
    guard only to the shape WITH a target, so the shape without one walked past."""
    conn, _ = box
    root_id, root = _root(conn)
    mgr_id, mgr = _person(conn, "mgr", role="user_manager")
    plain_id, _ = _person(conn, "plain")
    admin.create_enrollment(conn, root, user_id=plain_id, label="ordinary")
    with conn.cursor() as cur:          # a key of the superadmin's own account
        cur.execute("INSERT INTO enrollment_key (key_hash, user_id, permission, "
                    "label, expires_at) VALUES (%s, %s, 'admin', 'boss laptop', "
                    "now() + interval '1 day')",
                    (identity.token_hash("mge_x"), root_id))

    seen = admin.list_enrollments(conn, mgr)
    assert [r["label"] for r in seen] == ["ordinary"]
    assert admin.list_enrollments(conn, root)                # root still sees all
    assert len(admin.list_enrollments(conn, root)) == 2


# ─── files that hold secrets ─────────────────────────────────────────────────

def test_a_secret_is_never_written_through_a_symlink(tmp_path):
    """Plant a symlink where a secret is about to land and it goes to the
    attacker's file instead. Proved end to end against the provisioning CLI."""
    target = tmp_path / "attacker.txt"
    target.write_text("")
    link = tmp_path / "ivan.token"
    link.symlink_to(target)

    identity.write_private(str(link), "mgk_secret\n")

    # The link is replaced rather than followed: the secret is at the path the
    # operator named, and the attacker's file never saw it. `O_NOFOLLOW` closes
    # the re-plant race between the unlink and the open — the write fails there
    # rather than going through.
    assert target.read_text() == ""
    assert not link.is_symlink() and link.read_text().strip() == "mgk_secret"
    assert stat.S_IMODE(os.stat(link).st_mode) == 0o600


def test_a_secret_is_never_briefly_world_readable(tmp_path):
    """`O_CREAT`'s mode is IGNORED for a file that already exists, so the secret
    sat in a 0644 file for the duration of the write and only became 0600
    after — measured, and the docstring claimed otherwise."""
    path = tmp_path / "t.token"
    path.write_text("stale")
    os.chmod(path, 0o644)
    identity.write_private(str(path), "mgk_fresh\n")
    assert path.read_text().strip() == "mgk_fresh"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_the_sink_directory_is_tightened_and_so_are_its_parents(tmp_path):
    """`os.makedirs(mode=…)` applies to the LEAF only, and not at all to a
    directory that already exists: a 0777 sink stayed 0777 and every
    intermediate was created 0775."""
    loose = tmp_path / "loose"
    loose.mkdir(mode=0o777)
    identity.stash_secret(str(loose), "tok", "mgk_s")
    assert stat.S_IMODE(os.stat(loose).st_mode) == 0o700

    nested = tmp_path / "a" / "b" / "c"
    identity.stash_secret(str(nested), "tok", "mgk_s")
    for d in (tmp_path / "a", tmp_path / "a" / "b", nested):
        assert stat.S_IMODE(os.stat(d).st_mode) == 0o700


def test_an_enrollment_key_obeys_the_sink_like_any_other_secret(tmp_path, box):
    """The 0.9.0 wave took the TOKEN out of the reply and left the key in it —
    but a key is a bearer credential, and whoever reads it first gets the
    account. An operator who set a sink meant this one too."""
    conn, _ = box
    _, root = _root(conn)
    uid, _ = _person(conn, "joiner")
    out = admin.deliver_key(admin.create_enrollment(conn, root, user_id=uid),
                            str(tmp_path / "sink"))
    assert "key" not in out and out["exposed"] is False
    written = Path(out["path"]).read_text().strip()
    assert written.startswith("mge_")
    # and it is the real key: it still redeems
    assert identity.redeem_enrollment(conn, written,
                                      identity.new_token())["user_id"] == uid


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
