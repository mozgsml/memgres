"""Administration that can also take back: un-share, hand over, switch off.

Until now this control plane could only add. Access could be granted and never
withdrawn except by revoking every token a person held — which cuts them off
from everything rather than from the one namespace — and a person leaving had no
operation at all.

What the tests are actually about is the SHAPE of each refusal, because every
one of them is a place where a plausible implementation silently does nothing:
removing an owner (whose access is not a membership row, so a DELETE reports
success and changes nothing), disabling the last superadmin (nobody left to undo
it), transferring onto a name the receiving account already uses (a unique
constraint, i.e. a driver fault instead of an explanation), and scoping a
credential to a namespace its owner cannot reach (valid, and sees nothing).
"""

import os
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
    """A managed deployment and a live connection, service layer only.

    No MCP here on purpose: these are authority rules, and asserting them
    through a tool would mix in the SDK's error wrapping — which differs between
    generations and has already turned this suite red for no reason.
    """
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
    """A superadmin principal with a full credential."""
    uid = identity.create_user(conn, "root", role="superadmin")
    return identity.Principal(user_id=uid, permission="admin",
                              scope_namespace_id=None, role="superadmin",
                              is_admin=True)


def _person(conn, name, *, ceiling="admin", role="user"):
    """A user plus a principal holding an unscoped token of that ceiling."""
    uid = identity.create_user(conn, name, role=role)
    return uid, identity.Principal(user_id=uid, permission=ceiling,
                                   scope_namespace_id=None, role=role)


# ─── sharing is now the owner's to do, and to undo ───────────────────────────

def test_an_owner_shares_and_unshares_without_a_superadmin(box):
    """The gap that sent every "let a colleague into my cabinet" to the
    operator: add_member demanded the deployment's root."""
    conn, _ = box
    owner_id, owner = _person(conn, "owner")
    mate_id, _ = _person(conn, "mate")
    ns = identity.create_namespace(conn, owner_id, "notes")

    admin.add_member(conn, owner, namespace_id=ns, user_id=mate_id,
                     permission="write")
    assert identity.reaches(conn, mate_id, ns) == "write"

    assert admin.remove_member(conn, owner, namespace_id=ns,
                               user_id=mate_id)["removed"] is True
    assert identity.reaches(conn, mate_id, ns) is None


def test_removing_someone_who_was_never_a_member_says_so(box):
    conn, _ = box
    owner_id, owner = _person(conn, "owner")
    stranger_id, _ = _person(conn, "stranger")
    ns = identity.create_namespace(conn, owner_id, "notes")
    assert admin.remove_member(conn, owner, namespace_id=ns,
                               user_id=stranger_id)["removed"] is False


def test_the_owner_cannot_be_removed_from_their_own_namespace(box):
    """A DELETE against `namespace_member` would report success and change
    nothing: the owner's access does not live there. Reporting success for a
    no-op is how someone believes they revoked access they did not."""
    conn, _ = box
    owner_id, owner = _person(conn, "owner")
    ns = identity.create_namespace(conn, owner_id, "notes")
    with pytest.raises(identity.AuthError, match="OWNS"):
        admin.remove_member(conn, owner, namespace_id=ns, user_id=owner_id)
    assert identity.reaches(conn, owner_id, ns) == "admin"


def test_a_stranger_cannot_share_or_unshare_a_namespace(box):
    """And is told "no such namespace", not "forbidden".

    That is the deliberate answer, not a sloppy one: a namespace someone cannot
    reach must be indistinguishable from one that does not exist, or the refusal
    itself confirms which uuids are real. The same reasoning runs through
    `request_access`, down to matching the response TIME.
    """
    conn, _ = box
    owner_id, _ = _person(conn, "owner")
    other_id, other = _person(conn, "other")
    ns = identity.create_namespace(conn, owner_id, "notes")
    for call in (lambda: admin.add_member(conn, other, namespace_id=ns,
                                          user_id=other_id),
                 lambda: admin.remove_member(conn, other, namespace_id=ns,
                                             user_id=other_id)):
        with pytest.raises(identity.SpaceNotFound):
            call()
    assert identity.reaches(conn, other_id, ns) is None


def test_sharing_still_needs_an_admin_ceiling(box):
    """Membership ∧ ceiling is the whole permission model: a write-ceiling token
    belonging to the owner must not be able to hand out access."""
    conn, _ = box
    owner_id, _ = _person(conn, "owner")
    weak = identity.Principal(user_id=owner_id, permission="write",
                              scope_namespace_id=None, role="user")
    ns = identity.create_namespace(conn, owner_id, "notes")
    with pytest.raises(admin.Forbidden, match="admin on this namespace"):
        admin.add_member(conn, weak, namespace_id=ns, user_id=owner_id)


def test_an_admin_member_may_share_but_not_give_away(box):
    """Delegated authority over the CONTENTS is not authority to dispose of the
    namespace itself — the one place the owner tier and the admin tier differ."""
    conn, _ = box
    owner_id, owner = _person(conn, "owner")
    deputy_id, deputy = _person(conn, "deputy")
    third_id, _ = _person(conn, "third")
    ns = identity.create_namespace(conn, owner_id, "notes")
    admin.add_member(conn, owner, namespace_id=ns, user_id=deputy_id,
                     permission="admin")

    admin.add_member(conn, deputy, namespace_id=ns, user_id=third_id)   # allowed
    with pytest.raises(admin.Forbidden, match="only the owner"):
        admin.transfer_namespace(conn, deputy, namespace_id=ns,
                                 new_owner_user_id=deputy_id)


# ─── handing a namespace over ────────────────────────────────────────────────

def test_transfer_moves_ownership_and_leaves_the_old_owner_behind(box):
    conn, _ = box
    owner_id, owner = _person(conn, "owner")
    heir_id, _ = _person(conn, "heir")
    ns = identity.create_namespace(conn, owner_id, "notes")

    out = admin.transfer_namespace(conn, owner, namespace_id=ns,
                                   new_owner_user_id=heir_id)
    assert out["owner_user_id"] == heir_id
    assert identity.namespace_owner(conn, ns) == heir_id
    assert identity.reaches(conn, heir_id, ns) == "admin"
    # the default keeps the outgoing owner in, which is the point of the default
    assert identity.reaches(conn, owner_id, ns) == "admin"


def test_a_clean_handoff_is_possible_but_must_be_asked_for(box):
    conn, _ = box
    owner_id, owner = _person(conn, "owner")
    heir_id, _ = _person(conn, "heir")
    ns = identity.create_namespace(conn, owner_id, "notes")
    admin.transfer_namespace(conn, owner, namespace_id=ns,
                             new_owner_user_id=heir_id,
                             keep_previous_owner=None)
    assert identity.reaches(conn, owner_id, ns) is None


def test_transfer_is_refused_when_the_name_is_taken_over_there(box):
    """(owner, name) is unique. Without the check this dies on a constraint —
    a driver fault the caller cannot act on — instead of a sentence.

    And the sentence must not quote the RECIPIENT's inventory: the check runs
    before the UPDATE, so a positive answer costs the prober nothing and can be
    repeated forever. Naming what it collided with turned a transfer into a free
    dictionary attack on another tenant's namespace names, which routinely
    encode customers and projects.
    """
    conn, _ = box
    owner_id, owner = _person(conn, "owner")
    heir_id, _ = _person(conn, "heir")
    ns = identity.create_namespace(conn, owner_id, "notes")
    identity.create_namespace(conn, heir_id, "notes")
    other = identity.create_namespace(conn, heir_id, "other")
    identity.create_alias(conn, heir_id, "secret-client", other)
    with pytest.raises(identity.SpaceAmbiguous) as e:
        admin.transfer_namespace(conn, owner, namespace_id=ns,
                                 new_owner_user_id=heir_id)
    assert "cannot hold a namespace by this name" in str(e.value)
    # neither what it hit, nor whether it was a namespace or an alias
    for leak in ("already owns", "alias", "other", "secret-client"):
        assert leak not in str(e.value)
    assert identity.namespace_owner(conn, ns) == owner_id       # nothing moved


def test_transfer_drops_the_new_owners_stale_membership(box):
    """Ownership outranks any membership; leaving a `read` row behind means a
    later transfer silently demotes them."""
    conn, _ = box
    owner_id, owner = _person(conn, "owner")
    heir_id, _ = _person(conn, "heir")
    ns = identity.create_namespace(conn, owner_id, "notes")
    admin.add_member(conn, owner, namespace_id=ns, user_id=heir_id,
                     permission="read")
    admin.transfer_namespace(conn, owner, namespace_id=ns,
                             new_owner_user_id=heir_id)
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM namespace_member WHERE namespace_id=%s "
                    "AND user_id=%s", (ns, heir_id))
        assert cur.fetchone()[0] == 0
    assert identity.reaches(conn, heir_id, ns) == "admin"       # by ownership


def test_transfer_to_a_nonexistent_user_is_refused(box):
    conn, _ = box
    owner_id, owner = _person(conn, "owner")
    ns = identity.create_namespace(conn, owner_id, "notes")
    with pytest.raises(identity.SpaceNotFound):
        admin.transfer_namespace(
            conn, owner, namespace_id=ns,
            new_owner_user_id="00000000-0000-0000-0000-000000000000")


# ─── switching an account off ────────────────────────────────────────────────

def test_disabling_stops_every_token_at_once(box):
    """Including one issued BEFORE the account was switched off, and one issued
    after — which is why the check lives in authentication and not at the doors."""
    conn, cfg = box
    root = _root(conn)
    uid, _ = _person(conn, "leaver")
    before, _ = identity.issue_token(conn, uid, permission="write")
    conn.commit()
    assert identity.resolve(conn, cfg, before).user_id == uid

    admin.set_disabled(conn, root, user_id=uid, disabled=True)
    conn.commit()
    with pytest.raises(identity.AuthError, match="disabled"):
        identity.resolve(conn, cfg, before)

    after, _ = identity.issue_token(conn, uid, permission="write")
    conn.commit()
    with pytest.raises(identity.AuthError, match="disabled"):
        identity.resolve(conn, cfg, after)


def test_re_enabling_restores_the_same_credentials(box):
    """Reversible is the whole difference from deletion: someone coming back
    must not have to be re-provisioned."""
    conn, cfg = box
    root = _root(conn)
    uid, _ = _person(conn, "returner")
    tok, _ = identity.issue_token(conn, uid, permission="write")
    admin.set_disabled(conn, root, user_id=uid, disabled=True)
    admin.set_disabled(conn, root, user_id=uid, disabled=False)
    conn.commit()
    assert identity.resolve(conn, cfg, tok).user_id == uid


def test_disabling_is_idempotent_and_says_nothing_changed(box):
    conn, _ = box
    root = _root(conn)
    uid, _ = _person(conn, "leaver")
    admin.set_disabled(conn, root, user_id=uid, disabled=True)
    assert admin.set_disabled(conn, root, user_id=uid,
                              disabled=True).get("unchanged") is True


def test_the_last_active_superadmin_cannot_be_switched_off(box):
    """There would be nobody left able to switch it back on; recovery would mean
    editing the database by hand."""
    conn, _ = box
    root = _root(conn)
    with pytest.raises(identity.AuthError, match="last active superadmin"):
        admin.set_disabled(conn, root, user_id=root.user_id, disabled=True)

    second = identity.create_user(conn, "root2", role="superadmin")
    admin.set_disabled(conn, root, user_id=root.user_id, disabled=True)  # now ok
    # …and the remaining one is protected in turn
    with pytest.raises(identity.AuthError, match="last active superadmin"):
        admin.set_disabled(conn, root, user_id=second, disabled=True)


def test_a_user_manager_cannot_switch_off_an_admin_account(box):
    """Same rule as every other targeted act: a provisioning tier that could
    disable a superadmin would BE one."""
    conn, _ = box
    root = _root(conn)
    _, mgr = _person(conn, "mgr", role="user_manager")
    with pytest.raises(admin.Forbidden):
        admin.set_disabled(conn, mgr, user_id=root.user_id, disabled=True)


def test_the_directory_shows_who_is_switched_off(box):
    """An account you cannot see is off is one you re-provision by accident."""
    conn, _ = box
    root = _root(conn)
    uid, _ = _person(conn, "leaver")
    admin.set_disabled(conn, root, user_id=uid, disabled=True)
    row = [u for u in admin.list_users(conn, root) if u["id"] == uid][0]
    assert row["disabled"] is True and row["disabled_at"] is not None


# ─── renaming ────────────────────────────────────────────────────────────────

def test_a_namespace_can_be_renamed_and_the_warning_says_what_breaks(box):
    conn, _ = box
    owner_id, owner = _person(conn, "owner")
    ns = identity.create_namespace(conn, owner_id, "notes")
    out = admin.edit_namespace(conn, owner, namespace_id=ns, name="records")
    assert out["name"] == "records" and "old name" in out["warning"]
    assert [s["name"] for s in identity.list_spaces(conn, owner_id)] == ["records"]


def test_renaming_onto_a_name_the_owner_already_uses_is_refused(box):
    conn, _ = box
    owner_id, owner = _person(conn, "owner")
    ns = identity.create_namespace(conn, owner_id, "notes")
    identity.create_namespace(conn, owner_id, "records")
    with pytest.raises(identity.SpaceAmbiguous, match="already owns"):
        admin.edit_namespace(conn, owner, namespace_id=ns, name="records")


def test_renaming_onto_ones_own_name_is_not_a_conflict(box):
    """The namespace being renamed must not collide with ITSELF."""
    conn, _ = box
    owner_id, owner = _person(conn, "owner")
    ns = identity.create_namespace(conn, owner_id, "notes")
    admin.edit_namespace(conn, owner, namespace_id=ns, name="notes")


def test_renaming_onto_an_alias_is_refused(box):
    """An alias of the owner's already means something else, so the namespace
    would be born unaddressable by its own name."""
    conn, _ = box
    owner_id, owner = _person(conn, "owner")
    ns = identity.create_namespace(conn, owner_id, "notes")
    other = identity.create_namespace(conn, owner_id, "other")
    identity.create_alias(conn, owner_id, "records", other)
    with pytest.raises(identity.SpaceAmbiguous, match="alias"):
        admin.edit_namespace(conn, owner, namespace_id=ns, name="records")


# ─── the credential that is valid and sees nothing ───────────────────────────

def test_scoping_a_credential_to_an_unreachable_namespace_warns(box):
    conn, _ = box
    root = _root(conn)
    owner_id, _ = _person(conn, "owner")
    outsider_id, _ = _person(conn, "outsider")
    ns = identity.create_namespace(conn, owner_id, "notes")

    for out in (admin.issue_token(conn, root, user_id=outsider_id,
                                  namespace_id=ns),
                admin.create_enrollment(conn, root, user_id=outsider_id,
                                        namespace_id=ns)):
        assert "add_member" in out["warning"]

    admin.add_member(conn, root, namespace_id=ns, user_id=outsider_id)
    assert "warning" not in admin.create_enrollment(conn, root,
                                                    user_id=outsider_id,
                                                    namespace_id=ns)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))


def test_creating_the_same_namespace_twice_still_returns_the_same_one(box):
    """A regression guard for the rename work, not a new feature.

    `create_namespace` is an idempotent upsert by design — that is what makes a
    provisioning script re-runnable — and the collision check written for
    renaming turned the second call into a refusal. The existing suite caught
    it; this pins the property next to the check that nearly removed it.
    """
    conn, _ = box
    owner_id, _ = _person(conn, "owner")
    first = identity.create_namespace(conn, owner_id, "notes")
    assert identity.create_namespace(conn, owner_id, "notes") == first
